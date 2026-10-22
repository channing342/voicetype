import json
import subprocess
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

import core
from history import History

DGX = core.DEFAULTS["llm_url"]


def cfg_with(**kw):
    return {**json.loads(json.dumps(core.DEFAULTS)), **kw}


def fake_llm(delay, reply):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            time.sleep(delay)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": reply}}]}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
    return srv


# ---------- 設定與快捷鍵 ----------

def test_load_config_creates_defaults_and_merges(tmp_path):
    p = tmp_path / "c.json"
    cfg = core.load_config(p)
    assert p.exists() and cfg["llm_timeout"] == 10 and cfg["history_days"] == 30
    p.write_text(json.dumps({"llm_timeout": 20, "prompts": {"clean": "X"}}))
    cfg = core.load_config(p)
    assert cfg["llm_timeout"] == 20
    assert cfg["prompts"]["clean"] == "X" and "edit" in cfg["prompts"]
    assert "hotkeys" in json.loads(p.read_text())  # 舊設定檔會補上新欄位


def test_load_config_migrates_whisper_model(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"whisper_model": "mlx-community/whisper-small"}))
    cfg = core.load_config(p)
    assert cfg["asr_model"] == "mlx-community/whisper-small" and "whisper_model" not in cfg


def test_to_traditional_keeps_taiwan_terms():
    out = core.to_traditional("我们现在有些权限要调整")
    assert out == "我們現在有些權限要調整"  # s2twp 會誤轉成「許可權」


def test_hf_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert not core.hf_cached("org/model")
    snap = tmp_path / "models--org--model/snapshots/abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    assert core.hf_cached("org/model")


def test_api_asr_switch(tmp_path):
    from window import Api
    cfg = cfg_with()
    called = []
    api = Api(History(tmp_path), lambda: cfg, config_path=tmp_path / "c.json", on_asr_change=lambda: called.append(1),
              asr_status=lambda: {"loading": True, "error": ""})
    a = api.call("asr", [])["result"]
    assert a["current"] == core.DEFAULTS["asr_model"] and a["loading"] and len(a["choices"]) == len(core.ASR_CHOICES)
    api.call("asr_set", ["mlx-community/Qwen3-ASR-0.6B-8bit"])
    assert cfg["asr_model"] == "mlx-community/Qwen3-ASR-0.6B-8bit" and called == [1]
    assert json.loads((tmp_path / "c.json").read_text())["asr_model"] == "mlx-community/Qwen3-ASR-0.6B-8bit"
    api.call("asr_set", ["mlx-community/Qwen3-ASR-0.6B-8bit"])
    assert called == [1]  # 同一個模型不重載
    assert api.call("asr_set", ["evil/model"])["ok"] is False
    cfg["asr_model"] = "someone/custom-asr"
    assert api.call("asr", [])["result"]["choices"][-1]["id"] == "someone/custom-asr"


def test_match_hotkey():
    hk = core.DEFAULTS["hotkeys"]
    cmd, shift = core.MOD_FLAGS["cmd"], core.MOD_FLAGS["shift"]
    assert core.match_hotkey(hk, 43, cmd) == "clean"
    assert core.match_hotkey(hk, 47, cmd | 0x100) == "organize"  # 非修飾鍵旗標不影響
    assert core.match_hotkey(hk, 41, cmd) == "edit"
    assert core.match_hotkey(hk, 43, cmd | core.MOD_FLAGS["alt"]) == "window"
    assert core.match_hotkey(hk, 43, cmd | shift) is None
    assert core.match_hotkey(hk, 43, 0) is None


def test_keylogic_tap_toggles():
    k = core.KeyLogic(0.4)
    assert k.down("clean", 43, 0.0, recording=False) == ("start", "clean")
    assert k.up(43, 0.1, recording=True, mode="clean") is None  # 短按：繼續錄
    assert k.down("organize", 47, 3.0, recording=True) == ("stop", "organize")  # 結束鍵決定模式
    assert k.up(47, 3.1, recording=False, mode="clean") is None


def test_keylogic_hold_to_talk():
    k = core.KeyLogic(0.4)
    k.down("clean", 43, 0.0, recording=False)
    assert k.up(43, 2.0, recording=True, mode="clean") == ("stop", "clean")
    assert k.up(43, 2.1, recording=False, mode="clean") is None  # 同一次放開不會重複觸發


def test_too_short_or_silent():
    cfg = core.DEFAULTS
    t = np.arange(core.SAMPLE_RATE) / core.SAMPLE_RATE
    tone = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert not core.is_too_short_or_silent(tone, cfg)
    assert core.is_too_short_or_silent(tone[: core.SAMPLE_RATE // 4], cfg)
    assert core.is_too_short_or_silent(np.zeros(core.SAMPLE_RATE, np.float32), cfg)


# ---------- 語氣規則、情境、替換 ----------

def test_match_style_by_bundle_glob_and_title():
    cfg = cfg_with()
    assert core.match_style(cfg, "com.tinyspeck.slackmacgap", "")["name"] == "聊天"
    assert core.match_style(cfg, "com.jetbrains.pycharm", "")["name"] == "終端機／IDE"
    assert core.match_style(cfg, "com.google.Chrome", "收件匣 - Gmail")["name"] == "郵件"
    assert core.match_style(cfg, "com.google.Chrome", "Google 搜尋") is None


def test_build_messages_includes_style_vocab_and_context():
    cfg = cfg_with(vocabulary=["Papaya-VPC"])
    sys_msg, user = core.build_messages("內容", "clean", cfg,
                                        {"app": "Slack", "bundle": "com.tinyspeck.slackmacgap", "title": "#infra",
                                         "selection": "不該出現"})
    assert "口語" in sys_msg["content"] and "Papaya-VPC" in sys_msg["content"]
    assert "目前 App：Slack" in sys_msg["content"] and "#infra" in sys_msg["content"]
    assert "不該出現" not in sys_msg["content"] + user["content"]  # 選取文字只給編輯模式
    assert core.SELF_CORRECTION in sys_msg["content"]
    assert user["content"] == "內容"


def test_build_messages_edit_mode():
    _, user = core.build_messages("改正式一點", "edit", cfg_with(), {"selection": "明天見啦"})
    assert "明天見啦" in user["content"] and "改正式一點" in user["content"]


def test_apply_replacements():
    cfg = cfg_with(replacements={"歐提": "OT", "": "x"})
    assert core.apply_replacements("歐提 帳號", cfg) == "OT 帳號"


# ---------- LLM ----------

def test_llm_refine_sends_params_and_returns_reply():
    srv = fake_llm(0, "整理後")
    cfg = cfg_with(llm_url=srv.url)
    assert core.llm_refine("原文", "organize", cfg) == ("整理後", True)
    assert srv.body["chat_template_kwargs"] == {"enable_thinking": False}
    assert srv.body["temperature"] == 0.1
    assert srv.body["messages"][0]["content"].startswith(core.DEFAULTS["prompts"]["organize"])
    srv.shutdown()


def test_llm_refine_falls_back_on_timeout_unreachable_and_empty():
    srv = fake_llm(2, "太慢")
    assert core.llm_refine("原文", "clean", cfg_with(llm_timeout=0.5, llm_url=srv.url)) == ("原文", False)
    srv.shutdown()
    assert core.llm_refine("原文", "clean", cfg_with(llm_url="http://127.0.0.1:1/x")) == ("原文", False)
    srv = fake_llm(0, "  ")
    assert core.llm_refine("原文", "clean", cfg_with(llm_url=srv.url)) == ("原文", False)
    srv.shutdown()


def test_suggest_vocabulary_parses_fenced_json_and_dedups():
    srv = fake_llm(0, '```json\n["Papaya-VPC", "terraform", "OT", "OT"]\n```')
    assert core.suggest_vocabulary(["..."], cfg_with(llm_url=srv.url)) == ["Papaya-VPC", "OT"]
    srv.shutdown()


def test_parse_capture():
    hk = core.DEFAULTS["hotkeys"]
    cmd = core.MOD_FLAGS["cmd"]
    assert core.parse_capture(hk, "clean", core.KEY_ESC, cmd, "") == (None, "")
    assert core.parse_capture(hk, "clean", 0, 0, "a")[0] is None  # 沒有修飾鍵
    new, msg = core.parse_capture(hk, "clean", 47, cmd, ".")  # 跟整理模式的 ⌘. 重複
    assert new is None and "整理" in msg
    assert core.parse_capture(hk, "clean", 43, cmd, ",")[0]["label"] == "⌘,"  # 重錄成自己原本的鍵可以
    new, msg = core.parse_capture(hk, "clean", 0, cmd | core.MOD_FLAGS["shift"], "a")
    assert new == {"keycode": 0, "mods": ["cmd", "shift"], "label": "⇧⌘A"} and "⇧⌘A" in msg


class FakeStream:
    """模擬 sounddevice.InputStream；開啟很慢，重現「Esc 比開麥克風還快」的競態。"""
    opened = []

    def __init__(self, **kw):
        time.sleep(0.2)
        self.closed = False
        FakeStream.opened.append(self)

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        self.closed = True


def test_recorder_stop_right_after_start_still_closes_mic(monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=FakeStream))
    FakeStream.opened = []
    r = core.Recorder()
    r.start()
    r.stop().result(timeout=2)  # 呼叫順序 start → stop，即使開麥克風很慢也要依序執行
    assert len(FakeStream.opened) == 1 and FakeStream.opened[0].closed and r.stream is None
    r.start()
    r.stop()
    r.start()
    r.stop().result(timeout=2)
    assert all(s.closed for s in FakeStream.opened) and len(FakeStream.opened) == 3


# ---------- 歷史紀錄 ----------

def test_history_stats(tmp_path):
    h = History(tmp_path)
    assert h.stats() == {"count": 0, "seconds": 0, "chars": 0, "ok": 0}
    h.add(np.zeros(10, np.float32), duration=30, raw="a", text="你好 世界", ok=1)
    h.add(np.zeros(10, np.float32), duration=90, raw="b", text="OK", ok=0)
    assert h.stats() == {"count": 2, "seconds": 120, "chars": 6, "ok": 1}


def test_api_home_and_hotkey_reset(tmp_path):
    from window import Api

    class System:
        capturing, changed, captured = None, 0, None

        def status(self):
            return {"permissions": {"accessibility": True}, "hotkeys_active": True, "capturing": self.capturing}

        def capture(self, mode):
            self.capturing = mode

        def hotkeys_changed(self):
            self.changed += 1

    cfg, sysm = cfg_with(), System()
    h = History(tmp_path)
    h.add(np.zeros(10, np.float32), duration=60, raw="x", text="一二三四五六七八", ok=1)
    api = Api(h, lambda: cfg, config_path=tmp_path / "c.json", system=sysm)
    home = api.call("home", [])["result"]
    assert home["stats"]["cpm"] == 8 and home["stats"]["ok_rate"] == 100 and home["stats"]["saved"] == 0
    assert home["hotkeys_active"] and home["asr"]["name"] == "Whisper Large V3 Turbo"
    assert [s["name"] for s in home["styles"]] == ["郵件", "聊天", "終端機／IDE"]
    api.call("hotkey_capture", ["organize"])
    assert api.call("home", [])["result"]["capturing"] == "organize"
    api.call("hotkey_cancel", [])
    assert sysm.capturing is None

    cfg["hotkeys"]["clean"] = {"keycode": 0, "mods": ["cmd"], "label": "⌘A"}
    cfg["hotkeys"]["edit"] = {"keycode": 43, "mods": ["cmd"], "label": "⌘,"}  # 佔用清理的預設鍵
    r = api.call("hotkey_reset", ["clean"])
    assert not r["ok"] and "編輯" in r["error"] and cfg["hotkeys"]["clean"]["label"] == "⌘A"
    assert api.call("hotkey_reset", ["edit"])["ok"] and cfg["hotkeys"]["edit"]["label"] == "⌘;"
    assert api.call("hotkey_reset", ["clean"])["ok"] and cfg["hotkeys"]["clean"]["label"] == "⌘,"
    assert sysm.changed == 2 and json.loads((tmp_path / "c.json").read_text())["hotkeys"]["clean"]["keycode"] == 43


def test_history_add_list_search_update_delete_purge(tmp_path):
    h = History(tmp_path)
    audio = (0.1 * np.ones(core.SAMPLE_RATE, np.float32))
    a = h.add(audio, mode="clean", app="Slack", raw="嗯 terraform", text="Terraform", segments=[{"start": 0, "end": 1, "text": "x"}], ok=1)
    b = h.add(audio, mode="organize", raw="另一筆", text="另一筆")
    assert [r["id"] for r in h.list()] == [b, a]
    assert [r["id"] for r in h.list("terraform")] == [a]  # 原文也搜得到
    r = h.get(a)
    assert r["segments"][0]["text"] == "x" and r["ok"] is True
    with wave.open(str(h.wav_path(a))) as w:
        assert w.getframerate() == core.SAMPLE_RATE and w.getnframes() == core.SAMPLE_RATE
    h.update(a, text="改過", mode="organize")
    assert h.get(a)["text"] == "改過"
    h.delete(b)
    assert h.get(b) is None and not h.wav_path(b).exists()
    h.update(a, created=time.time() - 31 * 86400)
    assert h.purge(30) == 1 and h.list() == []


def test_api_vocab_reprocess_and_method_allowlist(tmp_path):
    from window import Api
    cfg = cfg_with(vocabulary=["AWS"])
    h = History(tmp_path)
    api = Api(h, lambda: cfg, config_path=tmp_path / "c.json")
    assert api.call("vocab_add", ["aws"])["result"]["vocabulary"] == ["AWS"]  # 不分大小寫去重
    api.call("vocab_add", [["OT", "N8"]])
    api.call("repl_set", ["歐提", "OT"])
    assert json.loads((tmp_path / "c.json").read_text())["replacements"] == {"歐提": "OT"}
    api.call("vocab_remove", ["N8"])
    assert cfg["vocabulary"] == ["AWS", "OT"]
    assert api.call("_save", [])["ok"] is False

    srv = fake_llm(0, "歐提 帳號整理後")
    cfg["llm_url"] = srv.url
    rid = h.add(np.zeros(8000, np.float32), mode="clean", raw="歐提帳號", text="舊", ok=0)
    r = api.call("reprocess", [rid, "organize"])["result"]
    assert r["text"] == "OT 帳號整理後" and r["mode"] == "organize" and r["ok"] is True
    assert api.call("list", [""])["result"][0]["preview"] == "OT 帳號整理後"
    assert len(api.call("audio", [rid])["result"]) > 0
    srv.shutdown()


def test_api_reprocess_keeps_record_when_dgx_fails(tmp_path):
    from window import Api
    cfg = cfg_with(llm_url="http://127.0.0.1:1/x")
    h = History(tmp_path)
    api = Api(h, lambda: cfg, config_path=tmp_path / "c.json")
    rid = h.add(np.zeros(8000, np.float32), mode="clean", raw="原文", text="好的結果", ok=1)
    res = api.call("reprocess", [rid, "organize"])
    assert res["ok"] is False and "DGX" in res["error"]
    assert h.get(rid)["text"] == "好的結果" and h.get(rid)["mode"] == "clean"
    res = api.call("reprocess", [rid, "edit"])  # 沒有選取文字的紀錄不能改成編輯模式
    assert res["ok"] is False and "選取" in res["error"]


# ---------- macOS 介面元件 ----------

def test_paste_restores_clipboard_and_marks_transient(monkeypatch):
    import Quartz
    from AppKit import NSPasteboard, NSPasteboardTypeString
    import macos
    pb = NSPasteboard.generalPasteboard()
    seen = []

    def fake_post(tap, ev):  # 不真的按 ⌘V；記錄按下時剪貼簿裡的類型
        seen.append(list(pb.types()))
    monkeypatch.setattr(Quartz, "CGEventPost", fake_post)
    original = pb.stringForType_(NSPasteboardTypeString)
    pb.clearContents()
    pb.setString_forType_("使用者原本的剪貼簿", NSPasteboardTypeString)
    try:
        macos.paste("語音結果")
        assert len(seen) == 2 and "org.nspasteboard.TransientType" in seen[0]
        assert pb.stringForType_(NSPasteboardTypeString) == "使用者原本的剪貼簿"
    finally:
        pb.clearContents()
        if original:
            pb.setString_forType_(original, NSPasteboardTypeString)


def test_permissions_shape():
    import macos
    p = macos.permissions()
    assert set(p) == {"accessibility", "input", "microphone"} and p["microphone"] in (True, False, None)


def test_frontmost_context_has_app():
    import macos
    ctx = macos.frontmost_context()
    assert ctx["app"] and set(ctx) >= {"app", "bundle", "title"}


def test_hud_bars_and_panel():
    import hud
    assert hud.bar_heights([], 4) == [0.06] * 4
    assert hud.bar_heights([1.0], 2) == [0.06, 1.0]  # 超過滿刻度也不越界
    assert all(0 < v < 0.3 for v in hud.bar_heights([], 5, busy=True, t=1.0))
    h = hud.Hud()
    for style, busy in (("classic", False), ("classic", True), ("mini", False), ("mini", True)):
        h.show(style, [0.02, 0.04], "測試", busy)
        assert len(h.view.dataWithPDFInsideRect_(h.view.bounds())) > 0  # 實際跑一次 drawRect_
    assert not h.panel.canBecomeKeyWindow() and h.panel.ignoresMouseEvents()
    assert h.panel.frame().size.width == hud.SIZES["mini_text"][0]
    h.hide()


# ---------- E2E：本機 Whisper + 真實 DGX + 麥克風 ----------

def tts(tmp_path, text):
    aiff, wav = tmp_path / "a.aiff", tmp_path / "a.wav"
    subprocess.run(["say", "-v", "Meijia", "-o", aiff, text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, "-ar", "16000", "-ac", "1", wav], check=True)
    return np.frombuffer(wav.read_bytes()[44:], np.int16).astype(np.float32) / 32768


@pytest.mark.e2e
def test_e2e_tts_audio_to_refined_text(tmp_path):
    audio = tts(tmp_path, "嗯那個我們明天要把 Terraform 的 Cloudflare 設定改一下，然後再跑一次 plan")
    cfg = cfg_with()
    raw, segs = core.transcribe(audio, cfg)
    assert "terraform" in raw.lower() and segs and segs[0]["end"] > 0
    out, ok = core.llm_refine(raw, "clean", cfg, {"app": "Slack", "bundle": "com.tinyspeck.slackmacgap"})
    print(f"\nraw={raw}\nout={out}")
    assert ok and "Cloudflare" in out and "嗯" not in out


@pytest.mark.e2e
def test_e2e_qwen3_asr_and_switch_back(tmp_path):
    audio = tts(tmp_path, "我們現在有些權限要調整，然後明天要更新路由表")
    cfg = cfg_with(asr_model="mlx-community/Qwen3-ASR-0.6B-8bit")
    raw, segs = core.transcribe(audio, cfg)
    print(f"\nqwen raw={raw}")
    assert "權限" in raw and "们" not in raw and segs  # 已轉繁體
    from mlx_whisper.transcribe import ModelHolder
    assert ModelHolder.model is None  # 切到 Qwen 時已釋放 Whisper
    raw, _ = core.transcribe(audio, cfg_with())
    assert core._asr["model"] is None and ModelHolder.model is not None  # 切回 Whisper 時已釋放 Qwen


@pytest.mark.e2e
def test_e2e_self_correction():
    out, ok = core.llm_refine("我們明天下午三點，不對，是四點在三樓會議室開會", "clean", cfg_with())
    print(f"\nout={out}")
    assert ok and "四點" in out and "三點" not in out


@pytest.mark.e2e
def test_e2e_edit_mode_translates_selection():
    out, ok = core.llm_refine("翻成英文", "edit", cfg_with(), {"selection": "明天的維護時間改到晚上十點"})
    print(f"\nout={out}")
    assert ok and ("10" in out or "ten" in out.lower())
    assert not any("一" <= c <= "鿿" for c in out)


@pytest.mark.e2e
def test_e2e_suggest_vocabulary():
    words = core.suggest_vocabulary(["我們要把 Papaya-VPC 跟 Strawberry-VPC 透過 DXGW 打通",
                                     "Papaya-VPC 的路由表要更新"], cfg_with())
    print(f"\nwords={words}")
    assert any("Papaya" in w for w in words)


@pytest.mark.e2e
def test_recorder_start_stop():
    r = core.Recorder()
    assert len(r.stop().result()) == 0
    r.start().result()  # 等麥克風真的打開再開始計時
    time.sleep(0.5)
    audio = r.stop().result()
    assert audio.dtype == np.float32 and len(audio) > core.SAMPLE_RATE * 0.3 and r.levels


# ---------- App（voicetype.py）：不建立選單列與 event tap，只把需要的屬性塞進去 ----------

class Done:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def result(self, timeout=None):
        if self.error:
            raise self.error
        return self.value

    def exception(self):
        return self.error

    def add_done_callback(self, fn):
        fn(self)


class FakeRec:
    def __init__(self, audio=None):
        self.calls, self.audio, self.started, self.levels = [], audio, 0.0, []

    def start(self):
        self.calls.append("start")
        return Done()

    def stop(self):
        self.calls.append("stop")
        return Done(self.audio if self.audio is not None else np.zeros(0, np.float32))


class FakeListener:
    def __init__(self, alive=True):
        self.alive, self.stopped = alive, False

    def is_alive(self):
        return self.alive

    def start(self):
        pass

    def stop(self):
        self.stopped = True


@pytest.fixture
def app(tmp_path, monkeypatch):
    import queue
    import voicetype
    a = object.__new__(voicetype.App)
    a.cfg = cfg_with()
    a.state, a.mode, a.capturing, a.last_text = "idle", None, None, ""
    a.ctx, a.stage, a.stage_start, a.flash = {}, None, 0.0, ("", 0.0)
    a.ctx_thread = threading.Thread()
    a.keys = core.KeyLogic(0.4)
    a.rec, a.history, a.purged, a.jobs = FakeRec(), History(tmp_path), 0.0, queue.Queue()
    a.js_calls, a.notes = [], []
    a.win = type("W", (), {"js": lambda self, code: a.js_calls.append(code), "show": lambda self, tab: None,
                           "visible": lambda self: False})()
    a.listener, a.tap_disabled, a.tap_checked = FakeListener(), False, 0.0
    monkeypatch.setattr(voicetype.rumps, "notification", lambda *x: a.notes.append(x))
    monkeypatch.setattr(voicetype, "save_config", lambda cfg: None)
    monkeypatch.setattr(a, "build_menu", lambda: None, raising=False)
    return a


def key_event(keycode, flags, down=True):
    import Quartz
    ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    Quartz.CGEventSetFlags(ev, flags)
    return (Quartz.kCGEventKeyDown if down else Quartz.kCGEventKeyUp), ev


def test_app_hotkey_capture_defers_to_main_thread(app, monkeypatch):
    import voicetype
    calls = []
    monkeypatch.setattr(voicetype.AppHelper, "callAfter", lambda fn, *a: calls.append((fn, a)))
    app.capturing = "clean"
    assert app.intercept(*key_event(0, core.MOD_FLAGS["cmd"] | core.MOD_FLAGS["shift"])) is None
    assert app.capturing is None and app.notes == []  # event tap 執行緒沒有寫檔、碰選單或發通知
    (fn, args), = calls
    assert fn == app.save_capture and args[:3] == ("clean", 0, core.MOD_FLAGS["cmd"] | core.MOD_FLAGS["shift"])
    fn(*args)
    assert app.cfg["hotkeys"]["clean"]["mods"] == ["cmd", "shift"] and app.notes[0][1] == "已更新"
    app.save_capture("clean", 47, core.MOD_FLAGS["cmd"], ".")  # 重複就不改
    assert app.cfg["hotkeys"]["clean"]["keycode"] == 0 and app.notes[1][1] == "未變更"
    assert app.js_calls[0].startswith('captureDone({"ok": true') and '"ok": false' in app.js_calls[1]


def test_app_capture_needs_running_listener(app):
    app.capture("edit")
    assert app.capturing == "edit"
    app.capturing, app.listener = None, FakeListener(alive=False)
    with pytest.raises(RuntimeError, match="輔助使用"):
        app.capture("edit")
    assert app.capturing is None


def test_app_restarts_listener_after_permission_or_tap_disabled(app, monkeypatch):
    """沒權限時 pynput 的執行緒直接結束；授權後或 tap 被系統停用時，要自己重建。"""
    import Quartz
    import voicetype
    started, perms = [], {"accessibility": False}
    monkeypatch.setattr(voicetype.macos, "permissions", lambda: dict(perms))
    monkeypatch.setattr(voicetype.keyboard, "Listener", lambda **kw: started.append(kw) or FakeListener())
    app.listener = dead = FakeListener(alive=False)
    app.watch_listener(100)
    assert started == []  # 還沒授權，不要一直重建
    perms["accessibility"] = True
    app.watch_listener(101)
    assert started == []  # 間隔不到 TAP_CHECK 不檢查
    app.watch_listener(103)
    assert len(started) == 1 and dead.stopped and app.listener.is_alive()
    assert app.intercept(Quartz.kCGEventTapDisabledByTimeout, None) is None and app.tap_disabled
    app.watch_listener(106)
    assert len(started) == 2 and not app.tap_disabled


def test_app_hotkey_and_esc_flow(app):
    cmd = core.MOD_FLAGS["cmd"]
    app.ctx_thread = threading.Thread()
    app.start = lambda m: (setattr(app, "state", "recording"), setattr(app, "mode", m), app.rec.start())
    assert app.intercept(*key_event(43, cmd)) is None and app.state == "recording" and app.mode == "clean"
    assert app.intercept(*key_event(43, cmd, down=False)) is None  # 短按放開：繼續錄
    assert app.intercept(*key_event(core.KEY_ESC, 0)) is None and app.state == "idle"
    assert app.rec.calls == ["start", "stop"]  # Esc 也會關麥克風
    assert app.intercept(*key_event(12, cmd)) is not None  # 非快捷鍵放行
    app.state = "paused"
    assert app.intercept(*key_event(43, cmd)) is not None  # 暫停時快捷鍵也放行


def test_app_finish_reads_selection_with_copy_only_for_edit(app, monkeypatch):
    import voicetype
    calls = []
    monkeypatch.setattr(voicetype.macos, "read_selection", lambda allow_copy=True: calls.append(allow_copy) or "選取")
    monkeypatch.setattr(voicetype.macos, "frontmost_context", lambda inc: {"app": "Slack"})
    ctx = {}
    app.capture_context(ctx)
    assert calls == [False] and ctx["selection"] == "選取"  # 開始時只做 AX 讀取

    calls.clear()
    app.ctx, app.ctx_thread = {}, threading.Thread()
    app.ctx_thread.start()  # 實際流程中 start() 一定先啟動讀取情境的執行緒
    app._finish("edit")  # 用其他鍵開始、⌘; 結束：AX 讀不到時補 ⌘C
    assert calls == [True] and app.jobs.get_nowait()[2]["selection"] == "選取"
    app._finish("clean")
    assert calls == [True] and "selection" not in app.jobs.get_nowait()[2]


def test_app_process_paste_and_edit_failure(app, monkeypatch):
    import voicetype
    pasted = []
    monkeypatch.setattr(voicetype.macos, "paste", pasted.append)
    monkeypatch.setattr(voicetype, "transcribe", lambda audio, cfg: ("原文", []))
    audio = 0.1 * np.ones(core.SAMPLE_RATE, np.float32)

    monkeypatch.setattr(voicetype, "llm_refine", lambda *a: ("整理後", True))
    app.process(audio, "clean", {"app": "Slack"})
    assert pasted == ["整理後"] and app.last_text == "整理後"

    monkeypatch.setattr(voicetype, "llm_refine", lambda text, *a: (text, False))
    app.process(audio, "edit", {"selection": "原句"})
    assert pasted == ["整理後"]  # 編輯模式 DGX 失敗不貼上
    r = app.history.list()[0]
    assert r["mode"] == "edit" and r["selection"] == "原句" and r["ok"] is False

    app.process(audio, "edit", {})
    assert len(app.history.list()) == 2 and "選取" in app.flash[0]


def test_app_purges_history_periodically(app):
    rid = app.history.add(np.zeros(800, np.float32), mode="clean", raw="舊", text="舊")
    app.history.update(rid, created=time.time() - 31 * 86400)
    now = time.time()
    app.purge_history(now)
    for _ in range(50):  # 在背景執行緒刪
        if not app.history.list():
            break
        time.sleep(0.02)
    assert app.history.list() == [] and app.purged == now
    rid = app.history.add(np.zeros(800, np.float32), mode="clean", raw="舊", text="舊")
    app.history.update(rid, created=time.time() - 31 * 86400)
    app.purge_history(now + 60)  # 一小時內不重複掃
    assert len(app.history.list()) == 1


def test_app_late_context_thread_does_not_leak_into_next_recording(app, monkeypatch):
    import voicetype
    gate = threading.Event()

    def slow_context(inc):
        gate.wait(2)
        return {"app": "上一個 App"}
    monkeypatch.setattr(voicetype.macos, "frontmost_context", slow_context)
    monkeypatch.setattr(voicetype.macos, "read_selection", lambda allow_copy=True: "舊的選取")
    app.start("clean")
    old_thread = app.ctx_thread
    app.state = "idle"
    monkeypatch.setattr(voicetype.macos, "frontmost_context", lambda inc: {"app": "目前 App"})
    monkeypatch.setattr(voicetype.macos, "read_selection", lambda allow_copy=True: "")
    app.start("edit")
    app.ctx_thread.join(1)
    gate.set()
    old_thread.join(1)  # 上一輪的讀取這時才完成
    assert app.ctx == {"app": "目前 App", "selection": ""}


def test_app_finish_failure_and_mic_failure_return_to_idle(app, monkeypatch):
    app.state = "busy"
    app.rec.stop = lambda: Done(error=OSError("PortAudio"))
    app._finish("clean")
    assert app.state == "idle" and app.jobs.empty() and "處理失敗" in app.flash[0]

    app.rec.start = lambda: Done(error=OSError("沒有麥克風權限"))
    monkeypatch.setattr(app, "capture_context", lambda ctx: None)
    app.start("clean")
    assert app.state == "idle" and "麥克風" in app.flash[0]


def test_app_pause_only_when_idle(app):
    sender = type("S", (), {"state": 0})()
    app.toggle_pause(sender)
    assert app.state == "paused" and sender.state == 1
    app.toggle_pause(sender)
    assert app.state == "idle" and sender.state == 0
    app.state = "recording"
    app.toggle_pause(sender)
    assert app.state == "recording" and "無法暫停" in app.flash[0]
