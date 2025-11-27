import requests
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime
import pandas as pd
from storage import db
import urllib3

# --- 修复 SSL 报错的关键部分 ---
# 禁用 urllib3 的安全请求警告，防止控制台刷屏
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HuobiClient:
    def __init__(self):
        self.base_url = "https://api.hbdm.com"

    def _get_keys(self):
        access_key = db.get_config("access_key")
        secret_key = db.get_config("secret_key")
        # 简单检查 Key 是否为空，避免发无效请求
        if not access_key or not secret_key:
            # 这里抛出异常会被上层捕获并打印到日志
            raise ValueError("API Access Key 或 Secret Key 未配置，请先去配置页面填写。")
        return access_key, secret_key

    def _sign(self, method, path, params):
        access_key, secret_key = self._get_keys()
        # 使用 UTC 时间
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

        params.update({
            "AccessKeyId": access_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": timestamp
        })

        # 参数排序
        sorted_params = sorted(params.items(), key=lambda d: d[0], reverse=False)
        query_string = urllib.parse.urlencode(sorted_params)

        # 组装签名原文
        payload = [method, "api.hbdm.com", path, query_string]
        payload_str = "\n".join(payload)

        # HmacSHA256 签名
        signature = base64.b64encode(
            hmac.new(secret_key.encode('utf-8'), payload_str.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')

        params["Signature"] = signature
        return params

    def _request(self, method, path, params=None):
        if params is None:
            params = {}

        try:
            full_url = f"{self.base_url}{path}"
            if method == "GET":
                params = self._sign("GET", path, params)
                # verify=False 跳过 SSL 验证
                resp = requests.get(full_url, params=params, timeout=10, verify=False)
            else:
                # POST 请求：签名参数在 URL 中，业务参数在 Body 中
                sign_params = self._sign("POST", path, {})
                full_url = f"{full_url}?{urllib.parse.urlencode(sign_params)}"
                # verify=False 跳过 SSL 验证
                resp = requests.post(full_url, json=params, headers={'Content-Type': 'application/json'}, timeout=10,
                                     verify=False)

            if resp.status_code != 200:
                raise Exception(f"HTTP Error {resp.status_code}: {resp.text}")

            data = resp.json()
            if data.get("status") == "error":
                err_msg = data.get('err_msg', 'Unknown Error')
                err_code = data.get('err_code', '')
                raise Exception(f"Huobi API Error [{err_code}]: {err_msg}")
            return data
        except Exception as e:
            # 记录错误日志
            db.add_log("ERROR", f"API Request Failed ({path}): {str(e)}")
            raise e

    def get_kline(self, symbol, period, size=200):
        # 转换 period 格式
        req_period = period
        if period == '1hour': req_period = '60min'

        path = "/linear-swap-ex/market/history/kline"
        params = {"contract_code": symbol, "period": req_period, "size": size}

        try:
            # K线接口通常是公开的，不需要签名，但也加上 verify=False
            resp = requests.get(self.base_url + path, params=params, verify=False, timeout=10)
            data = resp.json()
            if data.get('status') == 'ok':
                df = pd.DataFrame(data['data'])
                # 简单的数据清洗
                if not df.empty:
                    df['id'] = pd.to_datetime(df['id'], unit='s') + pd.Timedelta(hours=8)  # 转为北京时间
                    df.set_index('id', inplace=True)
                    df.rename(columns={'vol': 'volume'}, inplace=True)
                    df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
                    return df.sort_index()
            return pd.DataFrame()
        except Exception as e:
            db.add_log("ERROR", f"Get Kline Failed: {str(e)}")
            return pd.DataFrame()

    def get_account_info(self, symbol):
        # 获取持仓信息
        pos_path = "/linear-swap-api/v1/swap_cross_position_info"
        pos_res = self._request("POST", pos_path, {"contract_code": symbol})

        # 获取当前挂单
        open_orders_path = "/linear-swap-api/v1/swap_cross_openorders"
        order_res = self._request("POST", open_orders_path, {"contract_code": symbol})

        return {
            "positions": pos_res.get("data", []),
            "orders": order_res.get("data", {}).get("orders", [])
        }

    def get_market_detail(self, symbol):
        path = "/linear-swap-ex/market/detail/merged"
        # 加上 verify=False
        resp = requests.get(self.base_url + path, params={"contract_code": symbol}, verify=False, timeout=10)
        return resp.json()

    def place_cross_order(self, symbol, direction, offset, volume, price, take_profit, stop_loss):
        """
        下单
        :param direction: 'buy' or 'sell'
        :param offset: 'open' or 'close'
        :param volume: 张数 (整数)
        :param price: 价格 (如果是对手价或市价，可为None)
        """
        print("下单参数:", locals())
        path = "/linear-swap-api/v1/swap_cross_order"

        # 价格类型转换逻辑
        price_type = "limit"
        if not price or price == 0:
            price_type = "market"

        params = {
            "contract_code": symbol,
            "volume": int(volume),
            "direction": direction,
            "offset": offset,
            "lever_rate": 200,
            "order_price_type": price_type,
            "tp_trigger_price": take_profit,
            "tp_order_price": take_profit,
            "sl_trigger_price": stop_loss,
            "sl_order_price": stop_loss
        }

        if price and price > 0:
            params["price"] = price

        return self._request("POST", path, params)

    def cancel_cross_order(self, symbol, order_id):
        """撤单"""
        path = "/linear-swap-api/v1/swap_cross_cancel"
        params = {
            "contract_code": symbol,
            "order_id": order_id
        }
        return self._request("POST", path, params)