import re

import requests
import json
from storage import db


class GeminiClient:
    def __init__(self):
        self.api_url = "https://api.gemai.cc/v1/chat/completions"
        self.model = "[满血A]gemini-3-pro-preview-thinking"  # 保持与原文件一致的模型名

    def get_analysis(self, prompt_text, images_base64):
        api_key = db.get_config("gemini_key")
        if not api_key:
            raise ValueError("Gemini API Key 未设置")

        content_parts = [{"type": "text", "text": prompt_text}]

        # 添加图片
        for img_b64 in images_base64:
            if img_b64:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                })

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": content_parts}
            ],
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
            print("Gemini API Response:", result)
            content = result['choices'][0]['message']['content']

            # 清洗 markdown json
            # content = content.replace('```json', '').replace('```', '').strip()
            blocks = re.findall(r'\{.*?}', content, re.S)

            for block in blocks:
                try:
                    obj = json.loads(block)
                    if "summary" in obj:
                        content = block
                        print("Gemini API Response:", obj)
                        break
                except:
                    pass

            return json.loads(content)
        except Exception as e:
            db.add_log("AI_ERROR", str(e))
            raise e