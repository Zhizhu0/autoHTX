import asyncio
import json
import gzip
import logging
import time
import os
from datetime import datetime
import pandas as pd
import aiohttp
from huobi_api import HuobiClient
from storage import db  # <--- 引入数据库以获取配置

# 配置日志
logger = logging.getLogger("MarketManager")


class MarketDataManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MarketDataManager, cls).__new__(cls)
            cls._instance.data_store = {}  # {symbol: {period: DataFrame}}
            cls._instance.lock = asyncio.Lock()
            cls._instance.active_symbol = "ETH-USDT"
            cls._instance.periods = ['15min', '60min', '4hour', '1day']
            cls._instance.running = False
            # 默认官方地址，后续会被 initialize_data 覆盖
            cls._instance.ws_url = "wss://api.hbdm.com/linear-swap-ws"
        return cls._instance

    async def initialize_data(self, user_id, symbol):
        """启动前先通过 REST API 拉取历史数据填满内存，并配置 WS 地址"""
        self.active_symbol = symbol
        huobi = HuobiClient(user_id)

        # --- [新增] 从数据库读取 Base URL 并转换为 WS 地址 ---
        # 获取用户配置的 REST URL (例如 https://your-worker.workers.dev)
        base_url = db.get_config(user_id, "huobi_api_url")
        if not base_url:
            base_url = "https://autohtx.top"

        # 去除末尾斜杠
        base_url = base_url.rstrip('/')

        # 协议转换: https -> wss, http -> ws
        if base_url.startswith("https://"):
            ws_base = base_url.replace("https://", "wss://", 1)
        elif base_url.startswith("http://"):
            ws_base = base_url.replace("http://", "ws://", 1)
        else:
            # 如果没写协议头，默认走 wss
            ws_base = f"wss://{base_url}"

        # 拼接线性合约 WS 路径
        # 注意：Cloudflare Worker 通常会透传路径，所以我们保留 /linear-swap-ws
        self.ws_url = f"{ws_base}/linear-swap-ws"

        print(f">>> [MarketManager] 初始化配置:")
        print(f"    - REST URL: {base_url}")
        print(f"    - WS URL  : {self.ws_url}")
        # -----------------------------------------------------

        async with self.lock:
            if symbol not in self.data_store:
                self.data_store[symbol] = {}

            print(f">>> [MarketManager] 正在初始化 {symbol} 历史数据 (via HTTP)...")

            period_map = {'15min': '15min', '60min': '1hour', '4hour': '4hour', '1day': '1day'}

            for ws_period, api_period in period_map.items():
                try:
                    df = await asyncio.to_thread(huobi.get_kline, symbol, api_period, size=200)
                    if not df.empty:
                        self.data_store[symbol][ws_period] = df
                        print(f"    - {ws_period} 加载完成: {len(df)} 条")
                    else:
                        print(f"    - {ws_period} 加载失败 (API返回空)")
                        self.data_store[symbol][ws_period] = pd.DataFrame()
                except Exception as e:
                    print(f"    - {ws_period} 初始化异常: {e}")
                    self.data_store[symbol][ws_period] = pd.DataFrame()

    async def start_ws_loop(self):
        """启动 WebSocket 维护循环"""
        if self.running:
            return
        self.running = True

        # 使用动态生成的 URL

        while self.running:
            url = self.ws_url

            print(f">>> [MarketManager] 准备连接 WS: {url}")
            try:
                # trust_env=True 读取系统代理
                # ssl=False 忽略证书验证 (这对 Cloudflare 也是安全的，因为中间链路加密)
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.ws_connect(url, ssl=False, timeout=10) as ws:
                        print(f">>> [MarketManager] WebSocket 已连接: {self.active_symbol}")

                        # 订阅所有周期的 K 线
                        for p in self.periods:
                            sub_msg = {
                                "sub": f"market.{self.active_symbol}.kline.{p}",
                                "id": f"id_{self.active_symbol}_{p}"
                            }
                            await ws.send_json(sub_msg)

                        async for msg in ws:
                            if not self.running:
                                break

                            if msg.type == aiohttp.WSMsgType.BINARY:
                                try:
                                    data_bytes = gzip.decompress(msg.data)
                                    data = json.loads(data_bytes.decode('utf-8'))

                                    if 'ping' in data:
                                        await ws.send_json({'pong': data['ping']})
                                        continue

                                    if 'ch' in data and 'tick' in data:
                                        await self._process_tick(data)

                                except Exception as e:
                                    print(f"WS Decode Error: {e}")

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print("WS Error, Reconnecting...")
                                break

            except Exception as e:
                print(f"WS Connection Failed ({url}): {e}, Retrying in 5s...")
                await asyncio.sleep(5)

    async def _process_tick(self, data):
        """处理单条推送数据并更新 DataFrame"""
        try:
            channel = data['ch']
            tick = data['tick']
            parts = channel.split('.')
            symbol = parts[1]
            period = parts[3]

            ts = pd.to_datetime(tick['id'], unit='s') + pd.Timedelta(hours=8)
            new_row = {
                'open': float(tick['open']),
                'high': float(tick['high']),
                'low': float(tick['low']),
                'close': float(tick['close']),
                'volume': float(tick['vol'])
            }

            async with self.lock:
                if symbol not in self.data_store:
                    self.data_store[symbol] = {}

                if period not in self.data_store[symbol]:
                    df = pd.DataFrame([new_row], index=[ts])
                    self.data_store[symbol][period] = df
                else:
                    df = self.data_store[symbol][period]
                    if not df.empty and df.index[-1] == ts:
                        df.iloc[-1] = pd.Series(new_row)
                    else:
                        new_df = pd.DataFrame([new_row], index=[ts])
                        df = pd.concat([df, new_df])

                    if len(df) > 200:
                        self.data_store[symbol][period] = df.tail(200)
                    else:
                        self.data_store[symbol][period] = df
        except Exception as e:
            pass  # 忽略单次解析错误

    def get_data(self, symbol, period):
        if period == '1hour': period = '60min'
        if symbol in self.data_store and period in self.data_store[symbol]:
            try:
                return self.data_store[symbol][period].copy()
            except:
                return pd.DataFrame()
        return pd.DataFrame()


market_manager = MarketDataManager()