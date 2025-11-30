import sqlite3
import os
import json
from datetime import datetime


class StorageManager:
    def __init__(self):
        # 确定存储路径: 用户目录/autoHTX
        self.base_dir = os.path.join(os.path.expanduser("~"), ".autoHTX")
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

        self.db_path = os.path.join(self.base_dir, "trade.db")
        self._init_db()
        self.log_callback = None  # 用于WebSocket广播的回调

    def set_log_callback(self, callback):
        """设置日志回调函数"""
        self.log_callback = callback

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # 创建配置表
        c.execute('''CREATE TABLE IF NOT EXISTS config
                     (
                         key
                         TEXT
                         PRIMARY
                         KEY,
                         value
                         TEXT
                     )''')
        # 创建日志表
        c.execute('''CREATE TABLE IF NOT EXISTS logs
                     (
                         id
                         INTEGER
                         PRIMARY
                         KEY
                         AUTOINCREMENT,
                         timestamp
                         TEXT,
                         level
                         TEXT,
                         message
                         TEXT
                     )''')
        conn.commit()
        conn.close()

    def set_config(self, key: str, value: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

    def get_config(self, key: str) -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key=?", (key,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else ""

    def add_log(self, level: str, message: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)", (timestamp, level, message))
        conn.commit()
        conn.close()

        log_entry = f"[{timestamp}] [{level}] {message}"
        print(log_entry)

        # 触发回调广播到 WebSocket
        if self.log_callback:
            try:
                self.log_callback(timestamp, level, message)
            except:
                pass

    def get_recent_logs(self, limit=100):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT timestamp, level, message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        # 返回正序以便阅读
        return list(reversed(rows))


# 单例模式
db = StorageManager()