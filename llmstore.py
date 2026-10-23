"""LLM 設定：多組設定存在 SQLite（與歷史紀錄同一個 history.db），header 值用 Fernet 加密。
金鑰放在設定目錄的獨立檔案（權限 600），跟資料庫分開：只外流資料庫（備份、同步、傳檔）時看不到 header 值。"""
import json
import os
import sqlite3
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from core import DEFAULT_LLM
from history import DATA_DIR

KEY_PATH = Path.home() / ".config/voicetype/secret.key"
# 舊版設定檔裡的 LLM 欄位：最早只有一組 llm_url…；鑰匙圈版是 llm_profiles + llm_profile（header 值在鑰匙圈，不搬）
LEGACY_KEYS = ("llm_url", "llm_model", "llm_timeout", "llm_headers", "llm_profiles", "llm_profile")


def load_key(path):
    """讀金鑰；不存在就產生一把，用 O_EXCL 建檔確保權限一開始就是 600。"""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # 另一個行程剛建好
        return path.read_bytes()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class LLMStore:
    def __init__(self, db=DATA_DIR / "history.db", key_path=KEY_PATH):
        self.db, self.fernet = Path(db), Fernet(load_key(Path(key_path)))
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS llm_profiles (id TEXT PRIMARY KEY, pos INTEGER, name TEXT UNIQUE,"
                      " url TEXT, model TEXT, timeout REAL, headers BLOB)")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        if not self.list():
            self.save(DEFAULT_LLM)
            self.use(DEFAULT_LLM["id"])

    def _conn(self):
        # ponytail: 跟 History 一樣每次開新連線，避免跨執行緒共用
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def _row(self, r):
        try:
            headers = json.loads(self.fernet.decrypt(r["headers"])) if r["headers"] else {}
        except InvalidToken:  # 金鑰檔被刪掉或換掉，舊的值解不開；當成沒有 header，讓使用者重新輸入
            print(f"LLM 設定「{r['name']}」的 header 解密失敗，請重新輸入", flush=True)
            headers = {}
        t = r["timeout"]
        return {"id": r["id"], "name": r["name"], "url": r["url"], "model": r["model"],
                "timeout": int(t) if float(t).is_integer() else t, "headers": headers}

    def list(self):
        with self._conn() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM llm_profiles ORDER BY pos")]

    def get(self, pid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM llm_profiles WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def active(self):
        """使用中的設定；記錄的 id 不存在時用第一組。"""
        with self._conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key = 'llm_profile'").fetchone()
        return (r and self.get(r[0])) or self.list()[0]

    def save(self, profile):
        """新增或更新（依 id）；header 整包加密後存。回傳 id。"""
        p = {**profile, "id": profile.get("id") or uuid.uuid4().hex[:8]}
        token = self.fernet.encrypt(json.dumps(p["headers"], ensure_ascii=False).encode()) if p["headers"] else None
        with self._conn() as c:
            c.execute("INSERT INTO llm_profiles (id, pos, name, url, model, timeout, headers)"
                      " VALUES (?, (SELECT COALESCE(MAX(pos), 0) + 1 FROM llm_profiles), ?, ?, ?, ?, ?)"
                      " ON CONFLICT(id) DO UPDATE SET name = excluded.name, url = excluded.url,"
                      " model = excluded.model, timeout = excluded.timeout, headers = excluded.headers",
                      (p["id"], p["name"], p["url"], p["model"], p["timeout"], token))
        return p["id"]

    def use(self, pid):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('llm_profile', ?)", (pid,))

    def delete(self, pid):
        with self._conn() as c:
            c.execute("DELETE FROM llm_profiles WHERE id = ?", (pid,))

    def import_legacy(self, cfg):
        """把設定檔裡舊版的 LLM 欄位搬進資料庫並從 cfg 拿掉。回傳是否有搬，有的話呼叫端要把 cfg 存回設定檔。"""
        old = {k: cfg.pop(k) for k in LEGACY_KEYS if k in cfg}
        if not old:
            return False
        if "llm_profiles" in old:
            for p in old["llm_profiles"]:
                self.save({**p, "headers": {}})
            active = old.get("llm_profile")
        else:
            p = {**DEFAULT_LLM, "url": old.get("llm_url", DEFAULT_LLM["url"]),
                 "model": old.get("llm_model", DEFAULT_LLM["model"]),
                 "timeout": old.get("llm_timeout", DEFAULT_LLM["timeout"]), "headers": old.get("llm_headers", {})}
            active = self.save(p)
        if active and self.get(active):
            self.use(active)
        return True
