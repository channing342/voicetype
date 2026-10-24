"""歷史紀錄：SQLite 存文字與中繼資料，錄音另存 16-bit WAV。"""
import json
import sqlite3
import time
import wave
from pathlib import Path

import numpy as np

from core import SAMPLE_RATE

DATA_DIR = Path.home() / ".local/share/voicetype"

FIELDS = ("created", "mode", "app", "bundle", "title", "duration", "raw", "text", "segments",
          "ok", "t_transcribe", "t_llm", "model", "selection", "ai_text", "edited")
# 後來加的欄位：舊資料庫啟動時補上。ai_text＝使用者手動修改前的 DGX 輸出；edited＝是否手動改過
ADDED = {"ai_text": "TEXT", "edited": "INTEGER DEFAULT 0"}


class History:
    def __init__(self, root=DATA_DIR):
        self.root, self.audio_dir = Path(root), Path(root) / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "history.db"
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS recordings (id INTEGER PRIMARY KEY, created REAL, mode TEXT,"
                      " app TEXT, bundle TEXT, title TEXT, duration REAL, raw TEXT, text TEXT, segments TEXT,"
                      " ok INTEGER, t_transcribe REAL, t_llm REAL, model TEXT, selection TEXT)")
            have = {r[1] for r in c.execute("PRAGMA table_info(recordings)")}
            for col, typ in ADDED.items():
                if col not in have:
                    c.execute(f"ALTER TABLE recordings ADD COLUMN {col} {typ}")

    def _conn(self):
        # ponytail: 每次開新連線，避免跨執行緒共用；寫入量極小不需要連線池
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def wav_path(self, rid):
        return self.audio_dir / f"{rid}.wav"

    def add(self, audio, **rec):
        rec = {"created": time.time(), **rec, "segments": json.dumps(rec.get("segments", []), ensure_ascii=False)}
        cols = [f for f in FIELDS if f in rec]
        with self._conn() as c:
            rid = c.execute(f"INSERT INTO recordings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [rec[f] for f in cols]).lastrowid
        with wave.open(str(self.wav_path(rid)), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        return rid

    def _row(self, r):
        d = dict(r)
        d["segments"] = json.loads(d["segments"] or "[]")
        d["ok"], d["edited"] = bool(d["ok"]), bool(d["edited"])
        return d

    def list(self, query="", limit=200):
        sql = "SELECT * FROM recordings"
        args = []
        if query:
            sql += " WHERE text LIKE ? OR raw LIKE ?"
            args = [f"%{query}%"] * 2
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql + " ORDER BY created DESC LIMIT ?", [*args, limit])]

    def get(self, rid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM recordings WHERE id = ?", [rid]).fetchone()
        return self._row(r) if r else None

    def update(self, rid, **fields):
        cols = [f for f in fields if f in FIELDS]
        with self._conn() as c:
            c.execute(f"UPDATE recordings SET {','.join(f + '=?' for f in cols)} WHERE id = ?",
                      [*[fields[f] for f in cols], rid])

    def delete(self, rid):
        with self._conn() as c:
            c.execute("DELETE FROM recordings WHERE id = ?", [rid])
        self.wav_path(rid).unlink(missing_ok=True)

    def stats(self):
        """首頁統計：筆數、錄音總秒數、整理後字數（不含空白）、DGX 成功筆數。"""
        with self._conn() as c:
            n, secs, ok = c.execute("SELECT COUNT(*), COALESCE(SUM(duration), 0), COALESCE(SUM(ok), 0)"
                                    " FROM recordings").fetchone()
            chars = sum(len("".join((t or "").split())) for (t,) in c.execute("SELECT text FROM recordings"))
        return {"count": n, "seconds": secs, "chars": chars, "ok": ok}

    def purge(self, days):
        cutoff = time.time() - days * 86400
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM recordings WHERE created < ?", [cutoff])]
        for rid in ids:
            self.delete(rid)
        return len(ids)
