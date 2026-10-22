"""歷史紀錄／詞彙管理視窗：WKWebView 載入 ui/index.html，JS 透過 messageHandlers.vt 呼叫 Api。"""
import base64
import json
import threading
from pathlib import Path

import objc
from AppKit import NSApplication, NSBackingStoreBuffered, NSMakeRect, NSObject, NSURL, NSWindow
from PyObjCTools import AppHelper
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

from core import ASR_CHOICES, CONFIG_PATH, apply_replacements, hf_cached, llm_refine, save_config, suggest_vocabulary

UI_DIR = Path(__file__).parent / "ui"


class Api:
    """頁面可呼叫的方法（只開放 METHODS 內的名稱）。cfg 用 getter 取得，App 重新載入設定後仍指向最新的。"""

    METHODS = {"list", "get", "audio", "delete", "reprocess", "copy", "vocab", "vocab_add", "vocab_remove",
               "repl_set", "repl_remove", "vocab_suggest", "settings", "asr", "asr_set"}

    def __init__(self, history, get_cfg, config_path=CONFIG_PATH, copy=None, on_asr_change=None, asr_status=None):
        """on_asr_change()：設定改了之後通知 App 到 worker 載入新模型；asr_status() → {"loading", "error"}。"""
        self.h, self.get_cfg, self.config_path, self._copy = history, get_cfg, config_path, copy
        self.on_asr_change = on_asr_change
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
        rect = NSMakeRect(0, 0, 1080, 720)
        self.webview = WKWebView.alloc().initWithFrame_configuration_(rect, conf)
        self.bridge.webview = self.webview
        index = NSURL.URLWithString_(NSURL.fileURLWithPath_(str(UI_DIR / "index.html")).absoluteString() + "#" + tab)
        self.webview.loadFileURL_allowingReadAccessToURL_(index, NSURL.fileURLWithPath_(str(UI_DIR)))
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, 1 | 2 | 4 | 8, NSBackingStoreBuffered, False)  # titled | closable | miniaturizable | resizable
        self.window.setTitle_("Voicetype")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_((760, 480))
        self.window.setContentView_(self.webview)
        self.window.center()

    def show(self, tab="history"):
        if self.window is None:
            self._build(tab)
        else:
            self.js(f"showTab('{tab}')")
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def js(self, code):
        """可從任何執行緒呼叫。"""
        if self.window is not None and self.window.isVisible():
            AppHelper.callAfter(self.webview.evaluateJavaScript_completionHandler_, code, None)
