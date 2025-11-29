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
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HuobiClient:
    def __init__(self):
        # 动态获取 API URL (用于发送请求)
        url = db.get_config("huobi_api_url")
        if not url:
            url = "https://api.hbdm.com"
        # 确保没有尾部斜杠
        self.base_url = url.rstrip('/')

    def _get_keys(self):
        access_key = db.get_config("access_key")
        secret_key = db.get_config("secret_key")
        if not access_key or not secret_key:
            raise ValueError("API Access Key 或 Secret Key 未配置，请先去配置页面填写。")
        return access_key, secret_key

    def _sign(self, method, path, params):
        access_key, secret_key = self._get_keys()
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

        params.update({
            "AccessKeyId": access_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": timestamp
        })

        sorted_params = sorted(params.items(), key=lambda d: d[0], reverse=False)
        query_string = urllib.parse.urlencode(sorted_params)

        # 【关键修正】
        # 签名时必须使用官方 Host "api.hbdm.com"，即使请求是发往反向代理的。
        # 如果这里使用 self.base_url 的域名，火币服务端会验签失败 (403)。
        host = "api.hbdm.com"

        payload = [method, host, path, query_string]
        payload_str = "\n".join(payload)

        signature = base64.b64encode(
            hmac.new(secret_key.encode('utf-8'), payload_str.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')

        params["Signature"] = signature
        return params

    def _request(self, method, path, params=None):
        if params is None:
            params = {}

        try:
            # 请求发往配置的 base_url (例如代理地址)
            full_url = f"{self.base_url}{path}"

            if method == "GET":
                params = self._sign("GET", path, params)
                resp = requests.get(full_url, params=params, timeout=10, verify=False)
            else:
                # POST 请求签名
                sign_params = self._sign("POST", path, {})
                # 将签名参数附加到 URL 上
                full_url = f"{full_url}?{urllib.parse.urlencode(sign_params)}"
                # Body 数据直接作为 json 发送
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
            db.add_log("ERROR", f"API Request Failed ({path}): {str(e)}")
            raise e

    def get_kline(self, symbol, period, size=200):
        req_period = period
        if period == '1hour': req_period = '60min'

        path = "/linear-swap-ex/market/history/kline"
        params = {"contract_code": symbol, "period": req_period, "size": size}

        try:
            # Kline 接口是公开的，通常不需要签名，直接发往 base_url
            resp = requests.get(self.base_url + path, params=params, verify=False, timeout=10)
            data = resp.json()
            if data.get('status') == 'ok':
                df = pd.DataFrame(data['data'])
                if not df.empty:
                    df['id'] = pd.to_datetime(df['id'], unit='s') + pd.Timedelta(hours=8)
                    df.set_index('id', inplace=True)
                    df.rename(columns={'vol': 'volume'}, inplace=True)
                    df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
                    return df.sort_index()
            return pd.DataFrame()
        except Exception as e:
            db.add_log("ERROR", f"Get Kline Failed: {str(e)}")
            return pd.DataFrame()

    def get_account_info(self, symbol):
        pos_path = "/linear-swap-api/v1/swap_cross_position_info"
        pos_res = self._request("POST", pos_path, {"contract_code": symbol})

        open_orders_path = "/linear-swap-api/v1/swap_cross_openorders"
        order_res = self._request("POST", open_orders_path, {"contract_code": symbol})

        return {
            "positions": pos_res.get("data", []),
            "orders": order_res.get("data", {}).get("orders", [])
        }

    def get_tpsl_openorders(self, symbol):
        path = "/linear-swap-api/v1/swap_cross_tpsl_openorders"
        return self._request("POST", path, {"contract_code": symbol})

    def get_market_detail(self, symbol):
        path = "/linear-swap-ex/market/detail/merged"
        # 同样使用配置的 base_url
        resp = requests.get(self.base_url + path, params={"contract_code": symbol}, verify=False, timeout=10)
        return resp.json()

    def place_cross_order(self, symbol, direction, offset, volume, price, take_profit, stop_loss, leverage=5):
        """
        下单 - 支持动态杠杆
        """
        path = "/linear-swap-api/v1/swap_cross_order"

        price_type = "limit"
        if not price or price == 0:
            price_type = "market"

        params = {
            "contract_code": symbol,
            "volume": int(volume),
            "direction": direction,
            "offset": offset,
            "lever_rate": int(leverage),
            "order_price_type": price_type,
        }

        if offset == 'open':
            # 只有开仓才带止盈止损
            if take_profit and take_profit > 0:
                params["tp_trigger_price"] = take_profit
                params["tp_order_price_type"] = "market"

            if stop_loss and stop_loss > 0:
                params["sl_trigger_price"] = stop_loss
                params["sl_order_price_type"] = "market"

        if price and price > 0:
            params["price"] = price

        return self._request("POST", path, params)

    def cancel_cross_order(self, symbol, order_id):
        path = "/linear-swap-api/v1/swap_cross_cancel"
        params = {
            "contract_code": symbol,
            "order_id": order_id
        }
        return self._request("POST", path, params)