import re
import requests
import json
from storage import db


class GeminiClient:
    def __init__(self, user_id):
        self.user_id = user_id
        # 默认配置
        self.default_api_url = "https://api.gemai.cc/v1/chat/completions"
        self.default_model = "[满血A]gemini-3-pro-preview"

    def _get_api_config(self, is_chat_mode=False):
        """获取 API 配置，处理聊天 AI 的 fallback 逻辑"""

        # 基础配置 (Analysis AI)
        base_api_url = db.get_config(self.user_id, "ai_api_url") or self.default_api_url
        base_model = db.get_config(self.user_id, "ai_model") or self.default_model

        if not is_chat_mode:
            return base_api_url, base_model

        # 聊天模式配置
        use_same = db.get_config(self.user_id, "use_same_ai") == "true"
        if use_same:
            return base_api_url, base_model

        # 独立的 Chat 配置
        chat_url = db.get_config(self.user_id, "chat_api_url")
        chat_model = db.get_config(self.user_id, "chat_model")

        return (chat_url if chat_url else base_api_url), (chat_model if chat_model else base_model)

    def _call_llm(self, api_url, model, system_prompt, user_text, images_base64=None):
        api_key = db.get_config(self.user_id, "gemini_key")
        if not api_key:
            raise ValueError("Gemini API Key 未设置")

        messages = [{"role": "system", "content": system_prompt}]
        user_content = [{"type": "text", "text": user_text}]

        if images_base64:
            for img_b64 in images_base64:
                if img_b64:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                    })

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }

        response = requests.post(api_url, headers=headers, json=payload, timeout=360)
        if response.status_code != 200:
            raise Exception(f"AI API Error: {response.text}")

        result = response.json()
        return result['choices'][0]['message']['content']

    def extract_json_from_text(self, text):
        """
        从文本中提取 JSON 对象，支持 Markdown 代码块和原生文本中的嵌套 JSON
        """
        results = []

        # 策略 1: 优先尝试提取 Markdown 代码块 (```json ... ```)
        # 解释：这是最准确的，因为 AI 明确标记了这是代码
        code_block_pattern = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.S)
        matches = code_block_pattern.findall(text)
        if matches:
            for match in matches:
                try:
                    results.append(json.loads(match))
                except json.JSONDecodeError:
                    continue

        # 如果代码块提取成功，直接返回，避免重复
        if results:
            return results

        # 策略 2: 堆栈计数法（处理嵌套大括号的核心逻辑）
        # 解释：不依赖正则，而是通过数 { 和 } 的数量来确定一个完整的 JSON 对象在哪里结束
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(text):
            # 跳过非 { 字符，寻找下一个 JSON 对象的开始
            start_idx = text.find('{', pos)
            if start_idx == -1:
                break

            try:
                # raw_decode 会从 start_idx 开始解析，直到找到一个合法的完整 JSON 对象
                # 它会自动处理嵌套的大括号
                obj, end_idx = decoder.raw_decode(text, idx=start_idx)
                results.append(obj)
                pos = end_idx  # 移动指针到这个 JSON 后面，继续找下一个
            except json.JSONDecodeError:
                # 如果解析失败，说明这个 { 不是有效的 JSON 起始，指针后移一位继续找
                pos = start_idx + 1

        return results

    def get_analysis(self, system_prompt, user_text, images_base64):
        api_url, model = self._get_api_config(is_chat_mode=False)
        empty_as_none = db.get_config(self.user_id, "empty_as_none") == "true"

        try:
            content = self._call_llm(api_url, model, system_prompt, user_text, images_base64)

            if not content or not content.strip():
                if empty_as_none:
                    db.add_log(self.user_id, "AI_WARN", "AI返回内容为空，根据配置视为无操作")
                    return {"summary": "AI返回空，视为无操作", "do": []}
                else:
                    raise Exception("AI返回内容为空")

            # --- 修改开始：使用增强的提取逻辑 ---

            # 1. 尝试直接解析整个内容（防止 AI 只返回了纯 JSON）
            try:
                return json.loads(content)
            except:
                pass

            # 2. 使用提取函数查找内容中的所有 JSON 对象
            json_objects = self.extract_json_from_text(content)

            if json_objects:
                for obj in json_objects:
                    # 可以在这里加校验逻辑，确保是我们要的那个 JSON（例如必须包含 summary 字段）
                    if "summary" in obj and "do" in obj:
                        return obj

                # 如果提取到了 JSON 但没有关键字段，默认返回第一个
                return json_objects[0]

            # --- 修改结束 ---

            raise Exception("AI返回内容中无法提取有效JSON")

        except Exception as e:
            db.add_log(self.user_id, "AI_ERROR", str(e))
            raise e

    def _clean_thought_process(self, text):
        """清洗掉 <thought>...</thought> 之间的思考过程"""
        if not text:
            return ""
        # 使用非贪婪匹配移除 thought 标签及其内容，flags=re.S 让 . 匹配换行符
        cleaned = re.sub(r'<thought>.*?</thought>', '', text, flags=re.S)
        return cleaned.strip()

    def get_chat_response(self, system_prompt, user_text, images_base64):
        """聊天 AI 调用"""
        api_url, model = self._get_api_config(is_chat_mode=True)
        try:
            content = self._call_llm(api_url, model, system_prompt, user_text, images_base64)

            final_response = self._clean_thought_process(content)

            # 如果清洗后为空（防止AI只输出了思考），则降级返回原始内容
            if not final_response:
                return content

            return final_response
        except Exception as e:
            db.add_log(self.user_id, "CHAT_ERR", str(e))
            return f"聊天请求失败: {str(e)}"