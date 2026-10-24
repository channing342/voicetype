"""純邏輯：設定、快捷鍵判斷、辨識、LLM 整理、替換規則、語氣規則。不碰 GUI，可直接測試。"""
import difflib
import fnmatch
import gc
import hashlib
import json
import os
import re
import time
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

# 版本號只寫在 pyproject.toml，選單列、管理介面、啟動訊息都從這裡讀
VERSION = tomllib.loads((Path(__file__).parent / "pyproject.toml").read_text())["project"]["version"]
CONFIG_PATH = Path.home() / ".config/voicetype/config.json"
SAMPLE_RATE = 16000
KEY_ESC, KEY_C, KEY_V, KEY_RETURN = 53, 8, 9, 36
MOD_FLAGS = {"ctrl": 1 << 18, "alt": 1 << 19, "shift": 1 << 17, "cmd": 1 << 20}
MOD_SYMBOLS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
MODE_NAMES = {"clean": "清理", "organize": "整理", "english": "說中文出英文", "edit": "編輯",
              "translate": "翻譯選取", "window": "管理介面"}
RECORD_MODES = ("clean", "organize", "english", "edit")  # 會錄音的模式；translate、window 按一下就執行

SELF_CORRECTION = "說話者自我修正時（例如「三點，不對，四點」「改成…」「我是說…」），即使改口隔了好幾句才出現，也只保留最後的決定。"
INTENT = ("說話者對你下的指示或順口補充的旁白（例如「這段寫正式一點」「這是給你參考的背景」）只用來理解意思，不寫進輸出。"
          "說話者想不起名稱而改用描述時（例如「那個管 DNS 的服務」），只有在詞彙表或背景資訊裡有明確對應才換成正確名稱，"
          "否則保留原本的描述，不要猜。")
# 歷代預設 prompt 的 sha1 前 12 碼。load_config 會把整份設定寫回設定檔，prompt 若還是舊版預設（使用者沒改過）
# 就換成新版，否則改了預設也到不了使用者手上。改 DEFAULTS 的 prompt 時，把改之前的 hash 加進來。
OLD_PROMPTS = {"09d1a581ffad", "133e96b227a7", "3aa4912d09fa", "3aab445029a2", "cea31b1a5f32", "d30f32b5e38a", "d3e7260901f0", "e34655fbf72b", "e66ae5fb9b79"}

# LLM 設定不在 config.json：多組設定存在 SQLite（llmstore.LLMStore），使用中那組在執行期放進 cfg["llm"]
DEFAULT_LLM = {"id": "dgx", "name": "DGX", "url": "http://dgx.ansible.news:8888/v1/chat/completions",
               "model": "Qwen3.8-Flash-Next", "timeout": 10, "headers": {}}
RUNTIME_KEYS = ("llm",)  # 只在記憶體、不寫進設定檔的欄位

DEFAULTS = {
    "hotkeys": {
        "clean": {"keycode": 43, "mods": ["cmd"], "label": "⌘,"},
        "organize": {"keycode": 47, "mods": ["cmd"], "label": "⌘."},
        "edit": {"keycode": 41, "mods": ["cmd"], "label": "⌘;"},
        "english": {"keycode": 47, "mods": ["alt", "cmd"], "label": "⌥⌘."},
        "translate": {"keycode": 39, "mods": ["cmd"], "label": "⌘'"},  # 翻譯選取的文字，結果顯示在浮動視窗
        "window": {"keycode": 43, "mods": ["alt", "cmd"], "label": "⌥⌘,"},  # 打開歷史紀錄／詞彙視窗，不是錄音模式
    },
    "hold_threshold": 0.4,
    "translate_timeout": 20,  # 選取翻譯可能是長文，逾時另外設
    "translate_max_chars": 4000,
    "temperature": 0.1,
    "asr_model": "mlx-community/whisper-large-v3-turbo",
    "vocabulary": ["Terraform", "Cloudflare", "AWS", "Direct Connect", "DX", "Ansible",
                   "BytePlus", "Gandi", "Consul", "CDN", "DNS", "VPC", "Jira", "Slack"],
    "replacements": {},
    "min_seconds": 0.5,
    "max_seconds": 300,
    "silence_rms": 0.003,
    "audio_cleanup": True,  # 辨識前去掉頭尾與過長的靜音、把太小聲的錄音放大，減少 Whisper 的幻覺文字
    "sound_cue": True,  # 麥克風真的打開時播一聲提示音
    "history_days": 30,
    "mic": "auto",  # auto：避開藍牙／iPhone／虛擬裝置；system：跟系統預設；其他值是指定裝置的 UID
    "mic_name": "",  # 指定裝置的名稱：沒接上時提示「找不到○○」用
    "ignored_corrections": [],  # 在「從紀錄找聽錯的詞」按過忽略的「原文→修正」
    "hud_style": "classic",
    "theme": "system",  # system／light／dark：管理介面與翻譯浮動視窗共用
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
                 "去除贅字（嗯、呃、那個、就是）、補上標點、修正明顯的辨識錯字。" + SELF_CORRECTION + INTENT +
                 "中英混用的技術名詞保留英文原文。不改寫語意、不摘要、不回答問題。只輸出整理後的文字。",
        "organize": "你是語音輸入的文字整理器。把使用者的口語逐字稿整理成繁體中文（台灣用語）："
                    "去除贅字、修正語序、補上標點。" + SELF_CORRECTION + INTENT +
                    "說話順序跳來跳去時，依主題與重要性重新排列。若內容有超過 3 個重點，改為條列（- 開頭）；否則整理成通順段落。"
                    "中英混用的技術名詞保留英文原文。不加入原話沒有的資訊，不加標題或結語，不回答問題。只輸出整理後的文字。",
        "english": "你是語音輸入的翻譯器。把使用者的中文口語逐字稿翻成自然、通順的英文："
                   "去除贅字，不逐字直譯，保持原意，不加入原話沒有的資訊。" + SELF_CORRECTION + INTENT +
                   "先套用修正、只留下最終說法再翻譯，英文裡不要出現 actually、I mean 這類改口痕跡"
                   "（例：「我明天早上到，不對，下午才到」→ I'll arrive tomorrow afternoon.）。" +
                   "技術名詞、產品名、指令保留原文。不回答問題。只輸出英文譯文。",
        "translate_zh": "把使用者給的文字翻譯成繁體中文（台灣用語），保持原意與段落、條列格式；"
                        "技術名詞、產品名、程式碼、指令保留原文。只輸出譯文，不加說明。",
        "translate_en": "把使用者給的中文翻譯成自然、通順的英文，保持原意與段落、條列格式；"
                        "技術名詞、產品名、程式碼、指令保留原文。只輸出譯文，不加說明。",
        "dictionary": "你是英漢字典。針對使用者給的英文單字或短語，用繁體中文（台灣用語）以純文字回答，不用 Markdown：\n"
                      "第一行：原文、詞性縮寫、最常用的中文意思\n"
                      "接著列出 2 到 3 個常見意思，每行以「1.」「2.」編號\n"
                      "最後一行：「例：」加一個英文例句與中文翻譯",
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
    for k, v in cfg["prompts"].items():
        if k in DEFAULTS["prompts"] and hashlib.sha1(v.encode()).hexdigest()[:12] in OLD_PROMPTS:
            cfg["prompts"][k] = DEFAULTS["prompts"][k]
    save_config(cfg, path)  # 舊設定檔補上新欄位
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: v for k, v in cfg.items() if k not in RUNTIME_KEYS}, ensure_ascii=False, indent=2))


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


def parse_capture(hotkeys, mode, keycode, flags, char):
    """錄製快捷鍵：回傳 (新的快捷鍵設定或 None, 提示訊息)。按 Esc 取消時訊息是空字串。"""
    if keycode == KEY_ESC:
        return None, ""
    mods = event_mods(flags)
    if not mods:
        return None, "快捷鍵需包含 ⌘ ⌥ ⌃ ⇧ 其中之一"
    other = match_hotkey({k: v for k, v in hotkeys.items() if k != mode}, keycode, flags)
    if other:
        return None, f"與{MODE_NAMES[other]}快捷鍵 {hotkeys[other]['label']} 重複"
    label = "".join(MOD_SYMBOLS[m] for m in ("ctrl", "alt", "shift", "cmd") if m in mods) + \
        ((char or "").upper().strip() or f"key{keycode}")
    return {"keycode": keycode, "mods": mods, "label": label}, f"{MODE_NAMES[mode]}快捷鍵：{label}"


def rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


FRAME = SAMPLE_RATE * 30 // 1000  # 30ms 一格判斷有沒有聲音
KEEP_PAD, MAX_GAP = 0.2, 0.6  # 有聲段前後各留 0.2 秒；中間超過 0.6 秒的停頓縮成 0.6 秒
TARGET_PEAK, MAX_GAIN = 0.9, 8.0


def clean_audio(audio, cfg):
    """靜音移除 + 音量正規化。門檻取設定的 silence_rms 與背景噪音（第 10 百分位）×3 兩者較大的。"""
    n = len(audio) // FRAME
    if not cfg["audio_cleanup"] or n == 0:
        return audio
    frames = audio[:n * FRAME].reshape(n, FRAME)
    levels = np.sqrt(np.mean(frames ** 2, axis=1))
    voiced = levels >= max(cfg["silence_rms"], float(np.percentile(levels, 10)) * 3)
    if not voiced.any():
        return audio
    pad, gap = int(KEEP_PAD * 1000 / 30), int(MAX_GAP * 1000 / 30)
    keep = np.zeros(n, bool)
    for i in np.flatnonzero(voiced):
        keep[max(0, i - pad):i + pad + 1] = True
    kept = np.flatnonzero(keep)
    out, silent_run = [], 0
    for i in range(kept[0], kept[-1] + 1):  # 頭尾靜音整段去掉；中間的停頓只留前 gap 格
        silent_run = 0 if keep[i] else silent_run + 1
        if silent_run <= gap:
            out.append(frames[i])
    audio = np.concatenate(out)
    peak = float(np.max(np.abs(audio)))
    return audio * min(TARGET_PEAK / peak, MAX_GAIN) if peak > 0 else audio


def is_too_short_or_silent(audio, cfg):
    return len(audio) < cfg["min_seconds"] * SAMPLE_RATE or rms(audio) < cfg["silence_rms"]


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


CHUNK_SEC, CHUNK_MIN_SEC = 30, 20  # 超過 30 秒就切；切點找 20–30 秒之間最安靜的地方


def split_chunks(audio):
    """長錄音切成每塊 ≤ 30 秒，切在最安靜的 30ms。整段一次送 Whisper 時，咬住一句幻覺會一直重複到結尾。"""
    chunks, start = [], 0
    while len(audio) - start > CHUNK_SEC * SAMPLE_RATE:
        lo, hi = start + CHUNK_MIN_SEC * SAMPLE_RATE, start + CHUNK_SEC * SAMPLE_RATE
        frames = audio[lo:hi][:(hi - lo) // FRAME * FRAME].reshape(-1, FRAME)
        cut = lo + int(np.argmin(np.mean(frames ** 2, axis=1))) * FRAME + FRAME // 2
        chunks.append((start, audio[start:cut]))
        start = cut
    chunks.append((start, audio[start:]))
    return chunks


def join_text(a, b):
    """接兩段辨識文字：英數字相鄰補空白，中文直接接。"""
    if not a or not b:
        return a or b
    return a + (" " if a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum() else "") + b


def transcribe(audio, cfg):
    """回傳 (全文, 分段 [{start, end, text}])。必須固定在同一條執行緒呼叫（MLX stream 綁執行緒）。"""
    text, segs = "", []
    for offset, chunk in split_chunks(audio):
        t, ss = transcribe_chunk(chunk, cfg)
        text = join_text(text, t)
        segs += [{**x, "start": round(x["start"] + offset / SAMPLE_RATE, 2),
                  "end": round(x["end"] + offset / SAMPLE_RATE, 2)} for x in ss]
    return text, segs


def transcribe_chunk(audio, cfg):
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


def chat(messages, cfg, timeout=None, profile=None):
    """profile 預設用使用中那組（cfg["llm"]）；測試連線會直接傳表單上還沒儲存的值。"""
    profile = profile or cfg["llm"]
    body = {"model": profile["model"], "temperature": cfg["temperature"],
            "chat_template_kwargs": {"enable_thinking": False}, "messages": messages}
    hdrs = {"Content-Type": "application/json", **profile["headers"]}
    req = urllib.request.Request(profile["url"], json.dumps(body).encode(), hdrs)
    with urllib.request.urlopen(req, timeout=timeout or profile["timeout"]) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def llm_refine(text, mode, cfg, ctx=None):
    """回傳 (文字, 是否成功)；連不上、逾時或空回應都退回原文。"""
    try:
        out = chat(build_messages(text, mode, cfg, ctx), cfg)
        return (out, True) if out else (text, False)
    except Exception as e:
        print(f"LLM 失敗，改用原文：{e!r}", flush=True)
        return text, False


def translate_kind(text):
    """選取翻譯的方向：以中文為主 → "en"；英文 ≤ 3 個字 → "dictionary"；其他語言 → "zh"。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    kana_hangul = re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text)
    letters = len(re.findall(r"[A-Za-z]", text))
    # ponytail: 一個漢字約當 3 個英文字母來比較，中英夾雜但以中文敘述為主的句子會判成中文
    if cjk and not kana_hangul and cjk * 3 >= letters:
        return "en"
    if letters and not cjk and not kana_hangul and len(text.split()) <= 3:
        return "dictionary"
    return "zh"


TRANSLATE_LABELS = {"en": "中文 → 英文", "zh": "翻成中文", "dictionary": "字典"}


def translate(text, cfg):
    """選取翻譯：回傳 (結果, 是否成功, 是否截斷)。失敗時結果是空字串。"""
    text = text.strip()
    truncated = len(text) > cfg["translate_max_chars"]
    text = text[:cfg["translate_max_chars"]].rstrip()
    kind = translate_kind(text)
    system = cfg["prompts"]["dictionary" if kind == "dictionary" else f"translate_{kind}"]
    if cfg["vocabulary"] and kind != "dictionary":
        system += "\n\n這些專有名詞保持原寫法：" + "、".join(cfg["vocabulary"])
    try:
        out = chat([{"role": "system", "content": system}, {"role": "user", "content": text}], cfg,
                   timeout=cfg["translate_timeout"])
        return out, bool(out), truncated
    except Exception as e:
        print(f"翻譯失敗：{e!r}", flush=True)
        return "", False, truncated


def apply_replacements(text, cfg):
    for src, dst in cfg["replacements"].items():
        if src:
            text = text.replace(src, dst)
    return text


# ---------- 聽錯的詞：比對「辨識原文 → DGX 輸出」與「DGX 輸出 → 使用者手動修改」 ----------

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*|\s+|.", re.S)  # 英數字整個字一組，其他一個字一組
NOISE = re.compile(r"[\s\W_]+")  # 只差標點或空白的不算
# 太長的改動是改寫句子，不是聽錯。以「詞」計算：中文一個字、英文一個單字各算一個
MAX_SRC_WORDS, MAX_DST_WORDS, MAX_CHARS = 4, 6, 20


def diff_pairs(before, after):
    """回傳 [(原文片段, 修正後)]。"""
    a, b = TOKEN.findall(before or ""), TOKEN.findall(after or "")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "replace":
            continue
        src, dst = "".join(a[i1:i2]).strip(), "".join(b[j1:j2]).strip()
        if NOISE.sub("", src) == NOISE.sub("", dst) or not NOISE.sub("", src) or not NOISE.sub("", dst):
            continue
        words = lambda toks: sum(1 for t in toks if not t.isspace())
        if words(a[i1:i2]) <= MAX_SRC_WORDS and words(b[j1:j2]) <= MAX_DST_WORDS and max(len(src), len(dst)) <= MAX_CHARS:
            out.append((src, dst))
    return out


def suggest_corrections(records, cfg):
    """跨紀錄統計聽錯的詞。只用清理模式（其他模式會改寫或翻譯，比對沒有意義）加上使用者手動改過的地方。
    回傳 [{src, dst, count, example, manual}]，次數多的在前；已有替換規則或按過忽略的不列。"""
    ignored = set(cfg["ignored_corrections"])
    found = {}
    for r in records:
        pairs, ai = [], r.get("ai_text")
        if r["mode"] == "clean":
            pairs += [(p, False) for p in diff_pairs(r["raw"], ai if ai is not None else r["text"])]
        if r.get("edited") and ai is not None:
            pairs += [(p, True) for p in diff_pairs(r["ai_text"], r["text"])]
        for (src, dst), manual in pairs:
            if src in cfg["replacements"] or f"{src}→{dst}" in ignored:
                continue
            item = found.setdefault((src, dst), {"src": src, "dst": dst, "count": 0, "example": "", "manual": False})
            item["count"] += 1
            item["manual"] |= manual
            if not item["example"]:
                i = (r["text"] or "").find(dst)
                item["example"] = (r["text"] or "")[max(0, i - 15):i + len(dst) + 15] if i >= 0 else ""
    return sorted(found.values(), key=lambda x: (-x["count"], -x["manual"], x["src"]))


def parse_json_list(out):
    """從 LLM 回覆取出第一個 JSON 陣列。前後可能有 ```json 或說明文字（說明裡也可能有 ]），所以從第一個 [ 開始解析一段。"""
    i = out.find("[")
    try:
        items = json.JSONDecoder().raw_decode(out[i:])[0] if i >= 0 else []
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def find_misheard(text, cfg, raw=""):
    """請 DGX 找單筆紀錄裡疑似聽錯的詞。DGX 只能回報「把 original 改成 suggestion」，
    original 必須一字不差出現在文字裡，否則丟掉（避免 DGX 自己編出原文沒有的詞）。"""
    prompt = ("下面是語音輸入辨識後再整理過的繁體中文。找出最可能是同音或近音聽錯的詞（專有名詞、技術名詞、人名最常見），"
              "不要改寫句子、不要改標點或語氣。只輸出 JSON 陣列，每一項是 "
              '{"original": 文字裡原樣出現的片段, "suggestion": 正確寫法, "reason": 簡短理由}，最多 10 項；沒有就輸出 []。'
              "文字裡的詞跟使用者的常用詞發音相近、寫法卻不同時，通常就是聽錯。"
              "\n\n使用者的常用詞：" + "、".join(cfg["vocabulary"]))
    user = text + (f"\n\n（辨識原文，供參考：{raw}）" if raw and raw != text else "")
    out = chat([{"role": "system", "content": prompt}, {"role": "user", "content": user}],
               cfg, timeout=max(cfg["llm"]["timeout"], 30))
    items, seen, result = parse_json_list(out), set(), []
    for it in items:
        if not isinstance(it, dict):
            continue
        o, sug = str(it.get("original", "")).strip(), str(it.get("suggestion", "")).strip()
        if o and sug and o != sug and o in text and o not in seen:
            seen.add(o)
            result.append({"original": o, "suggestion": sug, "reason": str(it.get("reason", "")).strip()})
    return result


def pick_input(devices, setting, name=""):
    """選麥克風。devices：[{uid, name, transport, default}]（只含有輸入的裝置）；name：指定裝置的名稱（提示用）。
    回傳 (裝置或 None＝交給系統預設, 提示訊息)。"""
    default = next((d for d in devices if d["default"]), None)
    if setting == "system":
        return None, ""
    notice = ""
    if setting != "auto":
        chosen = next((d for d in devices if d["uid"] == setting), None)
        if chosen:
            return chosen, ""
        notice = f"找不到{name or '指定的麥克風'}"
    if default and default["transport"] not in AVOID_TRANSPORTS:
        return (default, f"{notice}，改用{default['name']}") if notice else (None, "")
    builtin = next((d for d in devices if d["transport"] == "bltn"), None)
    fallback = builtin or next((d for d in devices if d["transport"] not in AVOID_TRANSPORTS), None)
    if fallback is None:
        return None, notice
    return fallback, (f"{notice}，改用{fallback['name']}" if notice else "")


def sd_index(devices, chosen, sd_devices):
    """CoreAudio 裝置 → sounddevice 編號。sounddevice 拿不到 UID，用名稱對應；
    同名的裝置靠排序區分（PortAudio 依 CoreAudio 的順序列裝置）。對不上回傳 None。"""
    name = chosen["name"].strip()  # 名稱前後的空白兩邊不一定一致
    rank = [d["uid"] for d in devices if d["name"].strip() == name].index(chosen["uid"])
    same = [i for i, d in enumerate(sd_devices) if d["name"].strip() == name and d["max_input_channels"] > 0]
    return same[rank] if rank < len(same) else None


def mic_name(devices, uid):
    """設定麥克風時一併記下名稱；auto／system 或找不到時是空字串。"""
    return next((d["name"] for d in devices if d["uid"] == uid), "")


MIC_TAGS = {"blue": "藍牙", "blea": "藍牙", "ccwl": "iPhone", "ccwd": "iPhone", "virt": "虛擬", "grup": "虛擬",
            "fgrp": "虛擬", "bltn": "內建", "usb ": "USB"}


def mic_options(devices, current, name=""):
    """管理介面與選單列的麥克風選項：[{id, name}]。指定的裝置沒接上時也列出來（標示未連接）。"""
    opts = [{"id": "auto", "name": "自動（避開藍牙、iPhone、虛擬裝置）"}, {"id": "system", "name": "跟隨系統預設"}]
    opts += [{"id": d["uid"], "name": d["name"] + (f"（{MIC_TAGS[d['transport']]}）" if d["transport"] in MIC_TAGS else "")}
             for d in devices]
    if current not in {o["id"] for o in opts}:
        opts.append({"id": current, "name": f"{name or '指定的麥克風'}（未連接）"})
    return opts


# CoreAudio transport type（四字元碼）：藍牙會讓整條藍牙連線掉成電話音質；iPhone 切換要 1.5–2 秒；虛擬裝置不是真的麥克風
AVOID_TRANSPORTS = {"blue", "blea", "ccwl", "ccwd", "virt", "grup", "fgrp"}


class Recorder:
    """開關麥克風可能超過 1 秒，不能卡住 event tap。start/stop 立即返回 Future，實際工作在同一條執行緒依呼叫順序執行。
    只用鎖不夠：鎖只保證互斥不保證先後，stop 先搶到鎖的話，之後才開的麥克風就沒人關。"""

    def __init__(self, choose=None):
        """choose(sd) → (sounddevice 裝置編號或 None＝系統預設, 狀態列顯示的麥克風名稱, 提示訊息)。"""
        self.chunks, self.levels, self.stream, self.started = [], [], None, 0.0
        self.choose, self.mic_label, self.mic_notice = choose, "", ""
        self.ops = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recorder")

    def start(self):
        self.started, self.levels, self.mic_label, self.mic_notice = time.time(), [], "", ""
        return self.ops.submit(self._start)

    def stop(self):
        """Future 的結果是錄到的音訊（float32）。"""
        return self.ops.submit(self._stop)

    def _start(self):
        import sounddevice as sd
        self.chunks = []
        # PortAudio 只在初始化時讀一次裝置清單，App 開著時才接上的耳機看不到；重讀實測約 4ms
        sd._terminate()
        sd._initialize()
        device, self.mic_label, self.mic_notice = self.choose(sd) if self.choose else (None, "", "")
        # 每區塊 50ms，levels 一格就是浮動視窗的一根柱子
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=device,
                                     blocksize=SAMPLE_RATE // 20, callback=self._on_audio)
        self.stream.start()

    def _on_audio(self, data, *_):
        self.chunks.append(data.copy())
        self.levels.append(rms(data))

    def _stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return np.concatenate(self.chunks)[:, 0] if self.chunks else np.zeros(0, np.float32)
