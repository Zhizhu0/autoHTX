import math
import os
import time
import asyncio
import json
from datetime import datetime
from typing import List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import feedparser


from storage import db
from huobi_api import HuobiClient
from chart_engine import ChartGenerator
from ai_service import GeminiClient

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

huobi = HuobiClient()
chart_gen = ChartGenerator()
ai = GeminiClient()


# --- 全局状态管理 ---
class GlobalState:
    is_analyzing = False


# --- WebSocket 管理 ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_log(self, timestamp, level, message):
        payload = json.dumps({
            "type": "log",
            "data": f"[{timestamp}] [{level}] {message}"
        })
        for connection in self.active_connections:
            try:
                await connection.send_text(payload)
            except:
                pass

    async def broadcast_status(self, is_analyzing):
        payload = json.dumps({
            "type": "status",
            "is_analyzing": is_analyzing
        })
        for connection in self.active_connections:
            try:
                await connection.send_text(payload)
            except:
                pass


ws_manager = ConnectionManager()


# 定义一个全局变量来存储主事件循环
main_event_loop = None

# 注册 DB 回调，当有日志写入时推送到 WS
def on_db_log(t, l, m):
    """
    跨线程安全的日志回调
    无论是在主线程还是子线程(to_thread)中调用 db.add_log，
    都能通过 run_coroutine_threadsafe 将广播任务调度到主循环执行。
    """
    global main_event_loop
    if main_event_loop and main_event_loop.is_running():
        # 调度协程到主循环中执行，这是跨线程调用的标准做法
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast_log(t, l, m), main_event_loop)
    else:
        # 如果循环还没准备好（极少数情况），回退到尝试获取当前循环
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(ws_manager.broadcast_log(t, l, m))
        except RuntimeError:
            pass

db.set_log_callback(on_db_log)


def get_current_settings():
    """获取当前配置的交易对、杠杆、开关等"""
    symbol = db.get_config("trade_symbol") or "ETH-USDT"
    leverage = db.get_config("trade_leverage") or 5
    return symbol, int(leverage)

def get_latest_news():
    """获取简单的加密货币新闻快讯"""
    try:
        # 使用 Cointelegraph 的 RSS 源 (也可以换成其他中文源)
        feed_url = "https://cointelegraph.com/rss"
        feed = feedparser.parse(feed_url)
        news_text = "【最新市场新闻 (仅供参考)】\n"
        # 取前 3 条新闻标题
        for entry in feed.entries[:5]:
            news_text += f"- {entry.title}\n"
        return news_text
    except Exception as e:
        print(f"新闻获取失败: {e}")
        return "【新闻获取失败，请仅参考技术面】\n"

# --- 核心业务逻辑 (使用 to_thread 避免阻塞) ---

async def gather_market_data(symbol):
    """收集数据并生成图表 (非阻塞封装)"""

    def _sync_gather():
        data_context = {}
        chart_images = []

        # 1. K线与图表
        periods = [('15min', '15min'), ('1hour', '1hour'), ('4hour', '4hour'), ('1day', '1day')]
        for pid, p_name in periods:
            df = huobi.get_kline(symbol, pid)
            img = chart_gen.generate_chart_base64(df, f"{symbol} {p_name}")
            chart_images.append(img)

        news_summary = get_latest_news()
        info_text = news_summary + "\n" + f"【当前 {symbol} 市场与账户信息】\n"

        # 2. 账户信息
        try:
            acc_info = huobi.get_account_info(symbol)
            market_detail = huobi.get_market_detail(symbol)
            tpsl = huobi.get_tpsl_openorders(symbol)

            if 'tick' in market_detail:
                tick = market_detail['tick']
                info_text += f"24H最高: {tick.get('high')}, 24H最低: {tick.get('low')}, 现价: {tick.get('close')}\n"

            info_text += "当前持仓: "
            positions = acc_info.get('positions', [])

            if positions:
                for p in positions:
                    info_text += f"[{p['direction']} {p['volume']}张 盈亏:{p['profit']}] "
            else:
                info_text += "无持仓"

            info_text += "\n当前挂单: "
            orders = acc_info.get('orders', [])
            if orders:
                for o in orders:
                    info_text += f"[{o['direction']} {o['offset']} {o['volume']}张 价格:{o['price']} id:{o['order_id_str']}] "
            else:
                info_text += "无挂单"

            info_text += "\n当前TP/SL: "
            if tpsl and 'data' in tpsl and tpsl['data']:
                tpsl_orders = tpsl['data'].get('orders', [])
                if tpsl_orders:
                    for t in tpsl_orders:
                        info_text += f"【TP/SL】{t['direction']} {t['volume']}张 触发价格: {t['trigger_price']}\n"
                else:
                    info_text += "无TP/SL"
            else:
                info_text += "无TP/SL"

            data_context['text'] = info_text
            data_context['positions'] = positions
            data_context['orders'] = orders

        except Exception as e:
            info_text = f"获取账户信息失败: {str(e)}"
            data_context['text'] = info_text
            data_context['positions'] = []
            data_context['orders'] = []

        return data_context, chart_images

    # 放入线程池运行
    return await asyncio.to_thread(_sync_gather)


def construct_system_prompt():
    """构造 System Prompt，包含核心规则"""
    schema = {
        "summary": "对当前操作的评价或解释，一般不超过50字",
        "do": [
            {
                "action": "GO_LONG(开多) | GO_SHORT(开空) | CLOSE_LONG(平多) | CLOSE_SHORT(平空) | CANCEL(撤销挂单)",
                "price": "交易价格 (纯数字，请输入一个具体数字，而不是范围，如果需要市价交易请填入0)",
                "amount_level": "交易数量级别，输入0-3的整数，0表示极轻仓，1表示轻仓，2表示中仓，3表示重仓",
                "order_id": "挂单ID (填入所需要撤单的id，当action为CANCEL时必填，否则应该为空)",
                "take_profit": "止盈价格 (纯数字，当开仓时必填)",
                "stop_loss": "止损价格 (纯数字，当开仓时必填)"
            }
        ]
    }

    prompt = f"""
你是一个专业的加密货币交易员 AI。
你只会回复纯净的 JSON 格式字符串，不包含 Markdown 标记。
不得进行自我反思、不输出思考过程。

【核心规则】
1. 参考提供的【最新市场新闻】以及 K 线图进行分析。
2. 结合提供的 4 张 K 线图进行技术分析。
3. 风格偏向稳健，止盈止损一般不超过现价 2%。
4. 优先关注压力位和阻力位。
5. 【重要】检查用户当前持仓和挂单，不要重复开仓。旧挂单如果不合适请先 CANCEL。
6. 如果行情不明朗，保持观望，do 数组为空。
7. 用户的挂单永远有设置止盈止损，不要因为用户信息中显示无TP/SL就认为无止盈止损。

【输出格式】
JSON 结构必须严格如下：
{json.dumps(schema, indent=4, ensure_ascii=False)}

不需要操作时，返回 {{"summary": "观望理由", "do": []}}。
禁止返回空内容。
"""
    return prompt


def construct_user_prompt(info_text):
    return f"""
现在时间为：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
以下是当前市场数据和账户信息：

{info_text}

"""


async def execute_trade_action(action, positions, symbol, leverage):
    """执行交易 (封装为非阻塞)"""

    def _sync_exec():
        print('action: ', action)
        act_type = action.get('action')
        price = float(action.get('price', 0))
        level = int(action.get('amount_level', 0))
        order_id = action.get('order_id')
        take_profit = float(action.get('take_profit', 0) or 0)
        stop_loss = float(action.get('stop_loss', 0) or 0)

        # 动态获取张数
        def get_vol(lvl, default):
            val = db.get_config(f"vol_level_{lvl}")
            return int(val) if val and val.isdigit() else default

        vol_map = {0: get_vol(0, 1), 1: get_vol(1, 5), 2: get_vol(2, 10), 3: get_vol(3, 20)}
        volume = vol_map.get(level, 1)

        log_msg = f"执行: {act_type} | 标的:{symbol} | 价格:{price} | 级别:{level} (Vol:{volume})"
        db.add_log("EXEC", log_msg)

        try:
            if act_type == "GO_LONG":
                return huobi.place_cross_order(symbol, "buy", "open", volume, price, take_profit, stop_loss, leverage)
            elif act_type == "GO_SHORT":
                return huobi.place_cross_order(symbol, "sell", "open", volume, price, take_profit, stop_loss, leverage)
            elif act_type == "CLOSE_LONG":
                # 计算平仓量逻辑略... (保持原样，省略部分代码以节省篇幅，实际应包含完整逻辑)
                vol_to_close = 0
                for pos in positions:
                    if pos['direction'] == "buy":
                        if level == 3:
                            vol_to_close = pos['volume']
                        elif level == 2:
                            vol_to_close = math.floor(int(pos['volume']) * 0.5)
                        elif level == 1:
                            vol_to_close = math.floor(int(pos['volume']) * 0.3)
                        else:
                            vol_to_close = 0
                if vol_to_close > 0:
                    return huobi.place_cross_order(symbol, "sell", "close", vol_to_close, price, 0, 0, leverage)

            elif act_type == "CLOSE_SHORT":
                # 同上
                vol_to_close = 0
                for pos in positions:
                    if pos['direction'] == "sell":
                        if level == 3:
                            vol_to_close = pos['volume']
                        elif level == 2:
                            vol_to_close = math.floor(int(pos['volume']) * 0.5)
                        elif level == 1:
                            vol_to_close = math.floor(int(pos['volume']) * 0.3)
                        else:
                            vol_to_close = 0
                if vol_to_close > 0:
                    return huobi.place_cross_order(symbol, "buy", "close", vol_to_close, price, 0, 0, leverage)

            elif act_type == "CANCEL":
                if order_id:
                    return huobi.cancel_cross_order(symbol, order_id)
                else:
                    db.add_log("EXEC_ERR", "撤单缺少 Order ID")
        except Exception as e:
            db.add_log("EXEC_ERR", str(e))
            print(e)

    # 线程池执行
    await asyncio.to_thread(_sync_exec)


async def run_automated_trading(force=False):
    """
    自动化流程
    :param force: 是否强制执行（忽略开关和持仓跳过配置），用于手动触发
    :return: Boolean (True=执行成功, False=发生错误)
    """
    if GlobalState.is_analyzing:
        db.add_log("SYSTEM", "当前正在分析中，跳过本次请求")
        return False

    GlobalState.is_analyzing = True
    await ws_manager.broadcast_status(True)

    try:
        # 1. 基础检查
        if not db.get_config("access_key") or not db.get_config("gemini_key"):
            db.add_log("SYSTEM", "API Key 未配置，停止")
            return False

        symbol, leverage = get_current_settings()

        # 2. 准备数据
        # 使用 await 调用非阻塞的数据收集
        context, images = await gather_market_data(symbol)

        # 3. 检查跳过条件 (仅当非强制执行时)
        if not force:
            skip_when_holding = db.get_config("skip_when_holding") == "true"
            current_positions = context.get('positions', [])
            if skip_when_holding and len(current_positions) > 0:
                db.add_log("SYSTEM", "检测到持仓且配置为跳过，流程结束")
                return True  # 视为逻辑成功完成

        # 4. 构造 Prompt
        system_prompt = construct_system_prompt()
        user_text = construct_user_prompt(context['text'])

        # 5. 调用 AI (线程池)
        db.add_log("AI", "正在请求 Gemini 分析...")

        # 使用 run_in_executor 或 to_thread
        result = await asyncio.to_thread(ai.get_analysis, system_prompt, user_text, images)

        # 6. 记录摘要
        if 'summary' in result:
            db.add_log("SUMMARY", result['summary'])

        # 7. 执行操作
        actions = result.get('do', [])
        if not actions:
            db.add_log("ACTION", "AI 建议观望")
        else:
            for act in actions:
                await execute_trade_action(act, context.get('positions', []), symbol, leverage)

        db.add_log("SUCCESS", "流程执行完毕")
        return True

    except Exception as e:
        db.add_log("ERROR", f"自动流程异常: {str(e)}")
        return False
    finally:
        GlobalState.is_analyzing = False
        await ws_manager.broadcast_status(False)


# --- 调度器 ---

async def scheduler_loop():
    print(">>> 自动交易调度器已启动")
    while True:
        try:
            is_active = (db.get_config("is_active") == "true")
            retry_on_error = (db.get_config("ensure_valid_req") == "true")

            interval_str = db.get_config("trade_interval")
            try:
                interval = int(interval_str) if interval_str else 60
                if interval <= 0: interval = 60
            except:
                interval = 60

            now_ts = int(time.time())
            current_slot = (now_ts // 60) // interval

            last_slot_str = db.get_config("last_run_slot")
            last_slot = int(last_slot_str) if last_slot_str else -1

            if current_slot != last_slot:
                if is_active:
                    db.add_log("SCHEDULER", f"触发定时任务 (Interval: {interval}m)")
                    success = await run_automated_trading(force=False)

                    # 关键逻辑：如果失败且配置了“保证有效请求”，则不更新 last_run_slot
                    # 这样下一轮循环（10秒后）会因为 current_slot != last_slot 再次尝试（直到本分钟过去或成功）
                    # 注意：如果一分钟内一直失败，会一直重试直到下一分钟 slot 变更，
                    # 更好的方式是只在成功时更新，或者记录 retry 次数。这里按需求：失败时不更新 slot。
                    print(success)
                    if success or not retry_on_error:
                        db.set_config("last_run_slot", str(current_slot))
                    else:
                        db.add_log("SCHEDULER", "任务执行失败，等待重试 (Ensure Valid Request)")
                else:
                    # 如果未激活，也更新 slot 防止激活瞬间连续运行
                    db.set_config("last_run_slot", str(current_slot))

        except Exception as e:
            print(f"Scheduler Error: {e}")

        await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    global main_event_loop
    # 获取当前运行的主循环（uvicorn的主循环）
    main_event_loop = asyncio.get_running_loop()
    asyncio.create_task(scheduler_loop())


# --- Web 路由 ---

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    # 发送当前状态
    await websocket.send_text(json.dumps({"type": "status", "is_analyzing": GlobalState.is_analyzing}))
    try:
        while True:
            await websocket.receive_text()  # 保持连接
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse("config.html", {"request": request})


@app.get("/logs_view", response_class=HTMLResponse)
async def logs_page(request: Request):
    # 首次加载旧日志
    logs = db.get_recent_logs(200)
    log_text = "\n".join([f"[{l[0]}] [{l[1]}] {l[2]}" for l in logs])
    log_text += "\n"
    return templates.TemplateResponse("logs.html", {"request": request, "initial_logs": log_text})


@app.get("/show", response_class=HTMLResponse)
async def show_prompt_page(request: Request):
    try:
        symbol, leverage = get_current_settings()
        # 复用 gather_market_data
        context, images = await gather_market_data(symbol)

        system_p = construct_system_prompt()
        user_p = construct_user_prompt(context['text'])

        full_display = f"--- SYSTEM ---\n{system_p}\n\n--- USER ---\n{user_p}"

        return templates.TemplateResponse("show.html", {"request": request, "prompt": full_display, "images": images})
    except Exception as e:
        return HTMLResponse(f"Error: {str(e)}")


@app.post("/api/trigger_now")
async def trigger_now():
    if GlobalState.is_analyzing:
        return {"status": "error", "msg": "AI is currently analyzing"}

    # 异步触发，不阻塞 HTTP 返回
    asyncio.create_task(run_automated_trading(force=True))
    return {"status": "ok", "msg": "Triggered"}


# 配置映射表更新
CONFIG_MAP = {
    "getAIKey": "gemini_key",
    "getAccessKey": "access_key",
    "getSecretKey": "secret_key",
    "getSystemStatus": "is_active",
    "getInterval": "trade_interval",
    "getSymbol": "trade_symbol",
    "getLeverage": "trade_leverage",
    "getHuobiUrl": "huobi_api_url",
    "getSkipHolding": "skip_when_holding",
    # 新增
    "getEnsureValid": "ensure_valid_req",
    "getEmptyAsNone": "empty_as_none",
    # Vol
    "getVol0": "vol_level_0",
    "getVol1": "vol_level_1",
    "getVol2": "vol_level_2",
    "getVol3": "vol_level_3",
}

REVERSE_CONFIG_MAP = {v: k for k, v in CONFIG_MAP.items()}
# 前端传来的 key 对应数据库 key
FRONTEND_TO_DB_MAP = {
    "geminiKey": "gemini_key",
    "accessKey": "access_key",
    "secretKey": "secret_key",
    "systemStatus": "is_active",
    "tradeInterval": "trade_interval",
    "tradeSymbol": "trade_symbol",
    "tradeLeverage": "trade_leverage",
    "huobiUrl": "huobi_api_url",
    "skipWhenHolding": "skip_when_holding",
    "ensureValidReq": "ensure_valid_req",
    "emptyAsNone": "empty_as_none",
    "volLevel0": "vol_level_0",
    "volLevel1": "vol_level_1",
    "volLevel2": "vol_level_2",
    "volLevel3": "vol_level_3",
}


@app.get("/api/get_key/{key_name}")
async def get_key(key_name: str):
    db_key = CONFIG_MAP.get(key_name, "")
    val = db.get_config(db_key)
    # 默认值处理
    defaults = {
        "getEnsureValid": "false",
        "getEmptyAsNone": "false",
        "getSystemStatus": "false",
        "getSymbol": "ETH-USDT",
        "getLeverage": "5",
        "getInterval": "60"
    }
    if val == "" and key_name in defaults:
        val = defaults[key_name]

    return {"value": val}


@app.post("/api/set_key")
async def set_key(data: dict):
    key_name = data.get("key")  # 前端ID
    value = data.get("value")

    if key_name in FRONTEND_TO_DB_MAP:
        db.set_config(FRONTEND_TO_DB_MAP[key_name], str(value))
        return {"status": "ok"}
    return {"status": "error"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)