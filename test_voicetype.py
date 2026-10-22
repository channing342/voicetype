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


# ---------- 歷史紀錄 ----------

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
    assert len(r.stop()) == 0
    r.start()
    time.sleep(0.5)
    audio = r.stop()
    assert audio.dtype == np.float32 and len(audio) > core.SAMPLE_RATE * 0.3 and r.levels
