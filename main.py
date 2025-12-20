import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import feedparser
import jwt
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ai_service import GeminiClient
from chart_engine import ChartGenerator
from huobi_api import HuobiClient
from storage import db

# JWT 配置 (必须保持一致)
SECRET_KEY = "AUTOHTX_SECRET_KEY_PLEASE_CHANGE"
ALGORITHM = "HS256"
COOKIE_NAME = "access_token"

# --- 参数解析与 CLI 模式 ---
parser = argparse.ArgumentParser(description='AutoHTX Server')
group = parser.add_mutually_exclusive_group()
group.add_argument('--generate_key', action='store_true', help='Generate new server key and clear users')
group.add_argument('--generate_register_code', action='store_true', help='Generate a new registration code')

if __name__ == "__main__":
    args, unknown = parser.parse_known_args()

    if args.generate_key:
        print(">>> 正在生成新的服务器密钥...")
        try:
            key = db.generate_server_key()
            print(f"成功! 密钥已保存至 {db.key_path}")
            print("注意：数据库已被重置，所有旧用户数据已清空。")
        except Exception as e:
            print(f"Error: {e}")
        sys.exit(0)

    if args.generate_register_code:
        try:
            f = db.get_fernet()
            new_uuid = str(uuid.uuid4())
            payload = json.dumps({"uuid": new_uuid})
            code = f.encrypt(payload.encode()).decode()
            print("\n========== 注册码 (复制以下内容) ==========")
            print(code)
            print("=========================================\n")
        except Exception as e:
            print(f"Error: {e}")
            print("提示：请先运行 --generate_key 生成服务器密钥。")
        sys.exit(0)

# --- FastAPI App ---

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
from version import __version__

APP_VERSION = __version__

templates.env.globals["app_version"] = APP_VERSION

chart_gen = ChartGenerator()


# --- 自定义异常与处理器 ---

class NotAuthenticatedException(Exception):
    pass


@app.exception_handler(NotAuthenticatedException)
async def auth_exception_handler(_request: Request, _exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


# --- 全局状态管理 ---
class GlobalState:
    analyzing_status = {}
    chat_status = {}  # 记录 Chat AI 状态

    @classmethod
    def set_analyzing(cls, user_id, status):
        cls.analyzing_status[user_id] = status

    @classmethod
    def is_analyzing(cls, user_id):
        return cls.analyzing_status.get(user_id, False)

    @classmethod
    def set_chatting(cls, user_id, status):
        cls.chat_status[user_id] = status

    @classmethod
    def is_chatting(cls, user_id):
        return cls.chat_status.get(user_id, False)


# --- WebSocket 管理 ---
class ConnectionManager:
    def __init__(self):
        self.active_connections = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)

    def disconnect(self, websocket: WebSocket, user_id: int):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def broadcast_log(self, user_id, timestamp, level, message):
        if user_id not in self.active_connections: return
        payload = json.dumps({"type": "log", "data": f"[{timestamp}] [{level}] {message}"})
        for connection in list(self.active_connections[user_id]):
            try:
                await connection.send_text(payload)
            except:
                pass

    async def broadcast_status(self, user_id):
        """同时广播分析状态和聊天状态"""
        if user_id not in self.active_connections: return
        payload = json.dumps({
            "type": "status",
            "is_analyzing": GlobalState.is_analyzing(user_id),
            "is_chatting": GlobalState.is_chatting(user_id)
        })
        for connection in list(self.active_connections[user_id]):
            try:
                await connection.send_text(payload)
            except:
                pass


ws_manager = ConnectionManager()
main_event_loop = None


def on_db_log(user_id, t, l, m):
    global main_event_loop
    if main_event_loop and main_event_loop.is_running():
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast_log(user_id, t, l, m), main_event_loop)
    else:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(ws_manager.broadcast_log(user_id, t, l, m))
        except RuntimeError:
            pass


db.set_log_callback(on_db_log)


def get_aggression_prompt(level_str):
    try:
        level = int(level_str)
    except:
        level = 2

    if level == 0:
        return (
            "【极端保守策略】\n"
            "你的策略极其保守，优先关注日线级别图表。\n"
            "止损位置应设置在趋势完全反转的情况下（通常较宽），并且倾向于分批挂单。\n"
            "止盈设置在可预见的大级别阻力/支撑处。\n"
            "首要任务是本金安全，绝大部分时间选择观望，只有在确定性极高时才操作。\n"
            "几乎不去修改已有的挂单。"
        )
    elif level == 1:
        return (
            "【稳健趋势策略】\n"
            "你的策略较为稳健，主要关注1小时到4小时图表。\n"
            "尽可能地顺应大方向趋势，绝对不开逆势的单子。\n"
            "一般选择限价单（Pending Order）等待回调成交，不追单。\n"
            "大部分时间选择观望。"
        )
    elif level == 2:
        return (
            "【平衡/震荡策略】\n"
            "你的策略一般较为稳健，关注震荡区间。\n"
            "经常会同时持有多空两个方向的挂单（高抛低吸/网格思维）。\n"
            "根据当前是在区间上沿还是下沿来决定操作。\n"
            "止盈止损设置适中。"
        )
    elif level == 3:
        return (
            "【激进日内策略】\n"
            "你的策略较为激进，关注15分钟到1小时图表。\n"
            "为了不错过行情，偶尔会选择市价开单（Market Order）。\n"
            "止盈止损设置较近，一般不会超过开仓价的 1%。\n"
            "追求波段利润。"
        )
    elif level == 4:
        return (
            "【极激进/剥头皮策略】\n"
            "你的策略非常激进，关注短期动能和5分钟/15分钟图表。\n"
            "经常选择市价开单追逐波动。\n"
            "止损非常严格且极窄，一旦走势不对立即离场。\n"
            "交易频率较高。"
        )
    return ""


# --- 鉴权依赖 ---

async def get_current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]

    if not token:
        return None

    if token.startswith("Bearer "):
        token = token.replace("Bearer ", "")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id == "system_cli_admin":
            return "system_cli_admin"
        if user_id is None:
            return None
        return int(user_id)
    except jwt.PyJWTError:
        return None


async def login_required(request: Request, user_id=Depends(get_current_user)):
    if not user_id:
        if request.url.path.startswith("/api"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        raise NotAuthenticatedException()
    return user_id


# --- 核心业务逻辑 ---

def get_latest_news():
    try:
        feed_url = "https://cointelegraph.com/rss"
        feed = feedparser.parse(feed_url)
        news_text = "【最新市场新闻】\n"
        for entry in feed.entries[:5]: news_text += f"- {entry.title}\n"
        return news_text
    except:
        return "【新闻获取失败】\n"


async def gather_market_data(user_id, symbol):
    huobi = HuobiClient(user_id)

    def _sync_gather():
        data_context = {}
        chart_images = []
        periods = [('15min', '15min'), ('1hour', '1hour'), ('4hour', '4hour'), ('1day', '1day')]
        for pid, p_name in periods:
            df = huobi.get_kline(symbol, pid)
            img = chart_gen.generate_chart_base64(df, f"{symbol} {p_name}")
            chart_images.append(img)

        news = get_latest_news()
        info_text = news + "\n" + f"【{symbol} 市场/账户】\n"

        try:
            acc_info = huobi.get_account_info(symbol)
            market_detail = huobi.get_market_detail(symbol)
            tpsl = huobi.get_tpsl_openorders(symbol)

            if 'tick' in market_detail:
                tick = market_detail['tick']
                info_text += f"现价: {tick.get('close')}\n"

            info_text += "持仓: "
            positions = acc_info.get('positions', [])
            if positions:
                for p in positions: info_text += f"[{p['direction']} {p['volume']}张 开仓均价：{p['cost_open']} 盈亏：{p['profit']}] "
            else:
                info_text += "无"

            info_text += "\n挂单: "
            orders = acc_info.get('orders', [])
            if orders:
                for o in orders:
                    info_text += f"[{o['direction']} {o['offset']} {o['volume']}张 价格：{o['price']} id：{o['order_id_str']}] "
            else:
                info_text += "无"

            info_text += "\n当前TP/SL: "
            if tpsl and 'data' in tpsl and tpsl['data']:
                tpsl_orders = tpsl['data'].get('orders', [])
                if tpsl_orders:
                    for t in tpsl_orders:
                        info_text += f"【TP/SL】{t['direction']} {t['volume']}张 触发价格：{t['trigger_price']}\n"
                else:
                    info_text += "无TP/SL"
            else:
                info_text += "无TP/SL"

            data_context['text'] = info_text
            data_context['positions'] = positions
        except Exception as e:
            info_text += f"\n获取账户信息失败: {str(e)}"
            data_context['text'] = info_text
            data_context['positions'] = []

        return data_context, chart_images

    return await asyncio.to_thread(_sync_gather)


async def execute_trade_action(user_id, action, positions, symbol, leverage):
    huobi = HuobiClient(user_id)

    def _sync_exec():
        act_type = action.get('action')
        price = float(action.get('price', 0))
        level = int(action.get('amount_level', 0))
        order_id = action.get('order_id')
        take_profit = float(action.get('take_profit', 0) or 0)
        stop_loss = float(action.get('stop_loss', 0) or 0)

        def get_vol(lvl, default):
            val = db.get_config(user_id, f"vol_level_{lvl}")
            return int(val) if val and val.isdigit() else default

        vol_map = {0: get_vol(0, 1), 1: get_vol(1, 5), 2: get_vol(2, 10), 3: get_vol(3, 20)}
        volume = vol_map.get(level, 1)

        db.add_log(user_id, "EXEC", f"{act_type} {symbol} P:{price} V:{volume}")

        try:
            if act_type == "GO_LONG":
                huobi.place_cross_order(symbol, "buy", "open", volume, price, take_profit, stop_loss, leverage)
            elif act_type == "GO_SHORT":
                huobi.place_cross_order(symbol, "sell", "open", volume, price, take_profit, stop_loss, leverage)
            elif act_type == "CLOSE_LONG":
                vol_to_close = 0
                for p in positions:
                    if p['direction'] == "buy":
                        if level == 1:
                            vol_to_close = int(p['volume']) // 3
                        elif level == 2:
                            vol_to_close = int(p['volume']) // 2
                        elif level == 3:
                            vol_to_close = int(p['volume'])
                if vol_to_close > 0:
                    huobi.place_cross_order(symbol, "sell", "close", vol_to_close, price, 0, 0, leverage)
            elif act_type == "CLOSE_SHORT":
                vol_to_close = 0
                for p in positions:
                    if p['direction'] == "sell":
                        if level == 1:
                            vol_to_close = int(p['volume']) // 3
                        elif level == 2:
                            vol_to_close = int(p['volume']) // 2
                        elif level == 3:
                            vol_to_close = int(p['volume'])
                if vol_to_close > 0:
                    huobi.place_cross_order(symbol, "buy", "close", vol_to_close, price, 0, 0, leverage)
            elif act_type == "CANCEL":
                if order_id: huobi.cancel_cross_order(symbol, order_id)
        except Exception as e:
            db.add_log(user_id, "EXEC_ERR", str(e))

    await asyncio.to_thread(_sync_exec)


async def build_ai_context(user_id, symbol):
    """构建AI所需的上下文数据"""
    # 1. 市场数据
    context_data, images = await gather_market_data(user_id, symbol)

    # 2. 策略
    agg_level = db.get_config(user_id, "aggression_level") or "2"

    use_custom = db.get_config(user_id, "use_custom_strategy") == "true"
    strategy_prompt = ""

    if use_custom:
        # 尝试获取当前等级的自定义策略
        custom_text = db.get_config(user_id, f"custom_strategy_{agg_level}")
        if custom_text and custom_text.strip():
            strategy_prompt = custom_text

    # 如果没有开启自定义，或者自定义内容为空，使用默认生成的
    if not strategy_prompt:
        strategy_prompt = get_aggression_prompt(agg_level)

    # 3. 历史交互日志 (User Input & AI Summary & Chat AI)
    logs = db.get_context_logs(user_id, limit=10)
    log_history_text = "\n【近期交互记录】\n"
    if logs:
        for l_ts, l_level, l_msg in logs:
            if l_level == "USER_INPUT":
                prefix = "User: "
            elif l_level == "CHAT_AI":
                prefix = "Chat AI: "
            else:
                prefix = "Analysis AI: "  # for SUMMARY
            log_history_text += f"[{l_ts}] {prefix}{l_msg}\n"
    else:
        log_history_text += "无\n"

    final_prompt = f"当前时间:{datetime.now()}\n{context_data['text']}\n【核心策略配置】\n{strategy_prompt}\n{log_history_text}"
    return final_prompt, images, context_data, agg_level


async def run_automated_trading(user_id, force=False):
    if user_id == "system_cli_admin": return
    if GlobalState.is_analyzing(user_id): return
    GlobalState.set_analyzing(user_id, True)
    await ws_manager.broadcast_status(user_id)

    try:
        gemini_key = db.get_config(user_id, "gemini_key")
        access_key = db.get_config(user_id, "access_key")
        if not gemini_key or not access_key:
            db.add_log(user_id, "SYSTEM", "API Key 未配置")
            return

        symbol = db.get_config(user_id, "trade_symbol") or "ETH-USDT"
        leverage = db.get_config(user_id, "trade_leverage") or 5

        # 构建上下文
        user_prompt, images, context_data, agg_level = await build_ai_context(user_id, symbol)

        if not force:
            skip = db.get_config(user_id, "skip_when_holding") == "true"
            # 检查是否有持仓
            has_positions = len(context_data['positions']) > 0

            if skip and has_positions:
                # 获取最大允许跳过次数
                max_skip_str = db.get_config(user_id, "max_skip_count")
                try:
                    max_skip_count = int(max_skip_str) if max_skip_str else -1
                except:
                    max_skip_count = -1

                # 如果设置了正数，则启用计数逻辑 (使用 DB 持久化)
                if max_skip_count >= 0:
                    # 从数据库获取当前跳过次数
                    current_skip_str = db.get_config(user_id, "system_skip_count")
                    current_skip = int(current_skip_str) if current_skip_str and current_skip_str.isdigit() else 0

                    if current_skip >= max_skip_count:
                        db.add_log(user_id, "SYSTEM", f"持仓跳过次数达到上限 ({current_skip})，强制分析")
                    else:
                        # 增加计数并写入 DB
                        db.set_config(user_id, "system_skip_count", str(current_skip + 1))
                        db.add_log(user_id, "SYSTEM", f"持仓跳过 ({current_skip + 1}/{max_skip_count})")
                        return True
                else:
                    # 默认逻辑：无限跳过
                    db.add_log(user_id, "SYSTEM", "持仓跳过")
                    return True
            else:
                # 如果没有持仓，或者没开启跳过，重置计数器 (写入 DB)
                # 只有当 'skip and has_positions' 这个条件不满足时才重置
                if not has_positions:
                     db.set_config(user_id, "system_skip_count", "0")

        # 如果能执行到这里，说明进行了 AI 分析，此时也应该重置跳过计数器 (写入 DB)
        db.set_config(user_id, "system_skip_count", "0")

        schema = {
            "analysis": "这里用中文详细分析技术面核心逻辑（如：RSI超卖金叉，回踩EMA20支撑）",
            "position_check": "这里用中文明确持仓状态（如：'持有ETH多单(均价3000)，当前价格2900已跌破1%，触发补仓逻辑' 或 '价格未变，拒绝重复开单'）",
            "do": [
                {
                    "action": "GO_LONG | GO_SHORT | CLOSE_LONG | CLOSE_SHORT | CANCEL",
                    "price": "价格 (纯数字, 市价填 0)",
                    "amount_level": "0-3整数 (0:极轻仓, 1:轻仓, 2:中仓, 3:重仓)",
                    "order_id": "撤单ID (仅 CANCEL 时必填)",
                    "take_profit": "止盈价格 (必须设置)",
                    "stop_loss": "止损价格 (必须设置)"
                }
            ],
            "summary": "给用户的最终中文回复，解释决策理由（50字以内，语气专业）"
        }

        # SysPrompt
        sys_prompt = f"""
        # Role
        You are an expert **Crypto Quantitative Trader AI**.
        Your objective is to make profitable and safe trading decisions based on market data.

        # Output Format Protocol (CRITICAL)
        You must structure your response in two distinct parts:

        **Part 1: The Deep Thinking Process**
        Enclose your reasoning inside `<thought>` tags. 
        In this section, you must write a detailed, unstructured analysis in **English or Chinese**. You must act like a strict risk manager debating with a trader.
        You must cover:
        1. **Market Structure:** Analyze the 4 K-line charts (Trend, EMA, RSI, Bollinger Bands). Identify Key Support/Resistance levels.
        2. **Account Audit:** Look at `current_positions` and `open_orders`. 
        3. **Strategy Check:** Decide if this is a new entry, a text-book add-on (DCA/Pyramid), or a wait.
        4. **Final Decision:** Conclude exactly what to do before generating the JSON.

        **Part 2: The Execution Command**
        After the thinking process, output the final result in a **JSON Code Block**.
        Format:
        ```json
        {json.dumps(schema, indent=4, ensure_ascii=False)}
        ```

        # Rules of Engagement

        1. **Data Driven:** 
           Base logic ONLY on provided OHLCV and account data. Do NOT hallucinate.

        2. **Smart Position Management (The Logic Tree):**
           - **Case A: No Position:** If signals align -> Open New Position.
           - **Case B: Redundant Orders (FORBIDDEN):** If you already hold a position and the current price is very close (< 0.5% diff) to the entry price -> **Do NOT open** a new order. This is wasting fees.
           - **Case C: Strategic Adds (ALLOWED):** You MAY add to a position (DCA or Trend Pyramid) **ONLY IF**:
             1. The price has moved significantly (e.g., >1% drop for Long DCA, or Breakout for Pyramid).
             2. AND technical indicators confirm the move.
             3. AND total position size remains safe.

        3. **Risk Control:** 
           - NEVER open a trade without Stop Loss (SL) and Take Profit (TP).
           - If `open_orders` contains outdated pending orders, issue `CANCEL` instructions first.

        4. **User Interaction:** 
           - If the user's last message is a question, answer it in the `summary` field inside the JSON.

        # Example Response Structure
        <thought>
        1. Market: ETH dropped to 2900, hitting daily support. RSI is oversold.
        2. Account: User holds Long from 3000. Current PnL is -3.3%.
        3. Logic Check: Price gap is >1% and support held. This is a valid DCA opportunity, NOT a redundant order.
        4. Plan: Open small Long to average down.
        </thought>

        ```json
        {{
            "analysis": "ETH回踩日线支撑位2900，RSI进入超卖区，存在反弹需求。",
            "position_check": "当前持有均价3000的多单，现价2900偏离幅度超3%，符合DCA补仓条件。",
            "do": [
                {{
                    "action": "GO_LONG",
                    "price": "2900",
                    "amount_level": "1",
                    "take_profit": "3050",
                    "stop_loss": "2850"
                }}
            ],
            "summary": "触及关键支撑，且偏离持仓均价较多，建议轻仓补仓以摊低成本。"
        }}
        ```
        """

        ai = GeminiClient(user_id)
        db.add_log(user_id, "AI", f"请求分析(Lv.{agg_level})...")
        result = await asyncio.to_thread(ai.get_analysis, sys_prompt, user_prompt, images)

        if 'summary' in result: db.add_log(user_id, "SUMMARY", result['summary'])
        if 'analysis' in result: db.add_log(user_id, "ANALYSIS", result['analysis'])
        if 'position_check' in result: db.add_log(user_id, "POSITION_CHECK", result['position_check'])


        actions = result.get('do', [])
        if not actions:
            db.add_log(user_id, "ACTION", "观望")
        else:
            for act in actions:
                await execute_trade_action(user_id, act, context_data['positions'], symbol, leverage)

        db.add_log(user_id, "SUCCESS", "流程结束")
        return True
    except Exception as e:
        db.add_log(user_id, "ERROR", f"流程异常: {e}")
        return False
    finally:
        GlobalState.set_analyzing(user_id, False)
        await ws_manager.broadcast_status(user_id)


# --- 调度器 ---

retry_tracker = {}

async def scheduler_loop():
    print(">>> 多用户交易调度器已启动")
    while True:
        try:
            active_users = db.get_all_active_users()
            now_ts = int(time.time())

            for user_id in active_users:
                if GlobalState.is_analyzing(user_id): continue

                interval_str = db.get_config(user_id, "trade_interval")
                interval = int(interval_str) if interval_str and interval_str.isdigit() else 60
                if interval <= 0: interval = 60

                current_slot = (now_ts // 60) // interval
                last_slot_str = db.get_config(user_id, "last_run_slot")
                last_slot = int(last_slot_str) if last_slot_str else -1

                if current_slot != last_slot:
                    # 这里不再只是简单打印，而是进入带有重试计数的执行逻辑
                    # 只有当第一次尝试时才记录日志，避免刷屏
                    tracker = retry_tracker.get(user_id, {})
                    if tracker.get("slot") != current_slot:
                        db.add_log(user_id, "SCHEDULER", f"触发定时任务 ({interval}m)")

                    asyncio.create_task(run_and_update(user_id, current_slot, interval))

        except Exception as e:
            print(f"Scheduler Loop Error: {e}")
        await asyncio.sleep(5)


async def run_and_update(user_id, slot, interval):
    # 1. 初始化或更新计数器
    if user_id not in retry_tracker or retry_tracker[user_id]["slot"] != slot:
        retry_tracker[user_id] = {"slot": slot, "count": 0}

    retry_tracker[user_id]["count"] += 1
    current_attempt = retry_tracker[user_id]["count"]

    # 2. 执行任务
    success = await run_automated_trading(user_id, force=False)

    # 3. 获取重试配置
    should_retry = db.get_config(user_id, "ensure_valid_req") == "true"
    max_retries_str = db.get_config(user_id, "max_retry_count")
    # 默认为 -1 (无限重试)，保持向后兼容
    try:
        max_retries = int(max_retries_str) if max_retries_str else -1
    except:
        max_retries = -1

    # 4. 判断是否结束本次时间槽的任务
    finish_task = False

    if success:
        finish_task = True
    elif not should_retry:
        # 失败了，但没开启重试 -> 结束
        finish_task = True
    else:
        # 失败了，且开启了重试 -> 检查次数
        if max_retries < 0:
            # 负数：无限重试，不做操作，等待下一次循环
            pass
        elif max_retries == 0:
            # 0次重试：等同于不重试 -> 结束
            finish_task = True
        else:
            # 正数：检查是否超过 (当前尝试次数 > 最大重试次数)
            # 例如 max=3。第1次失败(1>3 False)，第2次(2>3 False)，第3次(3>3 False)，第4次(4>3 True) -> 结束
            # 这里的 max_retries 指的是“重试”次数。总尝试次数 = 1 + max_retries
            if current_attempt > max_retries:
                db.add_log(user_id, "SYSTEM", f"重试次数耗尽 ({max_retries}次)，跳过本次任务")
                finish_task = True
            else:
                # 还可以重试
                pass

    if finish_task:
        db.set_config(user_id, "last_run_slot", str(slot))
        # 清理内存
        if user_id in retry_tracker:
            del retry_tracker[user_id]


# --- 登录注册路由 ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username")
    password = form.get("password")
    user_id = db.verify_user(username, password)
    if not user_id:
        return templates.TemplateResponse("login.html", {"request": request, "error": "用户名或密码错误"})
    token = jwt.encode({
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=7)
    }, SECRET_KEY, algorithm=ALGORITHM)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(key=COOKIE_NAME, value=f"Bearer {token}", httponly=True)
    return resp


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    username = form.get("username")
    p1 = form.get("password")
    p2 = form.get("confirm_password")
    code = form.get("register_code")
    if p1 != p2:
        return templates.TemplateResponse("register.html", {"request": request, "error": "两次密码不一致"})
    try:
        db.register_user(username, p1, code)
        return RedirectResponse("/login", status_code=303)
    except Exception as e:
        return templates.TemplateResponse("register.html", {"request": request, "error": str(e)})


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --- 受保护路由 ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, user_id=Depends(login_required)):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, user_id=Depends(login_required)):
    return templates.TemplateResponse("config.html", {"request": request})


@app.get("/logs_view", response_class=HTMLResponse)
async def logs_page(request: Request, user_id=Depends(login_required)):
    # 限制改为 200
    logs = db.get_recent_logs(user_id, 200)
    log_text = "\n".join([f"[{l[0]}] [{l[1]}] {l[2]}" for l in logs]) + "\n"
    trigger_immediate = db.get_config(user_id, "chat_trigger_immediate") == "true"
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "initial_logs": log_text,
        "trigger_immediate": trigger_immediate
    })


@app.get("/show", response_class=HTMLResponse)
async def show_prompt_page(request: Request, user_id=Depends(login_required)):
    return templates.TemplateResponse("show.html", {"request": request})


@app.get("/api/get_show_data")
async def get_show_data(user_id=Depends(login_required)):
    try:
        symbol = db.get_config(user_id, "trade_symbol") or "ETH-USDT"
        user_prompt, images, context, agg_level = await build_ai_context(user_id, symbol)
        return {"status": "ok", "prompt": user_prompt, "images": images}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


@app.post("/api/trigger_now")
async def trigger_now(user_id=Depends(login_required)):
    if GlobalState.is_analyzing(user_id):
        return {"status": "error", "msg": "Analyzing"}
    asyncio.create_task(run_automated_trading(user_id, force=True))
    return {"status": "ok"}


# --- 新增：消息与日志接口 ---

@app.post("/api/send_user_message")
async def send_user_message(data: dict, user_id=Depends(login_required)):
    msg = data.get("message", "").strip()
    if not msg: return {"status": "error", "msg": "Empty message"}
    db.add_log(user_id, "USER_INPUT", msg)
    return {"status": "ok"}


@app.post("/api/delete_logs")
async def delete_logs_endpoint(data: dict, user_id=Depends(login_required)):
    mode = data.get("mode", "all")  # 'all' or 'useless'
    try:
        db.delete_logs(user_id, mode)
        # 记录一条新日志说明操作
        if mode == "all":
            db.add_log(user_id, "SYSTEM", "已清空所有日志")
        else:
            db.add_log(user_id, "SYSTEM", "已清理无用日志")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


# 新增：获取最新日志的接口 (用于重连后刷新)
@app.get("/api/get_logs")
async def get_recent_logs_endpoint(user_id=Depends(login_required)):
    logs = db.get_recent_logs(user_id, 200)
    log_text = "\n".join([f"[{l[0]}] [{l[1]}] {l[2]}" for l in logs]) + "\n"
    return {"status": "ok", "data": log_text}


@app.post("/api/trigger_chat")
async def trigger_chat(user_id=Depends(login_required)):
    # 状态锁定：如果正在聊天中，拒绝请求
    if GlobalState.is_chatting(user_id):
        return {"status": "error", "msg": "Chat AI is busy"}

    GlobalState.set_chatting(user_id, True)
    await ws_manager.broadcast_status(user_id)

    db.add_log(user_id, "SYSTEM", "正在请求 Chat AI...")

    async def _chat_task():
        try:
            symbol = db.get_config(user_id, "trade_symbol") or "ETH-USDT"
            # 获取上下文
            user_prompt, images, _, _ = await build_ai_context(user_id, symbol)

            sys_prompt = """
            # Role
            You are an expert Crypto Trading Assistant. Your goal is to answer user inquiries accurately based on market data and interaction history.

            # Instructions
            1. **Analyze First:** Before answering, you MUST think deeply about the user's intent, the current market structure (Trend, Indicators, Account Position), and previous logs.
            2. **Format:**
               - Put your thinking process inside `<thought>` tags in **English**.
               - Put your final response to the user outside the tags in **Chinese**.
            3. **Style:** Be professional, concise, and objective. Do not use Markdown unless necessary. Do not use Emojis.

            # Example
            User: Should I close my long position?
            Assistant:
            <thought>
            User holds ETH long at 3000. Current price is 3100. RSI is 75 (Overbought). 
            Logic: Profit is secured, but trend is strong. Suggest partial close or trailing stop.
            </thought>
            建议你考虑分批止盈。目前RSI显示超买，虽然趋势向上，但回调风险增加。可以先平掉一半仓位锁定利润，剩下的设置移动止损。
            """

            ai = GeminiClient(user_id)
            response = await asyncio.to_thread(ai.get_chat_response, sys_prompt, user_prompt, images)

            db.add_log(user_id, "CHAT_AI", response)
        except Exception as e:
            db.add_log(user_id, "CHAT_ERR", str(e))
        finally:
            GlobalState.set_chatting(user_id, False)
            await ws_manager.broadcast_status(user_id)

    asyncio.create_task(_chat_task())
    return {"status": "ok"}


# --- 配置接口 ---

@app.get("/api/get_key/{key_name}")
async def get_key(key_name: str, user_id=Depends(login_required)):
    CONFIG_MAP = {
        "getAIKey": "gemini_key", "getAccessKey": "access_key", "getSecretKey": "secret_key",
        "getSystemStatus": "is_active", "getInterval": "trade_interval", "getSymbol": "trade_symbol",
        "getLeverage": "trade_leverage", "getHuobiUrl": "huobi_api_url",
        "getSkipHolding": "skip_when_holding",
        "getApiRetryCount": "api_max_retry",
        "getSkipCount": "max_skip_count",
        "getEnsureValid": "ensure_valid_req", "getEmptyAsNone": "empty_as_none",
        "getVol0": "vol_level_0", "getVol1": "vol_level_1", "getVol2": "vol_level_2", "getVol3": "vol_level_3",
        "getAggressionLevel": "aggression_level",
        "getAiApiUrl": "ai_api_url", "getAiModel": "ai_model",
        "getChatApiUrl": "chat_api_url", "getChatModel": "chat_model",
        "getUseSameAi": "use_same_ai", "getChatTrigger": "chat_trigger_immediate",
        "getRetryCount": "max_retry_count",
        "getUseCustomStrategy": "use_custom_strategy",
        "getCustomStrategy0": "custom_strategy_0",
        "getCustomStrategy1": "custom_strategy_1",
        "getCustomStrategy2": "custom_strategy_2",
        "getCustomStrategy3": "custom_strategy_3",
        "getCustomStrategy4": "custom_strategy_4",
    }
    db_key = CONFIG_MAP.get(key_name)
    if not db_key: return {"value": ""}

    val = db.get_config(user_id, db_key)
    # 默认值
    defaults = {
        "getSymbol": "ETH-USDT", "getLeverage": "5", "getInterval": "60", "getAggressionLevel": "2",
        "getAiApiUrl": "https://api.gemai.cc/v1/chat/completions",
        "getAiModel": "[满血A]gemini-3-pro-preview",
        "getRetryCount": "-1", # 默认无限
        "getSkipCount": "-1"   # 默认无限
    }
    if val == "" and key_name in defaults: val = defaults[key_name]
    return {"value": val}


@app.post("/api/set_key")
async def set_key(data: dict, user_id=Depends(login_required)):
    FRONTEND_TO_DB_MAP = {
        "geminiKey": "gemini_key", "accessKey": "access_key", "secretKey": "secret_key",
        "systemStatus": "is_active", "tradeInterval": "trade_interval", "tradeSymbol": "trade_symbol",
        "tradeLeverage": "trade_leverage", "huobiUrl": "huobi_api_url",
        "skipWhenHolding": "skip_when_holding",
        "apiRetryCount": "api_max_retry",
        "skipCount": "max_skip_count",
        "ensureValidReq": "ensure_valid_req", "emptyAsNone": "empty_as_none",
        "volLevel0": "vol_level_0", "volLevel1": "vol_level_1", "volLevel2": "vol_level_2", "volLevel3": "vol_level_3",
        "aggressionLevel": "aggression_level",
        "aiApiUrl": "ai_api_url", "aiModel": "ai_model",
        "chatApiUrl": "chat_api_url", "chatModel": "chat_model",
        "useSameAi": "use_same_ai", "chatTrigger": "chat_trigger_immediate",
        "retryCount": "max_retry_count",
        "useCustomStrategy": "use_custom_strategy",
        "customStrategy0": "custom_strategy_0",
        "customStrategy1": "custom_strategy_1",
        "customStrategy2": "custom_strategy_2",
        "customStrategy3": "custom_strategy_3",
        "customStrategy4": "custom_strategy_4",
    }
    key_name = data.get("key")
    if key_name in FRONTEND_TO_DB_MAP:
        db_key = FRONTEND_TO_DB_MAP[key_name]
        new_val = str(data.get("value"))
        old_val = db.get_config(user_id, db_key)

        if "Key" not in key_name and old_val != new_val:
            db.add_log(user_id, "CONFIG", f"修改配置 {key_name}: {old_val} -> {new_val}")

        db.set_config(user_id, db_key, new_val)
        return {"status": "ok"}
    return {"status": "error"}


@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    cookie = websocket.cookies.get(COOKIE_NAME)
    user_id = None
    if cookie:
        try:
            token = cookie.replace("Bearer ", "")
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id = int(payload.get("sub"))
        except:
            pass

    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws_manager.connect(websocket, user_id)
    # 连接时发送当前所有状态
    await ws_manager.broadcast_status(user_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping": continue
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, user_id)


@app.on_event("startup")
async def startup_event():
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()
    asyncio.create_task(scheduler_loop())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)