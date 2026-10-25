"""記憶：使用者手動修正學到的「聽錯 → 正確」，存在 history.db，整理時挑相關的送給 DGX 參考。
判斷哪些修正算聽錯、挑哪些送出的純邏輯在 core（correction_pairs、pick_memories）。"""
import json
import sqlite3
import time
from pathlib import Path

from core import correction_pairs, diff_pairs, pick_memories
from history import DATA_DIR

# auto＝自動學到（只當 DGX 參考）；repl／vocab＝已設為替換規則／加入詞彙；off＝停用（不送給 DGX）
STATUSES = ("auto", "repl", "vocab", "off")
FIELDS = ("id", "src", "dst", "example", "count", "records", "status", "created", "last_used")


class MemoryStore:
    def __init__(self, db=DATA_DIR / "history.db"):
        self.db = Path(db)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, src TEXT, dst TEXT, example TEXT,"
                      " count INTEGER DEFAULT 0, records TEXT DEFAULT '[]', status TEXT DEFAULT 'auto',"
                      " created REAL, last_used REAL, UNIQUE(src, dst))")
            c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def _conn(self):
        # ponytail: 跟 History 一樣每次開新連線，避免跨執行緒共用
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def _row(self, r):
        d = dict(r)
        d["records"] = json.loads(d["records"] or "[]")
        return d

    def learn(self, src, dst, example="", rid=None):
        """學一次：已有就次數 +1。被扣到 0 而停用的會重新啟用；使用者按「不是聽錯」停用的維持停用。回傳記憶。"""
        with self._conn() as c:
            r = c.execute("SELECT * FROM memories WHERE src = ? AND dst = ?", [src, dst]).fetchone()
            if r is None:
                c.execute("INSERT INTO memories (src, dst, example, count, records, status, created)"
                          " VALUES (?, ?, ?, 1, ?, 'auto', ?)", [src, dst, example, json.dumps([rid] if rid else []), time.time()])
            else:
                records = json.loads(r["records"] or "[]")
                if rid and rid not in records:
                    records.append(rid)
                status = "auto" if r["status"] == "off" and r["count"] <= 0 else r["status"]
                c.execute("UPDATE memories SET count = count + 1, records = ?, status = ?, example = ? WHERE id = ?",
                          [json.dumps(records), status, example or r["example"], r["id"]])
            return self._row(c.execute("SELECT * FROM memories WHERE src = ? AND dst = ?", [src, dst]).fetchone())

    def learn_edit(self, before, after, rid=None):
        """一次手動修改（before → after）。回傳這次學到的記憶。
        - 改回既有記憶的（Redis → 雷特）扣次數、不反向學。改回去不一定通過聽錯的判斷（英文改回中文讀音比不出來），
          所以用沒過濾的 diff_pairs 找。
        - 修正自己上一次的修正（同一筆紀錄先學了「派 A → PAI」，又把 PAI 改成 Pi）：改掉原本那筆的目標，
          變成「派 A → Pi」，不另外學「PAI → Pi」（PAI 是使用者自己打錯，不是辨識出來的）。"""
        reverted, learned = set(), []
        for src, dst in diff_pairs(before, after):
            back = self.find(dst, src)
            if back and back["count"] > 0:
                self.unlearn(dst, src)
                reverted.add((src, dst))
        for src, dst, example in correction_pairs(before, after):
            if (src, dst) in reverted:
                continue
            prev = [m for m in self.list() if m["dst"] == src and rid in m["records"] and m["status"] != "off"] if rid else []
            if prev:
                for m in prev:
                    self._retarget(m, dst, example, rid)
                    learned.append(self.find(m["src"], dst))
            else:
                learned.append(self.learn(src, dst, example, rid))
        return learned

    def _retarget(self, m, dst, example, rid):
        """把 m 這次學到的那一次改算到 m["src"] → dst；m 只來自這一筆紀錄而且只算一次的就整筆拿掉。"""
        with self._conn() as c:
            if m["count"] <= 1 and m["records"] == [rid]:
                c.execute("DELETE FROM memories WHERE id = ?", [m["id"]])
            else:
                c.execute("UPDATE memories SET count = MAX(count - 1, 0) WHERE id = ?", [m["id"]])
        new = self.learn(m["src"], dst, example, rid)
        if m["status"] == "vocab" and new["status"] == "auto":
            self.set_status(new["id"], "vocab")

    def backfill(self, records):
        """第一次啟動新版時，從以前手動改過的紀錄（ai_text → text）補學一次。回傳學到幾筆。"""
        if self.meta("backfilled"):
            return 0
        n = sum(len(self.learn_edit(r["ai_text"], r["text"], r["id"]))
                for r in records if r.get("edited") and r.get("ai_text") is not None)
        self.meta("backfilled", "1")
        return n

    def find(self, src, dst):
        with self._conn() as c:
            r = c.execute("SELECT * FROM memories WHERE src = ? AND dst = ?", [src, dst]).fetchone()
        return self._row(r) if r else None

    def unlearn(self, src, dst):
        """使用者把修正改回去（dst → src）：次數 -1，扣到 0 就停用。沒有這筆記憶就不做事。"""
        with self._conn() as c:
            c.execute("UPDATE memories SET count = MAX(count - 1, 0) WHERE src = ? AND dst = ?", [src, dst])
            c.execute("UPDATE memories SET status = 'off' WHERE src = ? AND dst = ? AND count = 0", [src, dst])

    def get(self, mid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM memories WHERE id = ?", [mid]).fetchone()
        return self._row(r) if r else None

    def list(self, query="", status=""):
        sql, args = "SELECT * FROM memories WHERE 1", []
        if query:
            sql += " AND (src LIKE ? OR dst LIKE ? OR example LIKE ?)"
            args += [f"%{query}%"] * 3
        if status:
            sql += " AND status = ?"
            args.append(status)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql + " ORDER BY count DESC, id DESC", args)]

    def active(self):
        """可以送給 DGX 的記憶。替換規則在 DGX 之後一定會套用，不用再佔 prompt。"""
        return [m for m in self.list() if m["status"] in ("auto", "vocab")]

    def pick(self, raw):
        """這次整理要送給 DGX 的記憶，並記下用上的時間。"""
        picked = pick_memories(raw, self.active())
        self.touch([m["id"] for m in picked])
        return picked

    def by_ids(self, ids):
        if not ids:
            return []
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(ids))})", list(ids)).fetchall()
        found = {r["id"]: self._row(r) for r in rows}
        return [found[i] for i in ids if i in found]

    def set_status(self, mid, status):
        if status not in STATUSES:
            raise ValueError(status)
        with self._conn() as c:
            # 手動重新啟用時次數至少 1，否則下一次被改回去會立刻又停用
            c.execute("UPDATE memories SET status = ?, count = CASE WHEN ? != 'off' AND count < 1 THEN 1 ELSE count END"
                      " WHERE id = ?", [status, status, mid])

    def delete(self, mid):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE id = ?", [mid])

    def touch(self, ids):
        if ids:
            with self._conn() as c:
                c.execute(f"UPDATE memories SET last_used = ? WHERE id IN ({','.join('?' * len(ids))})", [time.time(), *ids])

    def export(self):
        return [{k: m[k] for k in FIELDS if k not in ("id", "records")} for m in self.list()]

    def import_(self, items):
        """合併匯入：同一組「聽錯 → 正確」取次數較大者，狀態以本機為準。回傳新增筆數。
        來源紀錄編號只在原本那台 Mac 有意義，不匯入。"""
        added = 0
        with self._conn() as c:
            for it in items:
                src, dst = str(it.get("src", "")).strip(), str(it.get("dst", "")).strip()
                if not src or not dst or src == dst:
                    continue
                status = it.get("status") if it.get("status") in STATUSES else "auto"
                count = max(int(it.get("count") or 1), 0)
                r = c.execute("SELECT * FROM memories WHERE src = ? AND dst = ?", [src, dst]).fetchone()
                if r is None:
                    c.execute("INSERT INTO memories (src, dst, example, count, records, status, created, last_used)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              [src, dst, str(it.get("example") or ""), count, "[]", status,
                               it.get("created") or time.time(), it.get("last_used")])
                    added += 1
                else:
                    c.execute("UPDATE memories SET count = MAX(count, ?) WHERE id = ?", [count, r["id"]])
        return added

    def meta(self, key, value=None):
        with self._conn() as c:
            if value is not None:
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [key, value])
                return value
            r = c.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return r[0] if r else None
