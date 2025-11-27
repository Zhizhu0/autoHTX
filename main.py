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
templates = Jinja2Templates(directory="templates")

huobi = HuobiClient()
chart_gen = ChartGenerator()
ai = GeminiClient()
SYMBOL = "ETH-USDT"


# --- 核心业务逻辑 ---

async def gather_market_data():
    """收集数据并生成图表 (复用逻辑)"""
    data_context = {}
    chart_images = []

    # 1. K线与图表
    periods = [('15min', '15min'), ('1hour', '1hour'), ('4hour', '4hour'), ('1day', '1day')]
    for pid, p_name in periods:
        df = huobi.get_kline(SYMBOL, pid)
        img = chart_gen.generate_chart_base64(df, f"{SYMBOL} {p_name}")
        chart_images.append(img)

    # 2. 账户信息
    try:
        acc_info = huobi.get_account_info(SYMBOL)
        market_detail = huobi.get_market_detail(SYMBOL)

        info_text = f"【当前市场与账户信息】\n"
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
请利用 Google Search 搜索最新的以太坊相关新闻、宏观经济数据。
结合我提供的 4 张图表（15m, 1h, 4h, 1d）进行技术分析，并给出具体操作。
请注意，每次给你提供的数据间隔将会达到1小时，市场瞬息万变，不仅结合搜索结果和我提供的图表与仓位信息，还要考虑到下一次更新操作时间在一小时以后，无法实时盯盘，请充分思考过后再进行操作。
你应该偏向稳健型交易，若非有十足的把握，止盈止损不应该设置到距离现价的2%以上。
同时多注意压力位和阻力位，分析过后将挂单尽量设置在未来一到四小时之内能够被触发的价格。
特别注意一下我已经拥有的持仓和挂单，不要重复操作，默认认为旧的挂单有设置止盈止损，在有更好的操作之前请先撤销已有挂单，若无更好操作，不要进行任何操作。
另外，当行情很难判断时，建议保持观望状态，不要进行任何操作。
{info_text}

【重要】输出格式要求：
请务必返回纯净的 JSON 格式字符串，不要包含 Markdown 代码块（如 ```json ... ```）。
JSON 结构必须严格如下：
{json.dumps(schema, indent=4, ensure_ascii=False)}。

其中 do 的数量一般不超过4个，即开仓时最多同时两个做多和两个做空，平仓时最多同时平两个做多和两个做空。如果不需要操作，do应为空数组，但是一定要返回summary和空的do，而不是什么都不输出。
即不需要操作时，返回{{"summary":"{{{{无操作的理由}}}}","do":[]}}
请务必返回纯净的JSON。
"""
    return prompt


async def execute_trade_action(action, positions):
    """执行单个交易指令"""
    act_type = action.get('action')
    price = float(action.get('price', 0))
    level = int(action.get('amount_level', 0))
    order_id = action.get('order_id')

    # 仓位映射 (简单版: 1->1张, 2->5张, 3->10张) - 请根据资金量自行调整
    vol_map = {0: 1, 1: 1, 2: 5, 3: 10}
    volume = vol_map.get(level, 1)

    log_msg = f"执行: {act_type} | 价格:{price} | 级别:{level}"
    db.add_log("EXEC", log_msg)

    try:
        if act_type == "GO_LONG":
            return huobi.place_cross_order(SYMBOL, "buy", "open", volume, price)

        elif act_type == "GO_SHORT":
            return huobi.place_cross_order(SYMBOL, "sell", "open", volume, price)

        elif act_type == "CLOSE_LONG":
            # 查找持仓，全平或按比例平
            # 这里简化为: 如果有对应方向持仓，按 volume 平仓
            return huobi.place_cross_order(SYMBOL, "sell", "close", volume, price)

        elif act_type == "CLOSE_SHORT":
            return huobi.place_cross_order(SYMBOL, "buy", "close", volume, price)

        elif act_type == "CANCEL":
            if order_id:
                return huobi.cancel_cross_order(SYMBOL, order_id)
            else:
                db.add_log("EXEC_ERR", "撤单缺少 Order ID")

    except Exception as e:
        db.add_log("EXEC_ERR", str(e))


async def run_automated_trading():
    """自动化流程的主入口"""
    # 检查 Key 是否配置
    if not db.get_config("access_key") or not db.get_config("gemini_key"):
        db.add_log("SYSTEM", "API Key 未配置，跳过本次执行")
        return

    db.add_log("SYSTEM", "开始执行自动分析流程...")

    try:
        # 1. 准备数据
        context, images = await gather_market_data()
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
                await execute_trade_action(act, context.get('positions', []))

        db.add_log("SUCCESS", "流程执行完毕")

    except Exception as e:
        db.add_log("ERROR", f"自动流程异常: {str(e)}")


# --- 调度器 ---

async def scheduler_loop():
    """后台循环：每10秒检查一次"""
    print(">>> 自动交易调度器已启动")
    while True:
        try:
            now = datetime.now()
            current_hour = now.hour

            # 读取上次运行的小时
            last_run = db.get_config("last_run_hour")
            if last_run:
                last_run = int(last_run)
            else:
                last_run = -1

            # 判断是否是新的一小时
            if current_hour != last_run:
                db.add_log("SCHEDULER", f"检测到新小时 ({current_hour}:00)，触发任务")

                # 执行任务
                await run_automated_trading()

                # 更新状态
                db.set_config("last_run_hour", str(current_hour))

        except Exception as e:
            print(f"Scheduler Error: {e}")

        # 等待 10 秒
        await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    # 使用 asyncio.create_task 在后台启动循环，不阻塞主线程
    asyncio.create_task(scheduler_loop())


# --- Web 路由 (保持不变) ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    return templates.TemplateResponse("config.html", {"request": request})


@app.get("/api/get_key/{key_name}")
async def get_key(key_name: str):
    db_map = {"getAIKey": "gemini_key", "getAccessKey": "access_key", "getSecretKey": "secret_key"}
    val = db.get_config(db_map.get(key_name, ""))
    return {"value": val}


@app.post("/api/set_key")
async def set_key(data: dict):
    key_name = data.get("key")
    value = data.get("value")
    db_map = {"geminiKey": "gemini_key", "accessKey": "access_key", "secretKey": "secret_key"}
    if key_name in db_map:
        db.set_config(db_map[key_name], value)
        return {"status": "ok"}
    return {"status": "error"}


@app.get("/logs_view", response_class=HTMLResponse)
async def logs_page(request: Request):
    logs = db.get_recent_logs(200)  # 增加查看行数
    log_text = "\n".join([f"[{l[0]}] [{l[1]}] {l[2]}" for l in logs])
    return templates.TemplateResponse("logs.html", {"request": request, "log_text": log_text})


@app.get("/show", response_class=HTMLResponse)
async def show_prompt_page(request: Request):
    # 手动触发查看，不影响调度器
    try:
        context, images = await gather_market_data()
        prompt = construct_prompt(context['text'])
        return templates.TemplateResponse("show.html", {"request": request, "prompt": prompt, "images": images})
    except Exception as e:
        return HTMLResponse(f"Error: {str(e)}")


# 手动触发一次分析的接口 (测试用)
@app.post("/api/trigger_now")
async def trigger_now():
    background_tasks = BackgroundTasks()
    await run_automated_trading()
    return {"status": "Triggered"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)