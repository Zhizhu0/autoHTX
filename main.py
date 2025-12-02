import math
import os
import time
import asyncio
import json
import uuid
import sys
import argparse
import jwt
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import feedparser

from storage import db
from huobi_api import HuobiClient
from chart_engine import ChartGenerator
from ai_service import GeminiClient

# --- 参数解析与 CLI 模式 ---
parser = argparse.ArgumentParser(description='AutoHTX Server')
group = parser.add_mutually_exclusive_group()
group.add_argument('--generate_key', action='store_true', help='Generate new server key and clear users')
group.add_argument('--generate_register_code', action='store_true', help='Generate a new registration code')

if __name__ == "__main__":
    # 使用 parse_known_args 避免 uvicorn 启动时的干扰
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
            # 加密生成注册码
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

# 注意：如果没有 images 文件夹可能需要创建，或者确保 logic 处理了
# app.mount("/static", StaticFiles(directory="static"), name="static")

chart_gen = ChartGenerator()

# JWT 配置
SECRET_KEY = "AUTOHTX_SECRET_KEY_PLEASE_CHANGE"
ALGORITHM = "HS256"
COOKIE_NAME = "access_token"


# --- 自定义异常与处理器 (修复跳转问题的核心) ---

class NotAuthenticatedException(Exception):
    """自定义未登录异常，用于网页跳转"""
    pass


@app.exception_handler(NotAuthenticatedException)
async def auth_exception_handler(_request: Request, _exc: NotAuthenticatedException):
    """捕获未登录异常，重定向到登录页"""
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


# --- 全局状态管理 (多用户) ---
class GlobalState:
    # user_id -> boolean
    analyzing_status = {}

    @classmethod
    def set_analyzing(cls, user_id, status):
        cls.analyzing_status[user_id] = status

    @classmethod
    def is_analyzing(cls, user_id):
        return cls.analyzing_status.get(user_id, False)


# --- WebSocket 管理 (多用户) ---
class ConnectionManager:
    def __init__(self):
        # user_id -> List[WebSocket]
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
            # 清理空列表
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]

    async def broadcast_log(self, user_id, timestamp, level, message):
        if user_id not in self.active_connections: return
        payload = json.dumps({"type": "log", "data": f"[{timestamp}] [{level}] {message}"})
        # 复制列表进行迭代以防止迭代中修改
        for connection in list(self.active_connections[user_id]):
            try:
                await connection.send_text(payload)
            except:
                pass

    async def broadcast_status(self, user_id, is_analyzing):
        if user_id not in self.active_connections: return
        payload = json.dumps({"type": "status", "is_analyzing": is_analyzing})
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


# --- 鉴权依赖 ---

async def get_current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        if token.startswith("Bearer "):
            token = token.split(" ")[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            return None
        return int(user_id)
    except jwt.PyJWTError:
        return None


async def login_required(request: Request, user_id: Optional[int] = Depends(get_current_user)):
    """
    鉴权依赖项：
    - 如果是 API 请求，鉴权失败返回 401。
    - 如果是 页面 请求，鉴权失败抛出异常跳转到 /login。
    """
    if not user_id:
        # 这里的判断很简单：如果路径以 /api 开头，视为 API 请求
        if request.url.path.startswith("/api"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

        # 抛出异常，触发 auth_exception_handler 进行跳转
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
                for p in positions: info_text += f"[{p['direction']} {p['volume']}张 盈亏:{p['profit']}] "
            else:
                info_text += "无"

            info_text += "\n挂单: "
            orders = acc_info.get('orders', [])
            if orders:
                for o in orders:
                    info_text += f"[{o['direction']} {o['offset']} {o['volume']}张 价格:{o['price']} id:{o['order_id_str']}] "
            else:
                info_text += "无"

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


async def run_automated_trading(user_id, force=False):
    if GlobalState.is_analyzing(user_id): return
    GlobalState.set_analyzing(user_id, True)
    await ws_manager.broadcast_status(user_id, True)

    try:
        gemini_key = db.get_config(user_id, "gemini_key")
        access_key = db.get_config(user_id, "access_key")
        if not gemini_key or not access_key:
            db.add_log(user_id, "SYSTEM", "API Key 未配置")
            return

        symbol = db.get_config(user_id, "trade_symbol") or "ETH-USDT"
        leverage = db.get_config(user_id, "trade_leverage") or 5

        context, images = await gather_market_data(user_id, symbol)

        if not force:
            skip = db.get_config(user_id, "skip_when_holding") == "true"
            if skip and len(context['positions']) > 0:
                db.add_log(user_id, "SYSTEM", "持仓跳过")
                return True

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
        sys_prompt = f"""
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
        user_prompt = f"当前时间:{datetime.now()}\n{context['text']}"

        ai = GeminiClient(user_id)
        db.add_log(user_id, "AI", "请求分析...")
        result = await asyncio.to_thread(ai.get_analysis, sys_prompt, user_prompt, images)

        if 'summary' in result: db.add_log(user_id, "SUMMARY", result['summary'])

        actions = result.get('do', [])
        if not actions:
            db.add_log(user_id, "ACTION", "观望")
        else:
            for act in actions:
                await execute_trade_action(user_id, act, context['positions'], symbol, leverage)

        db.add_log(user_id, "SUCCESS", "流程结束")
        return True
    except Exception as e:
        db.add_log(user_id, "ERROR", f"流程异常: {e}")
        return False
    finally:
        GlobalState.set_analyzing(user_id, False)
        await ws_manager.broadcast_status(user_id, False)


# --- 调度器 ---

async def scheduler_loop():
    print(">>> 多用户交易调度器已启动")
    while True:
        try:
            active_users = db.get_all_active_users()
            now_ts = int(time.time())

            for user_id in active_users:
                # 【新增】关键修复：如果用户正在分析中，直接跳过，防止重复触发
                if GlobalState.is_analyzing(user_id):
                    continue

                # 独立获取每个用户的配置
                interval_str = db.get_config(user_id, "trade_interval")
                interval = int(interval_str) if interval_str and interval_str.isdigit() else 60
                if interval <= 0: interval = 60

                current_slot = (now_ts // 60) // interval
                last_slot_str = db.get_config(user_id, "last_run_slot")
                last_slot = int(last_slot_str) if last_slot_str else -1

                if current_slot != last_slot:
                    db.add_log(user_id, "SCHEDULER", f"触发定时任务 ({interval}m)")
                    # 这里 create_task 启动后，GlobalState 会在 run_automated_trading 内部被设为 True
                    # 但为了保险，建议任务启动稍作延时或依靠下一次循环检测
                    asyncio.create_task(run_and_update(user_id, current_slot, interval))

        except Exception as e:
            print(f"Scheduler Loop Error: {e}")
        await asyncio.sleep(5)


async def run_and_update(user_id, slot, interval):
    success = await run_automated_trading(user_id, force=False)
    retry = db.get_config(user_id, "ensure_valid_req") == "true"
    # 如果成功，或者未开启重试，则更新时间戳
    if success or not retry:
        db.set_config(user_id, "last_run_slot", str(slot))


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

    # 登录成功，生成Token
    token = jwt.encode({
        "sub": str(user_id),
        "exp": datetime.utcnow() + timedelta(days=7)
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
async def read_root(request: Request, user_id: int = Depends(login_required)):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, user_id: int = Depends(login_required)):
    return templates.TemplateResponse("config.html", {"request": request})


@app.get("/logs_view", response_class=HTMLResponse)
async def logs_page(request: Request, user_id: int = Depends(login_required)):
    logs = db.get_recent_logs(user_id, 200)
    log_text = "\n".join([f"[{l[0]}] [{l[1]}] {l[2]}" for l in logs]) + "\n"
    return templates.TemplateResponse("logs.html", {"request": request, "initial_logs": log_text})


@app.get("/show", response_class=HTMLResponse)
async def show_prompt_page(request: Request, user_id: int = Depends(login_required)):
    try:
        symbol = db.get_config(user_id, "trade_symbol") or "ETH-USDT"
        context, images = await gather_market_data(user_id, symbol)
        return templates.TemplateResponse("show.html",
                                          {"request": request, "prompt": context['text'], "images": images})
    except Exception as e:
        return HTMLResponse(f"Error: {e}")


@app.post("/api/trigger_now")
async def trigger_now(user_id: int = Depends(login_required)):
    if GlobalState.is_analyzing(user_id):
        return {"status": "error", "msg": "Analyzing"}
    asyncio.create_task(run_automated_trading(user_id, force=True))
    return {"status": "ok"}


@app.get("/api/get_key/{key_name}")
async def get_key(key_name: str, user_id: int = Depends(login_required)):
    CONFIG_MAP = {
        "getAIKey": "gemini_key", "getAccessKey": "access_key", "getSecretKey": "secret_key",
        "getSystemStatus": "is_active", "getInterval": "trade_interval", "getSymbol": "trade_symbol",
        "getLeverage": "trade_leverage", "getHuobiUrl": "huobi_api_url", "getSkipHolding": "skip_when_holding",
        "getEnsureValid": "ensure_valid_req", "getEmptyAsNone": "empty_as_none",
        "getVol0": "vol_level_0", "getVol1": "vol_level_1", "getVol2": "vol_level_2", "getVol3": "vol_level_3",
    }
    db_key = CONFIG_MAP.get(key_name)
    if not db_key: return {"value": ""}

    val = db.get_config(user_id, db_key)
    defaults = {"getSymbol": "ETH-USDT", "getLeverage": "5", "getInterval": "60"}
    if val == "" and key_name in defaults: val = defaults[key_name]
    return {"value": val}


@app.post("/api/set_key")
async def set_key(data: dict, user_id: int = Depends(login_required)):
    FRONTEND_TO_DB_MAP = {
        "geminiKey": "gemini_key", "accessKey": "access_key", "secretKey": "secret_key",
        "systemStatus": "is_active", "tradeInterval": "trade_interval", "tradeSymbol": "trade_symbol",
        "tradeLeverage": "trade_leverage", "huobiUrl": "huobi_api_url", "skipWhenHolding": "skip_when_holding",
        "ensureValidReq": "ensure_valid_req", "emptyAsNone": "empty_as_none",
        "volLevel0": "vol_level_0", "volLevel1": "vol_level_1", "volLevel2": "vol_level_2", "volLevel3": "vol_level_3",
    }
    key_name = data.get("key")
    if key_name in FRONTEND_TO_DB_MAP:
        db.set_config(user_id, FRONTEND_TO_DB_MAP[key_name], str(data.get("value")))
        return {"status": "ok"}
    return {"status": "error"}


@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    # 手动解析 Cookie 获取 user_id
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
    # 发送状态
    await ws_manager.broadcast_status(user_id, GlobalState.is_analyzing(user_id))
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                continue
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, user_id)


@app.on_event("startup")
async def startup_event():
    global main_event_loop
    main_event_loop = asyncio.get_running_loop()
    asyncio.create_task(scheduler_loop())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)