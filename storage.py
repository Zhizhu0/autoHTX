import json
import os
import sqlite3
from datetime import datetime

from cryptography.fernet import Fernet


class StorageManager:
    def __init__(self):
        # 确定存储路径: 用户目录/autoHTX
        self.base_dir = os.path.join(os.path.expanduser("~"), ".autoHTX")
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

        self.db_path = os.path.join(self.base_dir, "trade.db")
        self.key_path = os.path.join(self.base_dir, "server.key")
        self._init_db()
        self.log_callback = None

    def set_log_callback(self, callback):
        self.log_callback = callback

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # 用户表
        c.execute('''CREATE TABLE IF NOT EXISTS users
                     (
                         id
                         INTEGER
                         PRIMARY
                         KEY
                         AUTOINCREMENT,
                         username
                         TEXT
                         UNIQUE,
                         password_enc
                         TEXT
                     )''')

        # 已使用的注册码UUID表
        c.execute('''CREATE TABLE IF NOT EXISTS used_uuids
                     (
                         uuid
                         TEXT
                         PRIMARY
                         KEY,
                         used_at
                         TEXT
                     )''')

        # 配置表
        c.execute('''CREATE TABLE IF NOT EXISTS config
        (
            user_id
            INTEGER,
            key
            TEXT,
            value
            TEXT,
            PRIMARY
            KEY
                     (
            user_id,
            key
                     )
            )''')

        # 日志表
        c.execute('''CREATE TABLE IF NOT EXISTS logs
                     (
                         id
                         INTEGER
                         PRIMARY
                         KEY
                         AUTOINCREMENT,
                         user_id
                         INTEGER,
                         timestamp
                         TEXT,
                         level
                         TEXT,
                         message
                         TEXT
                     )''')
        # 创建索引以加速查询和删除
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_userid_id ON logs (user_id, id)")
        conn.commit()
        conn.close()

    # --- 密钥与安全相关 ---

    def generate_server_key(self):
        """生成新的服务器密钥，保存并清空所有用户数据"""
        key = Fernet.generate_key()
        with open(self.key_path, "wb") as f:
            f.write(key)

        # 清空用户数据，因为旧密码无法解密了
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM users")
        c.execute("DELETE FROM config")
        c.execute("DELETE FROM used_uuids")
        conn.commit()
        conn.close()
        return key

    def get_server_key(self):
        if not os.path.exists(self.key_path):
            raise Exception("Server Key not found. Please run with --generate_key first.")
        with open(self.key_path, "rb") as f:
            return f.read()

    def get_fernet(self):
        return Fernet(self.get_server_key())

    # --- 用户管理 ---

    def register_user(self, username, password, register_code_str):
        f = self.get_fernet()

        # 1. 解密注册码
        try:
            decrypted_data = f.decrypt(register_code_str.encode()).decode()
            data = json.loads(decrypted_data)
            reg_uuid = data.get("uuid")
            if not reg_uuid:
                raise Exception("Invalid Code Structure")
        except Exception:
            raise Exception("无效的注册码或注册码已过期")

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        try:
            # 2. 检查UUID是否已使用
            c.execute("SELECT uuid FROM used_uuids WHERE uuid=?", (reg_uuid,))
            if c.fetchone():
                raise Exception("注册码已被使用")

            # 3. 检查用户名
            c.execute("SELECT id FROM users WHERE username=?", (username,))
            if c.fetchone():
                raise Exception("用户名已存在")

            # 4. 加密密码 (Fernet加密结果每次不同，需解密对比)
            pwd_enc = f.encrypt(password.encode()).decode()

            # 5. 写入
            c.execute("INSERT INTO users (username, password_enc) VALUES (?, ?)", (username, pwd_enc))
            c.execute("INSERT INTO used_uuids (uuid, used_at) VALUES (?, ?)",
                      (reg_uuid, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def verify_user(self, username, password):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT id, password_enc FROM users WHERE username=?", (username,))
        row = c.fetchone()
        conn.close()

        if not row:
            return None

        user_id, pwd_enc = row
        f = self.get_fernet()
        try:
            # 解密数据库中的密码进行比对
            decrypted_pwd = f.decrypt(pwd_enc.encode()).decode()
            if decrypted_pwd == password:
                return user_id
        except:
            return None
        return None

    def get_all_active_users(self):
        """获取所有开启了自动交易的用户ID"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # 找出 config 中 is_active = true 的 user_id
        c.execute("SELECT DISTINCT user_id FROM config WHERE key='is_active' AND value='true'")
        rows = c.fetchall()
        conn.close()
        return [r[0] for r in rows]

    # --- 配置与日志 (需传入 user_id) ---

    def set_config(self, user_id, key: str, value: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config (user_id, key, value) VALUES (?, ?, ?)", (user_id, key, value))
        conn.commit()
        conn.close()

    def get_config(self, user_id, key: str) -> str:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE user_id=? AND key=?", (user_id, key))
        result = c.fetchone()
        conn.close()
        return result[0] if result else ""

    def add_log(self, user_id, level: str, message: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 插入新日志
        c.execute("INSERT INTO logs (user_id, timestamp, level, message) VALUES (?, ?, ?, ?)",
                  (user_id, timestamp, level, message))

        # 2. 自动清理：只保留该用户最近的100条日志
        # 逻辑：删除该用户 id 不在 (前100个倒序id) 之列的所有记录
        c.execute("""
                  DELETE
                  FROM logs
                  WHERE user_id = ?
                    AND id NOT IN (SELECT id
                                   FROM logs
                                   WHERE user_id = ?
                                   ORDER BY id DESC
                      LIMIT 100
                      )
                  """, (user_id, user_id))

        conn.commit()
        conn.close()

        # 触发回调广播到 WebSocket
        if self.log_callback:
            try:
                self.log_callback(user_id, timestamp, level, message)
            except:
                pass

    def get_recent_logs(self, user_id, limit=100):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT timestamp, level, message FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
                  (user_id, limit))
        rows = c.fetchall()
        conn.close()
        return list(reversed(rows))

    def get_context_logs(self, user_id, limit=10):
        """获取用于AI上下文的日志（SUMMARY, USER_INPUT, CHAT_AI）"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        # 修改点：增加了 CHAT_AI，limit 改为 10
        c.execute("""
            SELECT level, message FROM logs 
            WHERE user_id=? AND level IN ('SUMMARY', 'USER_INPUT', 'CHAT_AI') 
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit))
        rows = c.fetchall()
        conn.close()
        # 保持时间顺序：最旧的在前
        return list(reversed(rows))


# 单例
db = StorageManager()