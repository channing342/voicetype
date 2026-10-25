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

import macos
from core import (VERSION, ASR_CHOICES, ASR_NOTE_KEYS, CONFIG_PATH, DEFAULTS, MOD_FLAGS, MODE_NAMES,
                  STYLE_NAME_KEYS, UI_LANGS, apply_replacements, chat, find_misheard,
                  hf_cached, llm_refine, match_hotkey, mic_name, mic_options, resolve_lang,
                  save_config, snippet, suggest_corrections)
from i18n import get_lang, set_lang, t

UI_DIR = Path(__file__).parent / "ui"
TYPING_CPM = 40  # 估算省下時間用：一般人每分鐘約打 40 個中文字
PROPER_NOUN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*( [A-Za-z0-9][A-Za-z0-9.\-]*)*")  # 英文專有名詞：Redis、Direct Connect
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 token


class Api:
    """頁面可呼叫的方法（只開放 METHODS 內的名稱）。cfg 用 getter 取得，App 重新載入設定後仍指向最新的。"""

    METHODS = {"list", "get", "audio", "delete", "reprocess", "copy", "vocab", "vocab_add", "vocab_remove",
               "repl_set", "repl_remove", "settings", "asr", "asr_set", "home", "hotkey_capture",
               "hotkey_cancel", "hotkey_reset", "permissions_request", "open_privacy", "restart",
               "llm", "llm_save", "llm_use", "llm_delete", "llm_test", "theme_set", "history_days_set",
               "mics", "mic_set", "save_text", "corrections", "correction_apply", "correction_ignore",
               "corrections_add_all", "record_suggest", "record_apply", "ui_lang", "ui_lang_set",
               "memories", "memory_set", "memory_delete", "memory_export", "memory_import", "memory_enabled_set"}

    def __init__(self, history, get_cfg, config_path=CONFIG_PATH, copy=None, on_asr_change=None, asr_status=None,
                 system=None, llm_store=None, memory=None):
        """on_asr_change()：設定改了之後通知 App 到 worker 載入新模型；asr_status() → {"loading", "error"}。
        system：App 本身，提供快捷鍵錄製、權限、重新啟動（見 voicetype.App 的「管理介面」區段）。
        llm_store：llmstore.LLMStore，多組 LLM 設定；使用中那組改了要同步到 cfg["llm"]。
        memory：memory.MemoryStore，手動修正學到的記憶。"""
        self.h, self.get_cfg, self.config_path, self._copy = history, get_cfg, config_path, copy
        self.memory = memory
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
        """紀錄加上 memories_used：整理時參考了哪些記憶（顯示在「資訊」）。"""
        r = self.h.get(int(rid))
        if r and self.memory:
            r["memories_used"] = [{"id": m["id"], "src": m["src"], "dst": m["dst"], "status": m["status"]}
                                  for m in self.memory.by_ids(r["memories"])]
        return r

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
            raise ValueError(t("err_edit_no_selection"))
        cfg = self.get_cfg()
        ctx = {k: r[k] for k in ("app", "bundle", "title", "selection") if r.get(k)}
        mems = self.memory.pick(r["raw"]) if self.memory and cfg["memory_enabled"] else []
        text, ok = llm_refine(r["raw"], mode, cfg, {**ctx, "memories": mems})
        if not ok:
            raise RuntimeError(t("err_dgx_no_response"))
        self.h.update(int(rid), text=apply_replacements(text, cfg), mode=mode, ok=1, ai_text=None, edited=0,
                      memories=[m["id"] for m in mems])
        return self.get(rid)

    def save_text(self, rid, text, _learned=None):
        """手動修改「AI 整理後」：保留修改前的 DGX 輸出（ai_text），並把這次改的地方學成記憶。
        比對的是「這次儲存前 → 儲存後」，同一筆分幾次改不會重複計算。回傳紀錄加上 learned（這次學到的記憶）。"""
        rid, r = int(rid), self.h.get(int(rid))
        ai = r["ai_text"] if r["ai_text"] is not None else r["text"]  # 空字串也是有效的 DGX 輸出
        self.h.update(rid, text=text, ai_text=ai, edited=int(text != ai))
        if _learned is None:
            _learned = self.memory.learn_edit(r["text"], text, rid) if self.memory else []
        if self.system:
            self.system.text_edited(rid, text)  # 「重新貼上上一次結果」要用改過的版本
        return {**self.get(rid), "learned": [self._memory_out(m) for m in _learned]}

    def record_suggest(self, rid):
        r = self.h.get(int(rid))
        return find_misheard(r["text"], self.get_cfg(), r["raw"])

    def record_apply(self, rid, original, suggestion, add_rule=False):
        r = self.h.get(int(rid))
        if original not in r["text"]:
            raise ValueError(t("err_record_text_changed"))
        if add_rule:
            self.repl_set(original, suggestion)  # 1 個字的規則會在這裡擋下，紀錄也不會被改
        # 使用者確認過是聽錯，直接學起來，不經過 correction_pairs 的判斷
        learned = []
        if self.memory:
            m = self.memory.learn(original, suggestion, snippet(r["text"].replace(original, suggestion, 1), suggestion), int(rid))
            if add_rule:
                self.memory.set_status(m["id"], "repl")
            learned = [self.memory.get(m["id"])]
        return self.save_text(rid, r["text"].replace(original, suggestion, 1), _learned=learned)

    def corrections(self):
        return suggest_corrections(self.h.list(limit=500), self.get_cfg())

    def correction_apply(self, src, dst, kind):
        """kind：repl（加入替換規則）或 vocab（把修正後的詞加入詞彙）。"""
        if kind == "repl":
            self.repl_set(src, dst)
        else:
            self.vocab_add(dst)
        return {**self.vocab(), "corrections": self.corrections()}

    def correction_ignore(self, src, dst):
        cfg, key = self.get_cfg(), f"{src}→{dst}"
        if key not in cfg["ignored_corrections"]:
            cfg["ignored_corrections"] = [*cfg["ignored_corrections"], key]
            self._save()
        return self.corrections()

    def corrections_add_all(self):
        """出現 2 次以上的建議全部加成替換規則；1 個字的由 repl_set 擋下、略過。"""
        added = []
        for c in self.corrections():
            if c["count"] >= 2 and len(c["src"].strip()) >= 2:
                self.repl_set(c["src"], c["dst"])
                added.append(c)
        return {**self.vocab(), "corrections": self.corrections(), "added": len(added)}

    # ---------- 記憶 ----------
    def _memory_out(self, m):
        """加上升級建議：出現 2 次以上建議設為替換規則；正確寫法是英文專有名詞建議加入詞彙（只標示，不自動升級）。"""
        cfg = self.get_cfg()
        auto = m["status"] == "auto"
        return {**m,
                "suggest_repl": auto and m["count"] >= 2 and len(m["src"].strip()) >= 2 and m["src"] not in cfg["replacements"],
                "suggest_vocab": auto and bool(PROPER_NOUN.fullmatch(m["dst"]))
                                 and m["dst"].lower() not in {v.lower() for v in cfg["vocabulary"]}}

    def memories(self, query="", status=""):
        items = self.memory.list(query, status)
        return {"items": [self._memory_out(m) for m in items], "enabled": self.get_cfg()["memory_enabled"]}

    def memory_set(self, mid, action):
        """action：repl（設為替換規則）、vocab（加入詞彙）、off（不是聽錯／停用）、on（重新啟用）。"""
        m = self.memory.get(int(mid))
        if not m:
            raise ValueError(t("err_memory_not_found"))
        if action == "repl":
            self.repl_set(m["src"], m["dst"])  # 1 個字的會在這裡擋下
        elif action == "vocab":
            self.vocab_add(m["dst"])
        elif action not in ("off", "on"):
            raise ValueError(action)
        if m["status"] == "repl" and action != "repl":
            self._drop_rule(m)
        self.memory.set_status(m["id"], "auto" if action == "on" else action)
        return {**self.vocab(), "memory": self._memory_out(self.memory.get(m["id"]))}

    def memory_delete(self, mid):
        m = self.memory.get(int(mid))
        if m:
            if m["status"] == "repl":
                self._drop_rule(m)
            self.memory.delete(m["id"])
        return True

    def _drop_rule(self, m):
        """記憶原本是替換規則、改成其他狀態或刪除時，一起拿掉那條規則（規則被改成別的就不動）。"""
        if self.get_cfg()["replacements"].get(m["src"]) == m["dst"]:
            self.repl_remove(m["src"])

    def memory_export(self):
        """存到「下載項目」並在 Finder 標示出來；回傳檔案路徑。"""
        path = Path.home() / "Downloads" / time.strftime("voicetype-memory-%Y%m%d-%H%M%S.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "memories": self.memory.export()}, ensure_ascii=False, indent=2))
        if self.system:
            self.system.reveal(path)
        return str(path)

    def memory_import(self, path=None):
        """沒給路徑時跳出選檔視窗（取消回傳 None）。接受匯出的格式或直接一個陣列。"""
        path = path or (self.system.choose_file() if self.system else None)
        if not path:
            return None
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            raise ValueError(t("err_memory_import_invalid"))
        items = data.get("memories") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError(t("err_memory_import_invalid"))
        return {"added": self.memory.import_([it for it in items if isinstance(it, dict)])}

    def memory_enabled_set(self, enabled):
        self.get_cfg()["memory_enabled"] = bool(enabled)
        self._save()
        return self.settings()

    def mics(self):
        cfg = self.get_cfg()
        return {"current": cfg["mic"], "options": mic_options(macos.input_devices(), cfg["mic"], cfg["mic_name"])}

    def mic_set(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError(t("err_unknown_mic"))
        cfg = self.get_cfg()
        name = mic_name(macos.input_devices(), value) or (cfg["mic_name"] if value == cfg["mic"] else "")
        cfg["mic"], cfg["mic_name"] = value, name
        self._save()
        if self.system:
            self.system.config_changed()  # 選單列的「麥克風」勾選要跟著變
        return self.mics()

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

    def repl_set(self, src, dst, force=False):
        src = src.strip()
        # 手動新增時，頁面收到這個錯誤會讓使用者再按一次確認（force=True）；聽錯的詞建議則一律不允許 1 個字
        if len(src) == 1 and not force:
            raise ValueError(t("err_single_char_replacement"))
        if src:
            self.get_cfg()["replacements"][src] = dst.strip()
            self._save()
        return self.vocab()

    def repl_remove(self, src):
        self.get_cfg()["replacements"].pop(src, None)
        self._save()
        return self.vocab()

    def settings(self):
        cfg = self.get_cfg()
        return {"history_days": cfg["history_days"], "memory_enabled": cfg["memory_enabled"]}

    def history_days_set(self, days):
        days = int(float(days))
        if not 1 <= days <= 3650:
            raise ValueError(t("err_history_days_range"))
        self.get_cfg()["history_days"] = days
        self._save()
        if self.system:
            self.system.purge_soon()
        return self.settings()

    def asr(self):
        current = self.get_cfg()["asr_model"]
        choices = [{**c, "note": t(ASR_NOTE_KEYS[c["id"]]) if c["id"] in ASR_NOTE_KEYS else c["note"],
                    "downloaded": hf_cached(c["id"])} for c in ASR_CHOICES]
        if current not in {c["id"] for c in ASR_CHOICES}:
            choices.append({"id": current, "name": current, "size": "", "note": t("asr_custom_model"),
                            "downloaded": hf_cached(current)})
        return {"current": current, "choices": choices, **self.asr_status()}

    def asr_set(self, repo):
        if repo not in {c["id"] for c in self.asr()["choices"]}:
            raise ValueError(t("err_unknown_model", repo=repo))
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
            raise ValueError(t("err_llm_name_empty"))
        if check_name and any(p["name"] == name and p["id"] != form.get("id") for p in self.llm_store.list()):
            raise ValueError(t("err_llm_name_duplicate", name=name))
        if not url.startswith(("http://", "https://")):
            raise ValueError(t("err_llm_url_protocol"))
        if not model:
            raise ValueError(t("err_llm_model_empty"))
        if not 1 <= timeout <= 120:
            raise ValueError(t("err_llm_timeout_range"))
        hdrs = {}
        for hname, value in form.get("headers") or []:  # 兩格都空的列略過
            hname, value = hname.strip(), value.strip()
            if not hname and not value:
                continue
            if not HEADER_NAME.fullmatch(hname):
                raise ValueError(t("err_llm_header_invalid", name=hname or t("empty_value")))
            if hname.lower() in {k.lower() for k in hdrs}:
                raise ValueError(t("err_llm_header_duplicate", name=hname))
            if any(c in value for c in "\r\n"):
                raise ValueError(t("err_llm_header_newline", name=hname))
            hdrs[hname] = value
        return {"id": form.get("id") or None, "name": name, "url": url, "model": model,
                "timeout": int(timeout) if timeout.is_integer() else timeout, "headers": hdrs}

    def _llm_find(self, pid):
        p = self.llm_store.get(pid)
        if not p:
            raise ValueError(t("err_llm_not_found"))
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
            raise ValueError(t("err_llm_cannot_delete_active"))
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
        return {"hotkeys": cfg["hotkeys"], "defaults": DEFAULTS["hotkeys"], "modes": dict(MODE_NAMES),
                "styles": [{"name": t(STYLE_NAME_KEYS[r["name"]]) if r["name"] in STYLE_NAME_KEYS else r["name"],
                            "style": r["style"]} for r in cfg["app_styles"]],
                "stats": {**st, "cpm": round(cpm), "saved": max(0, st["chars"] / TYPING_CPM * 60 - st["seconds"]),
                          "ok_rate": round(st["ok"] / st["count"] * 100) if st["count"] else None},
                "asr": {"name": cur, **self.asr_status()}, "history_days": cfg["history_days"], "theme": cfg["theme"], "version": VERSION,
                **self.system.status()}

    def theme_set(self, theme):
        if theme not in ("system", "light", "dark"):
            raise ValueError(t("err_unknown_theme", theme=theme))
        self.get_cfg()["theme"] = theme
        self._save()
        return theme

    def ui_lang(self):
        cfg = self.get_cfg()
        options = [{"id": "auto", "name": t("lang_auto")}] + [{"id": code, "name": native} for code, native in UI_LANGS]
        return {"setting": cfg.get("ui_lang", "auto"), "lang": get_lang(), "options": options}

    def ui_lang_set(self, value):
        valid = {"auto", *(code for code, _ in UI_LANGS)}
        if value not in valid:
            raise ValueError(t("err_unknown_lang", lang=value))
        cfg = self.get_cfg()
        cfg["ui_lang"] = value
        self._save()
        set_lang(resolve_lang(value, macos.system_language()))
        if self.system:
            self.system.config_changed()
        return self.ui_lang()

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
            raise ValueError(t("err_hotkey_used", label=hk['label'], mode=MODE_NAMES[other]))
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
            return {"ok": False, "error": str(e)}


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
