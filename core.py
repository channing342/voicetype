"""純邏輯：設定、快捷鍵判斷、辨識、LLM 整理、替換規則、語氣規則。不碰 GUI，可直接測試。"""
import fnmatch
import gc
import json
import os
import re
import threading
import urllib.request
from pathlib import Path

import numpy as np

CONFIG_PATH = Path.home() / ".config/voicetype/config.json"
SAMPLE_RATE = 16000
KEY_ESC, KEY_C, KEY_V = 53, 8, 9
MOD_FLAGS = {"ctrl": 1 << 18, "alt": 1 << 19, "shift": 1 << 17, "cmd": 1 << 20}
MOD_SYMBOLS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
MODE_NAMES = {"clean": "清理", "organize": "整理", "edit": "編輯", "window": "管理介面"}

SELF_CORRECTION = "說話者中途自我修正時（例如「三點，不對，四點」「改成…」「我是說…」），只保留修正後的最終說法。"

DEFAULTS = {
    "hotkeys": {
        "clean": {"keycode": 43, "mods": ["cmd"], "label": "⌘,"},
        "organize": {"keycode": 47, "mods": ["cmd"], "label": "⌘."},
        "edit": {"keycode": 41, "mods": ["cmd"], "label": "⌘;"},
        "window": {"keycode": 43, "mods": ["alt", "cmd"], "label": "⌥⌘,"},  # 打開歷史紀錄／詞彙視窗，不是錄音模式
    },
    "hold_threshold": 0.4,
    "llm_url": "http://dgx.ansible.news:8888/v1/chat/completions",
    "llm_model": "Qwen3.8-Flash-Next",
    "llm_timeout": 10,
    "temperature": 0.1,
    "asr_model": "mlx-community/whisper-large-v3-turbo",
    "vocabulary": ["Terraform", "Cloudflare", "AWS", "Direct Connect", "DX", "Ansible",
                   "BytePlus", "Gandi", "Consul", "CDN", "DNS", "VPC", "Jira", "Slack"],
    "replacements": {},
    "min_seconds": 0.5,
    "max_seconds": 300,
    "silence_rms": 0.003,
    "history_days": 30,
    "hud_style": "classic",
    "context": {"clipboard": False, "input_field": False},
    # 由上往下比對，第一條符合的生效；apps 支援萬用字元（bundle id），title 為視窗標題關鍵字
    "app_styles": [
        {"name": "郵件", "apps": ["com.apple.mail", "com.microsoft.Outlook"], "title": ["Gmail", "Outlook"],
         "style": "使用正式、完整的書面句子，語氣禮貌。"},
        {"name": "聊天", "apps": ["com.tinyspeck.slackmacgap", "jp.naver.line.mac", "ru.keepcoder.Telegram",
                                 "org.telegram.desktop", "com.hnc.Discord"], "title": [],
         "style": "使用口語、簡短的聊天語氣，句尾可省略句號。"},
        {"name": "終端機／IDE", "apps": ["com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty",
                                      "dev.warp.Warp-Stable", "com.microsoft.VSCode", "com.todesktop.*",
                                      "com.jetbrains.*"], "title": [],
         "style": "句尾不加句號；指令、路徑、參數、英文識別字保持原樣，不翻譯也不加全形標點。"},
    ],
    "prompts": {
        "clean": "你是語音輸入的文字清理器。把使用者的口語逐字稿整理成繁體中文（台灣用語）："
                 "去除贅字（嗯、呃、那個、就是）、補上標點、修正明顯的辨識錯字。" + SELF_CORRECTION +
                 "中英混用的技術名詞保留英文原文。不改寫語意、不摘要、不回答問題。只輸出整理後的文字。",
        "organize": "你是語音輸入的文字整理器。把使用者的口語逐字稿整理成繁體中文（台灣用語）："
                    "去除贅字、修正語序、補上標點。" + SELF_CORRECTION +
                    "若內容有超過 3 個重點，改為條列（- 開頭）；否則整理成通順段落。"
                    "中英混用的技術名詞保留英文原文。不加入原話沒有的資訊，不加標題或結語，不回答問題。只輸出整理後的文字。",
        "edit": "你是文字編輯器。依照使用者的語音指令修改「選取的文字」。" + SELF_CORRECTION +
                "除非指令要求其他語言，否則使用繁體中文（台灣用語）。只輸出修改後的完整文字，不加說明。",
    },
}


# 管理介面可切換的辨識模型；設定檔的 asr_model 也可填其他 mlx-audio 支援的 repo
ASR_CHOICES = [
    {"id": "mlx-community/whisper-large-v3-turbo", "name": "Whisper Large V3 Turbo", "size": "1.6 GB",
     "note": "OpenAI，fp16。辨識快，詞彙透過 initial_prompt 提示；雜音或含糊語音偶爾會產生幻覺文字。"},
    {"id": "mlx-community/Qwen3-ASR-0.6B-8bit", "name": "Qwen3-ASR 0.6B（8-bit）", "size": "1.0 GB",
     "note": "阿里通義。最省記憶體；詞彙以 hotwords 提示，輸出簡體會自動轉繁體。"},
    {"id": "mlx-community/Qwen3-ASR-1.7B-8bit", "name": "Qwen3-ASR 1.7B（8-bit）", "size": "2.5 GB",
     "note": "阿里通義。中文準確度較高（少量真人錄音實測優於 Whisper），記憶體用量最大。"},
]


def load_config(path=CONFIG_PATH):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        user = json.loads(path.read_text())
        if "whisper_model" in user:  # 舊版欄位改名
            user.setdefault("asr_model", user.pop("whisper_model"))
        for k, v in user.items():
            cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    save_config(cfg, path)  # 舊設定檔補上新欄位
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def event_mods(flags):
    return sorted(m for m, bit in MOD_FLAGS.items() if flags & bit)


def match_hotkey(hotkeys, keycode, flags):
    """回傳符合的模式名稱，沒有則 None。"""
    mods = event_mods(flags)
    for mode, hk in hotkeys.items():
        if hk["keycode"] == keycode and sorted(hk["mods"]) == mods:
            return mode
    return None


class KeyLogic:
    """短按切換、長按說話（放開送出）。回傳要執行的動作：("start", mode) / ("stop", mode) / None。"""

    def __init__(self, hold_threshold):
        self.hold_threshold = hold_threshold
        self.down_key, self.down_at = None, 0.0

    def down(self, mode, keycode, now, recording):
        if not recording:
            self.down_key, self.down_at = keycode, now
            return "start", mode
        self.down_key = None
        return "stop", mode

    def up(self, keycode, now, recording, mode):
        """放開的不一定還帶修飾鍵，所以只比 keycode。"""
        if keycode != self.down_key:
            return None
        self.down_key = None
        if recording and now - self.down_at >= self.hold_threshold:
            return "stop", mode
        return None


def is_too_short_or_silent(audio, cfg):
    return len(audio) < cfg["min_seconds"] * SAMPLE_RATE or \
        float(np.sqrt(np.mean(audio ** 2))) < cfg["silence_rms"]


def whisper_prompt(cfg):
    # ponytail: Whisper 只吃 prompt 最後約 220 token，詞彙很多時前段會被截掉；DGX 端仍拿得到完整詞彙
    # 實測：全形「：」「、」容易讓 Whisper 吐出「Ｂ」「４」之類的全形亂碼，半形逗號不會
    return "以下是繁體中文的技術討論, 會提到 " + ", ".join(cfg["vocabulary"]) + "."


def is_whisper(repo):
    return "whisper" in repo.lower()


_s2tw = None


def to_traditional(text):
    """只轉字形（s2tw），不做詞彙轉換：s2twp 會把「權限」誤轉成「許可權」，用詞交給 DGX。"""
    global _s2tw
    if _s2tw is None:
        import opencc
        _s2tw = opencc.OpenCC("s2tw")
    return _s2tw.convert(text)


_asr = {"repo": None, "model": None}  # 目前載入的非 Whisper 模型（Whisper 由 mlx_whisper.ModelHolder 快取）


def load_asr(repo):
    """切換辨識模型：先釋放舊模型再載入新的，避免兩個模型同時佔記憶體。第一次使用會從 Hugging Face 下載。"""
    import mlx.core as mx
    from mlx_whisper.transcribe import ModelHolder
    current = ModelHolder.model_path if ModelHolder.model is not None else _asr["repo"]
    if current == repo:
        return
    ModelHolder.model = ModelHolder.model_path = None
    _asr["repo"] = _asr["model"] = None
    gc.collect()
    mx.clear_cache()
    if is_whisper(repo):
        ModelHolder.get_model(repo, mx.float16)
    else:
        from mlx_audio.stt import load
        _asr["model"], _asr["repo"] = load(repo), repo


def transcribe(audio, cfg):
    """回傳 (全文, 分段 [{start, end, text}])。必須固定在同一條執行緒呼叫（MLX stream 綁執行緒）。"""
    repo = cfg["asr_model"]
    load_asr(repo)
    if is_whisper(repo):
        import mlx_whisper
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language="zh",
                                   initial_prompt=whisper_prompt(cfg), condition_on_previous_text=False)
        text, raw_segs = r["text"], r.get("segments", [])
    else:
        r = _asr["model"].generate(audio, language="Chinese", hotwords=cfg["vocabulary"])
        text, raw_segs = r.text, r.segments or []
    segs = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": to_traditional(s["text"].strip())}
            for s in raw_segs]
    return to_traditional(text.strip()), segs


def hf_cached(repo):
    """模型是否已下載到 Hugging Face 快取（決定管理介面是否標示「需下載」）。"""
    root = Path(os.environ.get("HF_HUB_CACHE") or Path.home() / ".cache/huggingface/hub")
    snaps = root / f"models--{repo.replace('/', '--')}" / "snapshots"
    return any(snaps.glob("*/config.json")) or any(snaps.glob("*/*.safetensors"))


def match_style(cfg, bundle_id, title):
    for rule in cfg["app_styles"]:
        if any(fnmatch.fnmatch(bundle_id or "", p) for p in rule["apps"]) or \
                any(k and k in (title or "") for k in rule["title"]):
            return rule
    return None


def build_messages(text, mode, cfg, ctx=None):
    """ctx：app / bundle / title / selection / clipboard / input_field（皆可缺）。"""
    ctx = ctx or {}
    system = [cfg["prompts"][mode]]
    rule = match_style(cfg, ctx.get("bundle"), ctx.get("title"))
    if rule and mode != "edit":
        system.append("輸出風格：" + rule["style"])
    if cfg["vocabulary"]:
        system.append("常用專有名詞：" + "、".join(cfg["vocabulary"]) + "。只有在逐字稿的發音或拼寫明顯對應時，"
                      "才改成這些寫法；不要把其他詞硬改成清單中的詞，不確定就保留原文。")
    bg = [f"{label}：{ctx[k]}" for k, label in
          (("app", "目前 App"), ("title", "視窗標題"), ("clipboard", "剪貼簿內容"), ("input_field", "輸入框現有文字"))
          if ctx.get(k)]
    if bg:
        system.append("背景資訊（僅供判斷用詞，不要輸出）：\n" + "\n".join(bg))
    user = f"選取的文字：\n{ctx.get('selection', '')}\n\n語音指令：\n{text}" if mode == "edit" else text
    return [{"role": "system", "content": "\n\n".join(system)}, {"role": "user", "content": user}]


def chat(messages, cfg, timeout=None):
    body = {"model": cfg["llm_model"], "temperature": cfg["temperature"],
            "chat_template_kwargs": {"enable_thinking": False}, "messages": messages}
    req = urllib.request.Request(cfg["llm_url"], json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout or cfg["llm_timeout"]) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def llm_refine(text, mode, cfg, ctx=None):
    """回傳 (文字, 是否成功)；連不上、逾時或空回應都退回原文。"""
    try:
        out = chat(build_messages(text, mode, cfg, ctx), cfg)
        return (out, True) if out else (text, False)
    except Exception as e:
        print(f"LLM 失敗，改用原文：{e!r}", flush=True)
        return text, False


def apply_replacements(text, cfg):
    for src, dst in cfg["replacements"].items():
        if src:
            text = text.replace(src, dst)
    return text


def suggest_vocabulary(texts, cfg):
    """請 DGX 從最近的紀錄找出還沒收錄的專有名詞。回傳 list[str]。"""
    prompt = ("以下是使用者最近的語音輸入紀錄。找出其中反覆出現、值得加入語音辨識詞彙表的專有名詞"
              "（產品、服務、公司、專案、技術縮寫、人名），排除一般詞彙與已收錄的詞。"
              "只輸出 JSON 字串陣列，例如 [\"Terraform\", \"OT\"]，最多 30 個。\n\n已收錄：" +
              "、".join(cfg["vocabulary"]))
    out = chat([{"role": "system", "content": prompt}, {"role": "user", "content": "\n---\n".join(texts)}],
               cfg, timeout=max(cfg["llm_timeout"], 60))
    m = re.search(r"\[.*\]", out, re.S)
    words = json.loads(m.group(0)) if m else []
    have = {v.lower() for v in cfg["vocabulary"]}
    return [w for w in dict.fromkeys(str(x).strip() for x in words) if w and w.lower() not in have]


class Recorder:
    """start/stop 在背景執行緒呼叫（開麥克風可能超過 1 秒，不能卡住 event tap），用鎖保證順序。"""

    def __init__(self):
        self.chunks, self.levels, self.stream, self.started = [], [], None, 0.0
        self.lock = threading.Lock()

    def start(self):
        import sounddevice as sd
        with self.lock:
            self.chunks, self.levels = [], []
            # 每區塊 50ms，levels 一格就是浮動視窗的一根柱子
            self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                         blocksize=SAMPLE_RATE // 20, callback=self._on_audio)
            self.stream.start()

    def _on_audio(self, data, *_):
        self.chunks.append(data.copy())
        self.levels.append(float(np.sqrt(np.mean(data ** 2))))

    def stop(self):
        with self.lock:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            return np.concatenate(self.chunks)[:, 0] if self.chunks else np.zeros(0, np.float32)
