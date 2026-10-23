"""歷史紀錄／詞彙管理視窗：WKWebView 載入 ui/index.html，JS 透過 messageHandlers.vt 呼叫 Api。"""
import base64
import json
import re
import threading
from pathlib import Path

import objc
from AppKit import NSApplication, NSBackingStoreBuffered, NSMakeRect, NSObject, NSURL, NSWindow
from PyObjCTools import AppHelper
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

import time

from core import (ASR_CHOICES, CONFIG_PATH, DEFAULTS, MOD_FLAGS, MODE_NAMES, apply_replacements, chat, hf_cached,
                  llm_refine, match_hotkey, save_config, suggest_vocabulary)

UI_DIR = Path(__file__).parent / "ui"
TYPING_CPM = 40  # 估算省下時間用：一般人每分鐘約打 40 個中文字
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 token


class Api:
    """頁面可呼叫的方法（只開放 METHODS 內的名稱）。cfg 用 getter 取得，App 重新載入設定後仍指向最新的。"""

    METHODS = {"list", "get", "audio", "delete", "reprocess", "copy", "vocab", "vocab_add", "vocab_remove",
               "repl_set", "repl_remove", "vocab_suggest", "settings", "asr", "asr_set", "home", "hotkey_capture",
               "hotkey_cancel", "hotkey_reset", "permissions_request", "open_privacy", "restart",
               "llm", "llm_save", "llm_use", "llm_delete", "llm_test", "theme_set", "history_days_set"}

    def __init__(self, history, get_cfg, config_path=CONFIG_PATH, copy=None, on_asr_change=None, asr_status=None,
                 system=None, llm_store=None):
        """on_asr_change()：設定改了之後通知 App 到 worker 載入新模型；asr_status() → {"loading", "error"}。
        system：App 本身，提供快捷鍵錄製、權限、重新啟動（見 voicetype.App 的「管理介面」區段）。
        llm_store：llmstore.LLMStore，多組 LLM 設定；使用中那組改了要同步到 cfg["llm"]。"""
        self.h, self.get_cfg, self.config_path, self._copy = history, get_cfg, config_path, copy
        self.llm_store = llm_store
        self.on_asr_change, self.system = on_asr_change, system
        self.asr_status = asr_status or (lambda: {"loading": False, "error": ""})

    def _save(self):
        save_config(self.get_cfg(), self.config_path)

    def list(self, query=""):
        return [{"id": r["id"], "created": r["created"], "mode": r["mode"], "app": r["app"],
                 "duration": r["duration"], "preview": (r["text"] or r["raw"] or "")[:160]}
                for r in self.h.list(query)]

    def get(self, rid):
        return self.h.get(int(rid))

    def audio(self, rid):
        p = self.h.wav_path(int(rid))
        return base64.b64encode(p.read_bytes()).decode() if p.exists() else ""

    def delete(self, rid):
        self.h.delete(int(rid))
        return True

    def reprocess(self, rid, mode):
        """DGX 失敗時不覆蓋原本的結果（跟錄音當下的編輯模式一樣，失敗就不改）。"""
        r = self.h.get(int(rid))
        if mode == "edit" and not r.get("selection"):
            raise ValueError("這筆紀錄沒有選取的文字，不能用編輯模式重新處理")
        cfg = self.get_cfg()
        ctx = {k: r[k] for k in ("app", "bundle", "title", "selection") if r.get(k)}
        text, ok = llm_refine(r["raw"], mode, cfg, ctx)
        if not ok:
            raise RuntimeError("DGX 未回應，紀錄沒有修改")
        self.h.update(int(rid), text=apply_replacements(text, cfg), mode=mode, ok=1)
        return self.h.get(int(rid))

    def copy(self, text):
        if self._copy:
            self._copy(text)
        return True

    def vocab(self):
        cfg = self.get_cfg()
        return {"vocabulary": cfg["vocabulary"], "replacements": cfg["replacements"]}

    def vocab_add(self, words):
        cfg = self.get_cfg()
        for w in [words] if isinstance(words, str) else words:
            w = w.strip()
            if w and w.lower() not in {v.lower() for v in cfg["vocabulary"]}:
                cfg["vocabulary"].append(w)
        self._save()
        return self.vocab()

    def vocab_remove(self, word):
        cfg = self.get_cfg()
        cfg["vocabulary"] = [v for v in cfg["vocabulary"] if v != word]
        self._save()
        return self.vocab()

    def repl_set(self, src, dst):
        if src.strip():
            self.get_cfg()["replacements"][src.strip()] = dst.strip()
            self._save()
        return self.vocab()

    def repl_remove(self, src):
        self.get_cfg()["replacements"].pop(src, None)
        self._save()
        return self.vocab()

    def vocab_suggest(self):
        texts = [r["raw"] for r in self.h.list(limit=50) if r["raw"]]
        return suggest_vocabulary(texts, self.get_cfg()) if texts else []

    def settings(self):
        cfg = self.get_cfg()
        return {"history_days": cfg["history_days"]}

    def history_days_set(self, days):
        days = int(float(days))
        if not 1 <= days <= 3650:
            raise ValueError("保留天數要在 1 到 3650 之間")
        self.get_cfg()["history_days"] = days
        self._save()
        if self.system:
            self.system.purge_soon()
        return self.settings()

    def asr(self):
        current = self.get_cfg()["asr_model"]
        choices = [{**c, "downloaded": hf_cached(c["id"])} for c in ASR_CHOICES]
        if current not in {c["id"] for c in ASR_CHOICES}:
            choices.append({"id": current, "name": current, "size": "", "note": "設定檔自訂的模型",
                            "downloaded": hf_cached(current)})
        return {"current": current, "choices": choices, **self.asr_status()}

    def asr_set(self, repo):
        if repo not in {c["id"] for c in self.asr()["choices"]}:
            raise ValueError(f"未知的模型 {repo}")
        if repo != self.get_cfg()["asr_model"]:
            self.get_cfg()["asr_model"] = repo
            self._save()
            if self.on_asr_change:
                self.on_asr_change()
        return self.asr()

    def llm(self):
        """全部 LLM 設定（header 值解密後給頁面編輯）與使用中的 id。"""
        return {"active": self.llm_store.active()["id"], "profiles": self.llm_store.list()}

    def _llm_form(self, form, check_name=True):
        """驗證頁面表單 {id, name, url, model, timeout, headers: [[名稱, 值], ...]}，回傳 profile（headers 為 dict）。"""
        name, url, model = (str(form.get(k) or "").strip() for k in ("name", "url", "model"))
        timeout = float(form.get("timeout") or 0)
        if check_name and not name:
            raise ValueError("請填設定名稱")
        if check_name and any(p["name"] == name and p["id"] != form.get("id") for p in self.llm_store.list()):
            raise ValueError(f"已經有叫「{name}」的設定")
        if not url.startswith(("http://", "https://")):
            raise ValueError("網址要以 http:// 或 https:// 開頭")
        if not model:
            raise ValueError("請填模型名稱")
        if not 1 <= timeout <= 120:
            raise ValueError("逾時要在 1 到 120 秒之間")
        hdrs = {}
        for hname, value in form.get("headers") or []:  # 兩格都空的列略過
            hname, value = hname.strip(), value.strip()
            if not hname and not value:
                continue
            if not HEADER_NAME.fullmatch(hname):
                raise ValueError(f"Header 名稱不合法：{hname or '（空白）'}")
            if hname.lower() in {k.lower() for k in hdrs}:
                raise ValueError(f"Header 重複：{hname}")
            if any(c in value for c in "\r\n"):
                raise ValueError(f"Header 值不能換行：{hname}")
            hdrs[hname] = value
        return {"id": form.get("id") or None, "name": name, "url": url, "model": model,
                "timeout": int(timeout) if timeout.is_integer() else timeout, "headers": hdrs}

    def _llm_find(self, pid):
        p = self.llm_store.get(pid)
        if not p:
            raise ValueError("找不到這組設定，可能已經刪除")
        return p

    def _llm_changed(self):
        self.get_cfg()["llm"] = self.llm_store.active()
        if self.system:
            self.system.config_changed()  # 選單列的 LLM 設定與逾時勾選要跟著變

    def llm_save(self, form):
        """新增（沒有 id）或更新一組設定。回傳 llm() 加上 saved（該組 id）。"""
        profile = self._llm_form(form)
        if profile["id"]:
            self._llm_find(profile["id"])
        pid = self.llm_store.save(profile)
        self._llm_changed()
        return {**self.llm(), "saved": pid}

    def llm_use(self, pid):
        self.llm_store.use(self._llm_find(pid)["id"])
        self._llm_changed()
        return self.llm()

    def llm_delete(self, pid):
        p = self._llm_find(pid)
        if p["id"] == self.llm_store.active()["id"]:
            raise ValueError("使用中的設定不能刪除，先切換到其他組")
        self.llm_store.delete(p["id"])
        self._llm_changed()
        return self.llm()

    def llm_test(self, form):
        """用表單上（還沒儲存）的值送一句測試，回傳 {ms, reply}；失敗直接拋出例外給頁面顯示。"""
        profile = self._llm_form(form, check_name=False)
        t0 = time.time()
        reply = chat([{"role": "user", "content": "只回答「OK」兩個字母"}], self.get_cfg(), profile=profile)
        return {"ms": round((time.time() - t0) * 1000), "reply": reply[:200]}

    def home(self):
        cfg, st = self.get_cfg(), self.h.stats()
        cpm = st["chars"] / (st["seconds"] / 60) if st["seconds"] else 0
        cur = next((c["name"] for c in ASR_CHOICES if c["id"] == cfg["asr_model"]), cfg["asr_model"])
        return {"hotkeys": cfg["hotkeys"], "defaults": DEFAULTS["hotkeys"], "modes": MODE_NAMES,
                "styles": [{"name": r["name"], "style": r["style"]} for r in cfg["app_styles"]],
                "stats": {**st, "cpm": round(cpm), "saved": max(0, st["chars"] / TYPING_CPM * 60 - st["seconds"]),
                          "ok_rate": round(st["ok"] / st["count"] * 100) if st["count"] else None},
                "asr": {"name": cur, **self.asr_status()}, "history_days": cfg["history_days"], "theme": cfg["theme"],
                **self.system.status()}

    def theme_set(self, theme):
        if theme not in ("system", "light", "dark"):
            raise ValueError(f"未知的主題 {theme}")
        self.get_cfg()["theme"] = theme
        self._save()
        return theme

    def hotkey_capture(self, mode):
        self.system.capture(mode)
        return True

    def hotkey_cancel(self):
        self.system.capturing = None
        return True

    def hotkey_reset(self, mode):
        hotkeys, hk = self.get_cfg()["hotkeys"], DEFAULTS["hotkeys"][mode]
        flags = sum(MOD_FLAGS[m] for m in hk["mods"])
        other = match_hotkey({k: v for k, v in hotkeys.items() if k != mode}, hk["keycode"], flags)
        if other:
            raise ValueError(f"預設的 {hk['label']} 已被{MODE_NAMES[other]}使用")
        hotkeys[mode] = dict(hk)
        self._save()
        self.system.config_changed()
        return self.home()

    def permissions_request(self):
        self.system.request_permissions()
        return True

    def open_privacy(self, key):
        self.system.open_privacy(key)
        return True

    def restart(self):
        self.system.restart()
        return True

    def call(self, method, args):
        """回傳 {"ok": bool, "result"/"error"}；未開放的方法一律拒絕。"""
        if method not in self.METHODS:
            return {"ok": False, "error": f"unknown method {method}"}
        try:
            return {"ok": True, "result": getattr(self, method)(*args)}
        except Exception as e:
            return {"ok": False, "error": repr(e)}


class Bridge(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    def initWithApi_(self, api):
        self = objc.super(Bridge, self).init()
        self.api, self.webview = api, None
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        msg = json.loads(str(message.body()))
        # DGX 呼叫可能數秒，背景執行，完成後回主執行緒交還結果
        threading.Thread(target=self._run, args=(msg,), daemon=True).start()

    @objc.python_method
    def _run(self, msg):
        payload = self.api.call(msg["method"], msg.get("args", []))
        js = f"window.__resolve({int(msg['id'])}, {json.dumps(payload, ensure_ascii=False)})"
        AppHelper.callAfter(self.webview.evaluateJavaScript_completionHandler_, js, None)


class MainWindow:
    def __init__(self, api):
        self.bridge = Bridge.alloc().initWithApi_(api)
        self.window = None

    def _build(self, tab):
        conf = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        ucc.addScriptMessageHandler_name_(self.bridge, "vt")
        conf.setUserContentController_(ucc)
        rect = NSMakeRect(0, 0, 1180, 760)
        self.webview = WKWebView.alloc().initWithFrame_configuration_(rect, conf)
        self.bridge.webview = self.webview
        index = NSURL.URLWithString_(NSURL.fileURLWithPath_(str(UI_DIR / "index.html")).absoluteString() + "#" + tab)
        self.webview.loadFileURL_allowingReadAccessToURL_(index, NSURL.fileURLWithPath_(str(UI_DIR)))
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, 1 | 2 | 4 | 8, NSBackingStoreBuffered, False)  # titled | closable | miniaturizable | resizable
        self.window.setTitle_("Voicetype")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_((900, 560))
        self.window.setContentView_(self.webview)
        self.window.center()

    def show(self, tab="home"):
        if self.window is None:
            self._build(tab)
        else:
            if self.window.isMiniaturized():  # ⌘M 縮到 Dock 後，再按 ⌥⌘, 要叫回來
                self.window.deminiaturize_(None)
            self.js(f"showTab('{tab}')")
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def visible(self):
        return self.window is not None and self.window.isVisible()

    def js(self, code):
        """可從任何執行緒呼叫。"""
        if self.window is not None and self.window.isVisible():
            AppHelper.callAfter(self.webview.evaluateJavaScript_completionHandler_, code, None)
