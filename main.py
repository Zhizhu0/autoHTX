import math
import os
import time

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
import json
from datetime import datetime
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


def get_current_settings():
    """获取当前配置的交易对、杠杆、开关等"""
    symbol = db.get_config("trade_symbol")
    if not symbol:
        symbol = "ETH-USDT"

    leverage = db.get_config("trade_leverage")
    if not leverage:
        leverage = 5
    else:
        leverage = int(leverage)

    return symbol, leverage


# --- 核心业务逻辑 ---

async def gather_market_data(symbol):
    """收集数据并生成图表"""
    data_context = {}
    chart_images = []

    # 1. K线与图表
    periods = [('15min', '15min'), ('1hour', '1hour'), ('4hour', '4hour'), ('1day', '1day')]
    for pid, p_name in periods:
        df = huobi.get_kline(symbol, pid)
        img = chart_gen.generate_chart_base64(df, f"{symbol} {p_name}")
        chart_images.append(img)

    # 2. 账户信息
    try:
        acc_info = huobi.get_account_info(symbol)
        market_detail = huobi.get_market_detail(symbol)
        tpsl = huobi.get_tpsl_openorders(symbol)

        info_text = f"【当前 {symbol} 市场与账户信息】\n"
        if 'tick' in market_detail:
            tick = market_detail['tick']
            info_text += f"24H最高: {tick.get('high')}, 24H最低: {tick.get('low')}, 现价: {tick.get('close')}\n"

        info_text += "当前持仓: "
        positions = acc_info.get('positions', [])
        # 将持仓列表传出去给执行逻辑使用
        current_positions = positions

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


def construct_prompt(info_text):
    schema = {
        "summary": "对当前操作的评价或解释，一般不超过50字",
        "do": [
            {
                "action": "GO_LONG(开多) | GO_SHORT(开空) | CLOSE_LONG(平多) | CLOSE_SHORT(平空) | CANCEL(撤销挂单)",
                "price": "交易价格 (纯数字，请输入一个具体数字，而不是范围，如果需要市价交易请填入0)",
                "amount_level": "交易数量级别，输入0-3的整数，0表示极轻仓，1表示轻仓，2表示中仓，3表示重仓，一般来说，开仓时，1表示总额的10%，2表示总额的20%，3表示总额的30%，填入0时会以最小可交易价格开仓（即0.01），平仓时，填入1将平仓30%，2将会平仓50%，3将会平仓100%，填入0没有任何作用。当action为CANCEL时该值没有任何作用",
                "order_id": "挂单ID (填入所需要撤单的id，当action为CANCEL时必填，否则应该为空)",
                "take_profit": "止盈价格 (纯数字，请输入一个具体数字，而不是范围，当开仓时必填，其他情况不应该填)",
                "stop_loss": "止损价格 (纯数字，请输入一个具体数字，而不是范围，当开仓时必填，其他情况不应该填)"
            }
        ]
    }

    prompt = f"""
你只会回复JSON格式的字符串，不会回复其他任何内容。
不得进行任何形式的自我反思、自我纠正。
不得输出思考过程。所有推理都必须在内部完成。
禁止使用markdown标记，如果使用，我会将其标记为无效。
禁止输出空内容，如果不需要操作，参考后面的无操作输出。
现在时间为：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
请利用 Google Search 搜索最新的加密货币相关新闻、宏观经济数据。
结合我提供的 4 张图表（15m, 1h, 4h, 1d）进行技术分析，并给出具体操作。
你应该偏向稳健型交易，若非有十足的把握，止盈止损不应该设置到距离现价的2%以上。
同时多注意压力位和阻力位，分析过后将挂单尽量设置在未来一到四小时之内能够被触发的价格。
特别注意一下我已经拥有的持仓和挂单，不要重复操作，默认认为旧的挂单有设置止盈止损，在有更好的操作之前请先撤销已有挂单，若无更好操作，不要进行任何操作。
止盈止损不需要撤销，如果需要提前止盈止损，直接平仓操作。
另外，当行情很难判断时，建议保持观望状态，不要进行任何操作。

{info_text}

【重要】输出格式要求：
请务必返回纯净的 JSON 格式字符串，不要包含 Markdown 代码块（如 ```json ... ```）。
JSON 结构必须严格如下：
{json.dumps(schema, indent=4, ensure_ascii=False)}。

其中 do 的数量一般不超过4个，即开仓时最多同时两个做多和两个做空，平仓时最多同时平两个做多和两个做空。如果不需要操作，do应为空数组，但是一定要返回summary和空的do，而不是什么都不输出。
即不需要操作时，返回{{"summary":"{{{{无操作的理由}}}}","do":[]}}
请务必返回纯净的JSON。
务必返回纯净的JSON。
返回纯净的JSON，禁止使用markdown。
"""
    return prompt


async def execute_trade_action(action, positions, symbol, leverage):
    """执行单个交易指令"""
    print('action: ', action)
    act_type = action.get('action')
    price = float(action.get('price', 0))
    level = int(action.get('amount_level', 0))
    order_id = action.get('order_id')
    take_profit = float(action.get('take_profit', 0) or 0)
    stop_loss = float(action.get('stop_loss', 0) or 0)

    # --- 动态获取开仓张数映射 ---
    def get_vol(lvl, default):
        val = db.get_config(f"vol_level_{lvl}")
        return int(val) if val and val.isdigit() else default

    vol_map = {
        0: get_vol(0, 1),
        1: get_vol(1, 5),
        2: get_vol(2, 10),
        3: get_vol(3, 20)
    }

    # 获取对应等级的张数 (仅用于开仓)
    volume = vol_map.get(level, 1)

    log_msg = f"执行: {act_type} | 标的:{symbol} | 价格:{price} | 级别:{level} (Vol:{volume})"
    db.add_log("EXEC", log_msg)

    try:
        if act_type == "GO_LONG":
            return huobi.place_cross_order(symbol, "buy", "open", volume, price, take_profit, stop_loss, leverage)

        elif act_type == "GO_SHORT":
            return huobi.place_cross_order(symbol, "sell", "open", volume, price, take_profit, stop_loss, leverage)

        elif act_type == "CLOSE_LONG":
            volume = 0
            for pos in positions:
                if pos['direction'] == "buy":
                    # 平仓逻辑保持按持仓比例
                    if level == 0:
                        volume = 0
                    elif level == 1:
                        volume = math.floor(int(pos['volume']) * 0.3)
                    elif level == 2:
                        volume = math.floor(int(pos['volume']) * 0.5)
                    elif level == 3:
                        volume = pos['volume']

                    if volume > 0:
                        return huobi.place_cross_order(symbol, "sell", "close", volume, price, 0, 0, leverage)

        elif act_type == "CLOSE_SHORT":
            volume = 0
            for pos in positions:
                if pos['direction'] == "sell":
                    # 平仓逻辑保持按持仓比例
                    if level == 0:
                        volume = 0
                    elif level == 1:
                        volume = math.floor(int(pos['volume']) * 0.3)
                    elif level == 2:
                        volume = math.floor(int(pos['volume']) * 0.5)
                    elif level == 3:
                        volume = pos['volume']

                    if volume > 0:
                        return huobi.place_cross_order(symbol, "buy", "close", volume, price, 0, 0, leverage)

        elif act_type == "CANCEL":
            if order_id:
                return huobi.cancel_cross_order(symbol, order_id)
            else:
                db.add_log("EXEC_ERR", "撤单缺少 Order ID")

    except Exception as e:
        db.add_log("EXEC_ERR", str(e))
        print(e)


async def run_automated_trading():
    """自动化流程的主入口"""
    # 检查 Key 是否配置
    if not db.get_config("access_key") or not db.get_config("gemini_key"):
        db.add_log("SYSTEM", "API Key 未配置，跳过本次执行")
        return

    symbol, leverage = get_current_settings()
    db.add_log("SYSTEM", f"开始执行自动分析流程 ({symbol}, x{leverage})...")

    try:
        # 1. 准备数据
        context, images = await gather_market_data(symbol)

        # --- 新增：检查是否需要跳过 AI 分析 (如果有持仓) ---
        skip_when_holding = db.get_config("skip_when_holding") == "true"
        current_positions = context.get('positions', [])

        if skip_when_holding and len(current_positions) > 0:
            db.add_log("SYSTEM", "检测到当前有持仓，且配置了[有持仓跳过AI分析]，流程结束。")
            return

        prompt = construct_prompt(context['text'])

        # 2. 调用 AI
        db.add_log("AI", "正在请求 Gemini 分析...")
        result = ai.get_analysis(prompt, images)

        # 3. 记录分析摘要
        if 'summary' in result:
            db.add_log("SUMMARY", result['summary'])

        # 4. 执行操作
        actions = result.get('do', [])
        if not actions:
            db.add_log("ACTION", "AI 建议观望 (无操作)")
        else:
            for act in actions:
                await execute_trade_action(act, context.get('positions', []), symbol, leverage)

        db.add_log("SUCCESS", "流程执行完毕")

    except Exception as e:
        db.add_log("ERROR", f"自动流程异常: {str(e)}")
        print(e)


# --- 调度器 ---

async def scheduler_loop():
    """后台循环：基于时间戳的分钟级调度"""
    print(">>> 自动交易调度器已启动")
    while True:
        try:
            # 读取配置
            is_active_str = db.get_config("is_active")
            is_active = (is_active_str == "true")

            interval_str = db.get_config("trade_interval")
            try:
                interval = int(interval_str)
                if interval <= 0: interval = 60
            except:
                interval = 60  # 默认 60 分钟

            now_ts = int(time.time())
            current_slot = (now_ts // 60) // interval

            last_slot_str = db.get_config("last_run_slot")
            last_slot = int(last_slot_str) if last_slot_str else -1

            if current_slot != last_slot:
                if is_active:
                    db.add_log("SCHEDULER", f"触发定时任务 (Interval: {interval}m)")
                    asyncio.create_task(run_automated_trading())

                db.set_config("last_run_slot", str(current_slot))

        except Exception as e:
            print(f"Scheduler Error: {e}")

        await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduler_loop())


# --- Web 路由 ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse("config.html", {"request": request})


# 统一的配置映射表
CONFIG_MAP = {
    "getAIKey": "gemini_key",
    "getAccessKey": "access_key",
    "getSecretKey": "secret_key",
    "getSystemStatus": "is_active",
    "getInterval": "trade_interval",
    "getSymbol": "trade_symbol",
    "getLeverage": "trade_leverage",
    "getHuobiUrl": "huobi_api_url",
    # 新增映射
    "getSkipHolding": "skip_when_holding",
    # 张数映射
    "getVol0": "vol_level_0",
    "getVol1": "vol_level_1",
    "getVol2": "vol_level_2",
    "getVol3": "vol_level_3",
}

REVERSE_CONFIG_MAP = {
    "geminiKey": "gemini_key",
    "accessKey": "access_key",
    "secretKey": "secret_key",
    "systemStatus": "is_active",
    "tradeInterval": "trade_interval",
    "tradeSymbol": "trade_symbol",
    "tradeLeverage": "trade_leverage",
    "huobiUrl": "huobi_api_url",
    # 新增映射
    "skipWhenHolding": "skip_when_holding",
    # 张数映射
    "volLevel0": "vol_level_0",
    "volLevel1": "vol_level_1",
    "volLevel2": "vol_level_2",
    "volLevel3": "vol_level_3",
}


@app.get("/api/get_key/{key_name}")
async def get_key(key_name: str):
    db_key = CONFIG_MAP.get(key_name, "")
    val = db.get_config(db_key)

    # 设置默认值
    if key_name == "getSystemStatus" and val == "": val = "false"
    if key_name == "getSkipHolding" and val == "": val = "false"  # 默认为关闭
    if key_name == "getInterval" and val == "": val = "60"
    if key_name == "getSymbol" and val == "": val = "ETH-USDT"
    if key_name == "getLeverage" and val == "": val = "5"
    if key_name == "getHuobiUrl" and val == "": val = "https://api.hbdm.com"

    # 张数默认值
    if key_name == "getVol0" and val == "": val = "1"
    if key_name == "getVol1" and val == "": val = "5"
    if key_name == "getVol2" and val == "": val = "10"
    if key_name == "getVol3" and val == "": val = "20"

    return {"value": val}


@app.post("/api/set_key")
async def set_key(data: dict):
    key_name = data.get("key")
    value = data.get("value")

    # 校验
    if key_name == "tradeInterval":
        try:
            if int(value) <= 0: return {"status": "error", "msg": "Invalid interval"}
        except:
            return {"status": "error", "msg": "Invalid interval"}

    if key_name in REVERSE_CONFIG_MAP:
        db.set_config(REVERSE_CONFIG_MAP[key_name], str(value))
        return {"status": "ok"}
    return {"status": "error"}


@app.get("/logs_view", response_class=HTMLResponse)
async def logs_page(request: Request):
    logs = db.get_recent_logs(200)
    log_text = "\n".join([f"[{l[0]}] [{l[1]}] {l[2]}" for l in logs])
    return templates.TemplateResponse("logs.html", {"request": request, "log_text": log_text})


@app.get("/show", response_class=HTMLResponse)
async def show_prompt_page(request: Request):
    try:
        symbol, leverage = get_current_settings()
        context, images = await gather_market_data(symbol)
        prompt = construct_prompt(context['text'])
        return templates.TemplateResponse("show.html", {"request": request, "prompt": prompt, "images": images})
    except Exception as e:
        return HTMLResponse(f"Error: {str(e)}")


@app.post("/api/trigger_now")
async def trigger_now():
    asyncio.create_task(run_automated_trading())
    return {"status": "Triggered"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)