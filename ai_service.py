import re
import requests
import json
from storage import db


class GeminiClient:
    def __init__(self):
        self.api_url = "https://api.gemai.cc/v1/chat/completions"
        self.model = "[满血A]gemini-3-pro-preview"

    def get_analysis(self, system_prompt, user_text, images_base64):
        """
        :param system_prompt: 核心指令、JSON格式要求、人设
        :param user_text: 市场数据、持仓信息
        :param images_base64: K线图
        """
        api_key = db.get_config("gemini_key")
        empty_as_none = db.get_config("empty_as_none") == "true"

        if not api_key:
            raise ValueError("Gemini API Key 未设置")

        # 构造消息体
        messages = [
            {"role": "system", "content": system_prompt}
        ]

        user_content = [{"type": "text", "text": user_text}]
        # 添加图片
        for img_b64 in images_base64:
            if img_b64:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                })

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        try:
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=120)
            if response.status_code != 200:
                raise Exception(f"AI API Error: {response.text}")

            result = response.json()
            content = result['choices'][0]['message']['content']

            # 处理空内容
            if not content or not content.strip():
                if empty_as_none:
                    db.add_log("AI_WARN", "AI返回内容为空，根据配置视为无操作")
                    return {"summary": "AI返回空，视为无操作", "do": []}
                else:
                    raise Exception("AI返回内容为空")

            # 清洗 markdown json
            blocks = re.findall(r'\{.*?}', content, re.S)

            # 尝试解析 JSON
            if blocks:
                for block in blocks:
                    try:
                        obj = json.loads(block)
                        if "summary" in obj:
                            content = obj
                            break
                    except:
                        continue

            return json.loads(content)
        except Exception as e:
            db.add_log("AI_ERROR", str(e))
            raise e