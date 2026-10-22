# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Voicetype：macOS 選單列語音輸入工具。快捷鍵錄音 → 本機 mlx Whisper 辨識 → DGX LLM 整理 → 模擬 ⌘V 貼到目前 App。功能與設定欄位見 `README.md`，變更紀錄寫在 `CHANGELOG.md`，進行中工作在 `TASKS.md`。

## 指令

```bash
uv sync
uv run python voicetype.py                   # 前景啟動，印出「就緒」才可用
uv run pytest -q -m "not e2e"                # 不需外部資源
uv run pytest -q                             # 含 E2E：本機辨識模型 + 真實 DGX + 麥克風
uv run pytest -q test_voicetype.py::test_keylogic_hold_to_talk   # 單一測試
uv run pytest --cov=core --cov=history --cov=window --cov=hud --cov=macos --cov=voicetype --cov-report=term-missing
tail -f ~/Library/Logs/voicetype.log         # LaunchAgent 模式的日誌
```

沒有 linter / formatter 設定。

## 架構重點

模組分層：`core`（純邏輯，不 import AppKit）→ `macos` / `history` / `hud` / `window`（各自包一塊系統功能）→ `voicetype.App`（rumps）串起全部。import `voicetype` 不會啟動 App，測試用 `object.__new__(App)` 塞入需要的屬性，直接測方法（見 `test_voicetype.py` 的 `app` fixture）。

執行緒模型（改動時最容易出錯）：
- **event tap 執行緒**（`App.intercept`）：必須立即返回，否則 macOS 停用 tap 且 pynput 不會重新啟用。回傳 `None` 吞掉按鍵。不可開關麥克風（實測 0.07–1.4 秒）、不可做 AX 讀取、寫檔或碰 UI（含選單、通知）；UI 相關用 `AppHelper.callAfter` 回主執行緒，其他丟到短命執行緒。
- **主執行緒**（rumps `Timer` → `tick`）：所有 UI 更新（選單列 title、`Hud`）只在這裡。活動中 30fps、閒置 0.5 秒，切換頻率時重建 Timer。
- **單一 worker 執行緒**（`App.worker` + `self.jobs`）：MLX GPU stream 綁執行緒，**所有 `transcribe` 都必須在這條**，含啟動預熱（`mode=None` 的 job）。
- `Recorder.start/stop` 立即回傳 Future，實際工作在 Recorder 自己的單一執行緒依呼叫順序執行。不要改回「各開一條執行緒 + 鎖」：鎖不保證先後，Esc 太快時麥克風會關不掉。
- `_finish` 會 `join` 讀情境的執行緒，避免短錄音拿不到選取文字。開始錄音時只用 AX 讀選取文字；結束時的模式是編輯且 AX 讀不到，才在 `_finish` 用 ⌘C 退路。
- WKWebView 的 bridge 回呼在主執行緒，`Bridge` 把 `Api.call` 丟背景執行緒（DGX 可能數秒），完成後用 `AppHelper.callAfter` 回主執行緒 `evaluateJavaScript`。

狀態機：`idle → recording → busy → idle`，另有 `paused`。處理模式由**結束錄音時的快捷鍵**決定；長按放開則用開始時的模式。長按判斷在 `core.KeyLogic`，keyUp 只比 keycode（修飾鍵可能先放開）。

辨識引擎：`core.transcribe` 依 `asr_model` 分流。repo 名稱含 whisper 的走 `mlx_whisper`，其他走 `mlx_audio.stt`（Qwen3-ASR 等）。`load_asr` 同一時間只保留一個模型；切換時必須清掉 `mlx_whisper.transcribe.ModelHolder`，否則兩個模型會同時佔記憶體。輸出一律經過 OpenCC `s2tw`（不要用 `s2twp`）。切換由 `Api.asr_set` → `App.switch_asr` 在 worker 排預熱工作，狀態存在 `App.asr_state`。

DGX 呼叫統一走 `core.chat`（帶 `chat_template_kwargs.enable_thinking=false`）。`llm_refine` 回傳 `(文字, 是否成功)`，失敗退回原文；編輯模式失敗時不貼上。替換規則在 LLM 之後套用。

設定：`DEFAULTS` 與 `config.json` 一層淺合併，`load_config` 會把新欄位寫回舊設定檔。新增設定只需加在 `DEFAULTS`。

## PyObjC 陷阱

- `NSObject`／`NSView` 子類裡的 Python 輔助方法若有參數，名稱會被當成 selector 而在 class 定義時報 `BadPrototypeError`，要加 `@objc.python_method`。
- Accessory App（無 Dock 圖示）沒有主選單，WKWebView 內 ⌘C/⌘V 依賴 `App._edit_menu` 建的 Edit 選單。
- WKWebView 不實作 `WKUIDelegate`，頁面的 `confirm()`／`alert()` 不會出現；UI 用二次點擊確認。

## 測試與驗證

- 涉及按鍵注入的測試要 monkeypatch `Quartz.CGEventPost`，否則會真的貼進執行測試的終端機。
- 驗證 `ui/index.html` 版面：用 `webview.takeSnapshotWithConfiguration_completionHandler_` 存圖。`screencapture -l` 在視窗不在最前面時會拍到空白。
- 浮動視窗繪圖可用 `view.dataWithPDFInsideRect_` 觸發 `drawRect_`。
- TTS（`say -v Meijia`）的英文發音差，E2E 不要對英文專有名詞的辨識結果下嚴格斷言。
- 權限綁在 `realpath .venv/bin/python`；重建 `.venv` 不影響，換 Python 版本需重新授權。
