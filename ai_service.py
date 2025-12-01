import re
import requests
import json
from storage import db


class GeminiClient:
    def __init__(self, user_id):
        self.user_id = user_id
        self.api_url = "https://api.gemai.cc/v1/chat/completions"
        self.model = "[满血A]gemini-3-pro-preview"

    def get_analysis(self, system_prompt, user_text, images_base64):
        api_key = db.get_config(self.user_id, "gemini_key")
        empty_as_none = db.get_config(self.user_id, "empty_as_none") == "true"

        if not api_key:
            raise ValueError("Gemini API Key 未设置")

        messages = [{"role": "system", "content": system_prompt}]
        user_content = [{"type": "text", "text": user_text}]
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

            if not content or not content.strip():
                if empty_as_none:
                    db.add_log(self.user_id, "AI_WARN", "AI返回内容为空，根据配置视为无操作")
                    return {"summary": "AI返回空，视为无操作", "do": []}
                else:
                    raise Exception("AI返回内容为空")

            blocks = re.findall(r'\{.*?}', content, re.S)
            if blocks:
                for block in blocks:
                    try:
                        obj = json.loads(block)
                        if "summary" in obj:
                            content = block
                            break
                    except:
                        continue

            return json.loads(content)
        except Exception as e:
            db.add_log(self.user_id, "AI_ERROR", str(e))
            raise e