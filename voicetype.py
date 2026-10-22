"""Voicetype：本地語音輸入。快捷鍵錄音 → mlx 辨識 → DGX LLM 整理 → 貼到目前 App。"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np
import Quartz
import rumps
from AppKit import NSApplication, NSMenu, NSMenuItem, NSSound
from pynput import keyboard
from PyObjCTools import AppHelper

import macos
from core import (CONFIG_PATH, KEY_ESC, MOD_FLAGS, MODE_NAMES, RECORD_MODES, SAMPLE_RATE, KeyLogic, Recorder,
                  apply_replacements, clean_audio, is_too_short_or_silent, llm_refine, load_config, match_hotkey, parse_capture,
                  save_config, transcribe, translate, translate_kind, TRANSLATE_LABELS)
from history import History
from hud import Hud, TranslatePanel
from window import Api, MainWindow

ICONS = {"idle": "🎙", "recording": "🔴", "busy": "⏳", "paused": "⏸"}
STAGES = {"transcribe": "辨識中…", "llm": "DGX 整理中…"}
FAST, SLOW = 1 / 30, 0.5  # 有畫面要動時 30fps；閒置時 2 次/秒，避免空轉吃 CPU
PURGE_EVERY = 3600  # 常駐不重開，每小時刪一次過期紀錄
TAP_CHECK = 2  # 每 2 秒檢查 event tap 是否還活著
SHIFT = MOD_FLAGS["shift"]
# 提示音會被剛打開的麥克風錄進去；實測音量 0.4 時錄到的大小跟環境噪音差不多（RMS 約 0.005），
# 保險起見切掉開頭 0.25 秒。使用者聽到提示音才開口，切不到話
CUE_SKIP = 0.25


class App(rumps.App):
    def __init__(self):
        super().__init__("Voicetype", title=ICONS["idle"], quit_button=None)
        NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory：不顯示 Dock 圖示
        self._edit_menu()
        first_run = not CONFIG_PATH.exists()
        self.cfg = load_config()
        self.state, self.mode, self.capturing, self.last_text = "idle", None, None, ""
        self.swallow_up = None  # ⇧ 結束錄音時，對應的 keyUp 也要吞掉
        self.ctx, self.stage, self.stage_start, self.flash = {}, None, 0.0, ("", 0.0)
        self.ctx_thread = threading.Thread()
        self.keys = KeyLogic(self.cfg["hold_threshold"])
        self.rec, self.hud, self.history, self.purged = Recorder(), Hud(), History(), 0.0
        self.listener, self.tap_disabled, self.tap_checked = None, False, 0.0
        self.tpanel, self.tr_seq = TranslatePanel(lambda t: macos.copy_text(t, transient=True),
                                                  self.replace_selection, self.retry_translate,
                                                  theme=lambda: self.cfg["theme"]), 0
        # ponytail: MLX 的 GPU stream 綁執行緒，所有辨識都在同一條 worker 執行
        self.jobs = queue.Queue()
        self.switch_asr()  # 預熱模型（也設定 asr_state，要在 Api 之前）
        self.win = MainWindow(Api(self.history, lambda: self.cfg, copy=macos.copy_text,
                                  on_asr_change=self.switch_asr, asr_status=lambda: dict(self.asr_state),
                                  system=self))
        self.build_menu()
        self.timer = rumps.Timer(self.tick, SLOW)
        self.timer.start()
        self.start_listener()
        threading.Thread(target=self.worker, daemon=True).start()
        # 選單列圖示可能被瀏海或其他圖示擠掉，啟動時直接打開管理介面，不依賴快捷鍵；
        # 開機自動啟動（install.sh 的 LaunchAgent 帶 VOICETYPE_AUTOSTART）則只在第一次或缺權限時打開，免得每天跳視窗
        missing = not macos.permissions()["accessibility"]
        if missing:
            print("⚠️ 尚未允許「輔助使用」，快捷鍵不會生效；已打開管理介面", flush=True)
            macos.request_permissions()
        if first_run or missing or not os.environ.get("VOICETYPE_AUTOSTART"):
            AppHelper.callAfter(self.win.show, "home")

    def _edit_menu(self):
        """Accessory App 沒有主選單，管理介面的 ⌘C／⌘V／⌘A（Edit）與 ⌘M／⌘W（Window）要自己建選單才會生效。"""
        main = NSMenu.alloc().init()
        for name, items in (("Edit", (("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                      ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a"))),
                            ("Window", (("Minimize", "performMiniaturize:", "m"), ("Close", "performClose:", "w")))):
            menu = NSMenu.alloc().initWithTitle_(name)
            for title, sel, key in items:
                menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key))
            holder = NSMenuItem.alloc().init()
            holder.setSubmenu_(menu)
            main.addItem_(holder)
        NSApplication.sharedApplication().setMainMenu_(main)

    def build_menu(self):
        self.menu.clear()
        hk = self.cfg["hotkeys"]
        timeout_menu = rumps.MenuItem("LLM 逾時")
        for s in (5, 10, 20, 30):
            item = rumps.MenuItem(f"{s} 秒", callback=lambda _, s=s: self.set_cfg("llm_timeout", s))
            item.state = int(self.cfg["llm_timeout"] == s)
            timeout_menu.add(item)
        hud_menu = rumps.MenuItem("錄音視窗樣式")
        for key, name in (("classic", "Classic"), ("mini", "Mini"), ("none", "不顯示")):
            item = rumps.MenuItem(name, callback=lambda _, k=key: self.set_cfg("hud_style", k))
            item.state = int(self.cfg["hud_style"] == key)
            hud_menu.add(item)
        self.menu = [
            rumps.MenuItem(f"打開 Voicetype（{hk['window']['label']}）", callback=lambda _: self.win.show("home")),
            None,
            *[rumps.MenuItem(f"開始／結束錄音：{MODE_NAMES[m]}（{hk[m]['label']}）",
                             callback=lambda _, m=m: self.toggle(m)) for m in ("clean", "organize", "english")],
            rumps.MenuItem(f"翻譯選取的文字（{hk['translate']['label']}）",
                           callback=lambda _: threading.Thread(target=self.translate_selection, daemon=True).start()),
            None,
            rumps.MenuItem("歷史紀錄…", callback=lambda _: self.win.show("history")),
            rumps.MenuItem("詞彙管理…", callback=lambda _: self.win.show("vocab")),
            rumps.MenuItem("設定（辨識模型）…", callback=lambda _: self.win.show("settings")),
            rumps.MenuItem("重新貼上上一次結果", callback=lambda _: self.last_text and macos.paste(self.last_text)),
            None,
            rumps.MenuItem("快捷鍵…", callback=lambda _: self.win.show("home")),
            hud_menu, timeout_menu,
            rumps.MenuItem("暫停", callback=self.toggle_pause),
            self._check_item("麥克風就緒提示音", "sound_cue"),
            self._check_item("辨識前去除靜音", "audio_cleanup"),
            None,
            rumps.MenuItem("開啟設定檔", callback=lambda _: subprocess.run(["open", "-t", str(CONFIG_PATH)])),
            rumps.MenuItem("重新載入設定", callback=self.reload),
            rumps.MenuItem("結束", callback=rumps.quit_application),
        ]

    def _check_item(self, title, key):
        item = rumps.MenuItem(title, callback=lambda _: self.set_cfg(key, not self.cfg[key]))
        item.state = int(bool(self.cfg[key]))
        return item

    def switch_asr(self):
        """在 worker 佇列排一個預熱工作：worker 會釋放舊模型、載入新模型（含首次下載）。"""
        self.asr_state = {"loading": True, "error": ""}
        self.jobs.put((np.zeros(SAMPLE_RATE, np.float32), None, {}))

    def set_cfg(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        self.build_menu()

    def reload(self, _):
        self.cfg = load_config()
        self.keys.hold_threshold = self.cfg["hold_threshold"]
        self.build_menu()

    def toggle_pause(self, sender):
        if self.state not in ("idle", "paused"):
            self.set_flash("錄音或處理中無法暫停", 1.5)
            return
        self.state = "paused" if self.state == "idle" else "idle"
        sender.state = int(self.state == "paused")

    # ---------- 管理介面（window.Api 的 system）：Api 在背景執行緒呼叫，碰 UI 的都要回主執行緒 ----------
    def status(self):
        return {"permissions": macos.permissions(), "hotkeys_active": self.listener.is_alive(),
                "capturing": self.capturing}

    def capture(self, mode):
        """下一個按鍵由 intercept 攔下，交給 save_capture 存檔。"""
        if not self.listener.is_alive():
            raise RuntimeError("快捷鍵功能還沒啟用，請先允許「輔助使用」權限")
        self.capturing = mode

    def purge_soon(self):
        """保留天數改短後，下一次 tick 就刪除過期紀錄，不用等一小時。"""
        self.purged = 0.0

    def config_changed(self):
        AppHelper.callAfter(self.build_menu)

    def request_permissions(self):
        AppHelper.callAfter(macos.request_permissions)

    def open_privacy(self, key):
        macos.open_privacy(key)

    def restart(self):
        """部分 macOS 版本授權後要重開才建得了 event tap。"""
        AppHelper.callAfter(os.execv, sys.executable, [sys.executable, os.path.abspath(sys.argv[0])])

    def start_listener(self):
        if self.listener is not None:
            self.listener.stop()
        self.tap_disabled = False
        self.listener = keyboard.Listener(darwin_intercept=self.intercept)
        self.listener.start()

    def watch_listener(self, now):
        """沒權限時 event tap 建立失敗，pynput 的執行緒會直接結束且不重試；tap 被系統停用也不會自己恢復。
        定期檢查，授權後不用重開 App 快捷鍵就會生效。"""
        if now - self.tap_checked < TAP_CHECK:
            return
        self.tap_checked = now
        if self.tap_disabled or (not self.listener.is_alive() and macos.permissions()["accessibility"]):
            print("重新建立快捷鍵攔截", flush=True)
            self.start_listener()

    # ---------- UI：rumps Timer 在主執行緒，所有畫面更新集中在這裡 ----------
    def tick(self, _):
        now = time.time()
        self.purge_history(now)
        self.watch_listener(now)
        title = ICONS[self.state]
        if self.title != title:
            self.title = title
        if self.state == "recording" and now - self.rec.started > self.cfg["max_seconds"]:
            self.finish(self.mode)
        style = self.cfg["hud_style"]
        active = self.state in ("recording", "busy") or now < self.flash[1]
        if style == "none" or not active:
            self.hud.hide()
        elif self.state == "recording":
            el = now - self.rec.started
            self.hud.show(style, self.rec.levels,
                          f"錄音中（{MODE_NAMES[self.mode]}） {int(el) // 60}:{int(el) % 60:02d}", False)
        elif self.state == "busy":
            self.hud.show(style, [], f"{STAGES.get(self.stage, '處理中…')} {now - self.stage_start:.1f}s", True)
        else:
            self.hud.show(style, [], self.flash[0], True)
        interval = FAST if active else SLOW
        if self.timer.interval != interval:
            self.timer.stop()
            self.timer.interval = interval
            self.timer.start()

    def purge_history(self, now):
        if now - self.purged >= PURGE_EVERY:
            self.purged = now
            # 刪 SQLite 紀錄和 WAV 檔是 I/O，不要卡住 30fps 的主執行緒
            threading.Thread(target=self.history.purge, args=(self.cfg["history_days"],), daemon=True).start()

    def set_flash(self, text, seconds):
        self.flash = (text, time.time() + seconds)

    # ---------- 錄音流程 ----------
    def toggle(self, mode):
        if self.state == "idle":
            self.start(mode)
        elif self.state == "recording":
            self.finish(mode)

    def start(self, mode):
        # 每次錄音用新的 dict：上一輪逾時的讀取執行緒晚到時只會寫進舊的，不會蓋掉這一輪
        self.mode, self.ctx = mode, {}
        self.state = "recording"
        self.rec.start().add_done_callback(self._mic_started)
        self.ctx_thread = threading.Thread(target=self.capture_context, args=(self.ctx,), daemon=True)
        self.ctx_thread.start()

    def _mic_started(self, fut):
        if fut.exception() and self.state == "recording":
            print(f"開啟麥克風失敗：{fut.exception()!r}", flush=True)
            self.set_flash("❌ 無法開啟麥克風", 2)
            self.state = "idle"
        elif not fut.exception() and self.state == "recording" and self.cfg["sound_cue"]:
            AppHelper.callAfter(self.play_cue)

    def play_cue(self):
        sound = NSSound.soundNamed_("Tink")
        sound.setVolume_(0.4)
        sound.play()

    def capture_context(self, ctx):
        try:
            ctx.update(macos.frontmost_context(self.cfg["context"]["input_field"]))
            # 開始時只做不打擾的 AX 讀取；模式以結束時的快捷鍵為準，⌘C 退路留到 _finish 才決定
            ctx["selection"] = macos.read_selection(allow_copy=False)
        except Exception as e:
            print(f"讀取情境失敗：{e!r}", flush=True)

    def cancel(self, message):
        self.rec.stop()
        self.set_flash(message, 1.2)
        self.state = "idle"

    def finish(self, mode, send=False):
        """send：結束時按著 Shift，貼上後再按 Enter 送出。"""
        self.stage, self.stage_start = "transcribe", time.time()
        self.state = "busy"
        threading.Thread(target=self._finish, args=(mode, send), daemon=True).start()

    def _finish(self, mode, send=False):
        try:
            audio = self.rec.stop().result()
            if self.cfg["sound_cue"]:
                audio = audio[int(CUE_SKIP * SAMPLE_RATE):]
            self.ctx_thread.join(2)  # 很短的錄音可能比讀取情境還快結束
            ctx = dict(self.ctx)
            if mode == "edit" and not ctx.get("selection") and not self.ctx_thread.is_alive():
                ctx["selection"] = macos.read_selection()  # AX 讀不到（部分 Electron App）才用 ⌘C
            if self.cfg["context"]["clipboard"]:
                ctx["clipboard"] = macos.clipboard_text()[:2000]
            ctx["send"] = send
        except Exception as e:  # job 沒進佇列就不會有 worker 把狀態改回 idle
            print(f"結束錄音失敗：{e!r}", flush=True)
            self.set_flash(f"❌ 處理失敗：{type(e).__name__}", 2)
            self.state = "idle"
            return
        self.jobs.put((audio, mode, ctx))

    # ---------- 按鍵：event tap 執行緒，必須立即返回；回傳 None = 吞掉 ----------
    def intercept(self, event_type, event):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            self.tap_disabled = True  # 不能在這裡重建，交給主執行緒的 watch_listener
            return event
        if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
            return event
        keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)
        down = event_type == Quartz.kCGEventKeyDown
        repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
        now = time.time()

        if self.capturing:
            if down and not repeat:
                _, ch = Quartz.CGEventKeyboardGetUnicodeString(event, 4, None, None)
                mode, self.capturing = self.capturing, None
                AppHelper.callAfter(self.save_capture, mode, keycode, flags, ch)  # 寫檔、選單、通知都回主執行緒
            return None
        if self.state == "paused":
            return event
        if keycode == KEY_ESC and self.state == "recording":
            if down:
                self.cancel("已取消")
            return None
        if keycode == KEY_ESC and self.tpanel.shown:  # 只在翻譯視窗開著時攔截 Esc
            if down:
                AppHelper.callAfter(self.tpanel.hide)
            return None
        if not down:
            if keycode == self.swallow_up:
                self.swallow_up = None
                return None
            if keycode == self.keys.down_key:  # 長按放開（修飾鍵可能已先放開，只比 keycode）
                action = self.keys.up(keycode, now, self.state == "recording", self.mode)
                if action:
                    self.finish(action[1], send=bool(flags & SHIFT))
                return None
            return event if match_hotkey(self.cfg["hotkeys"], keycode, flags) is None else None
        mode = match_hotkey(self.cfg["hotkeys"], keycode, flags)
        if mode is None:
            send_mode = self.shift_stop(keycode, flags)
            if send_mode is None:
                return event
            if not repeat:
                self.keys.down(send_mode, keycode, now, True)
                self.swallow_up = keycode
                self.finish(send_mode, send=True)
            return None
        if mode == "window":
            if not repeat:
                AppHelper.callAfter(self.win.show, "home")
            return None
        if mode == "translate":
            if not repeat:  # 讀選取可能要 ⌘C 並等剪貼簿，不能在 event tap 執行緒做
                threading.Thread(target=self.translate_selection, daemon=True).start()
            return None
        if not repeat and self.state in ("idle", "recording"):
            action, m = self.keys.down(mode, keycode, now, self.state == "recording")
            self.start(m) if action == "start" else self.finish(m)
        return None

    def shift_stop(self, keycode, flags):
        """錄音中按 ⇧ + 錄音快捷鍵：回傳要結束的模式（貼上後按 Enter）；不是這種情況回傳 None。"""
        if self.state != "recording" or not flags & SHIFT:
            return None
        m = match_hotkey(self.cfg["hotkeys"], keycode, flags & ~SHIFT)
        return m if m in RECORD_MODES and "shift" not in self.cfg["hotkeys"][m]["mods"] else None

    # ---------- 選取翻譯：背景執行緒讀選取、呼叫 DGX，浮動視窗只在主執行緒更新 ----------
    def translate_selection(self):
        """視窗開著時再按一次：選取沒變（或沒選取）就關閉，選了新的文字就換成新的翻譯。"""
        text = macos.read_selection().strip()  # AX 讀不到才用 ⌘C；這是使用者主動按的，可以送 ⌘C
        if self.tpanel.shown and (not text or text == self.tpanel.source):
            AppHelper.callAfter(self.tpanel.hide)
            return
        anchor = macos.selection_bounds()
        if not text:
            AppHelper.callAfter(self.tpanel.show_notice, "請先選取文字", anchor, 1.5)
            return
        self.run_translate(text, anchor)

    def run_translate(self, text, anchor):
        self.tr_seq += 1
        seq = self.tr_seq
        AppHelper.callAfter(self.tpanel.show_loading, text, anchor)
        out, ok, truncated = translate(text, self.cfg)
        if seq != self.tr_seq:  # 等 DGX 時已經按了新的翻譯
            return
        if ok:
            AppHelper.callAfter(self.tpanel.show_result, apply_replacements(out, self.cfg), truncated,
                                TRANSLATE_LABELS[translate_kind(text)])
        else:
            AppHelper.callAfter(self.tpanel.show_error, "DGX 未回應，請稍後重試")

    def retry_translate(self, source):
        threading.Thread(target=self.run_translate, args=(source, None), daemon=True).start()

    def replace_selection(self, text):
        """翻譯視窗不會讓 Voicetype 變成使用中的 App，⌘V 會貼回原本的選取。"""
        threading.Thread(target=macos.paste, args=(text,), daemon=True).start()

    def save_capture(self, mode, keycode, flags, ch):
        """主執行緒：intercept 經 AppHelper.callAfter 轉過來。"""
        hk, msg = parse_capture(self.cfg["hotkeys"], mode, keycode, flags, ch)
        if hk:
            self.cfg["hotkeys"][mode] = hk
            save_config(self.cfg)
            self.build_menu()
        self.win.js(f"captureDone({json.dumps({'ok': bool(hk), 'message': msg}, ensure_ascii=False)})")
        if msg and not self.win.visible():  # 錄製到一半關掉視窗時，改用通知告知結果
            rumps.notification("Voicetype", "已更新" if hk else "未變更", msg)

    # ---------- worker：辨識 → DGX → 替換 → 貼上 → 存歷史 ----------
    def worker(self):
        while True:
            audio, mode, ctx = self.jobs.get()
            try:
                if mode is None:
                    try:
                        transcribe(audio, self.cfg)
                        self.asr_state = {"loading": False, "error": ""}
                    except Exception as e:
                        self.asr_state = {"loading": False, "error": repr(e)}
                        raise
                    hk = self.cfg["hotkeys"]
                    keys = "／".join(f"{hk[m]['label']} {MODE_NAMES[m]}" for m in RECORD_MODES)
                    print(f"就緒：{keys}（短按切換、長按說話，Esc 取消）；{hk['translate']['label']} 翻譯選取；"
                          f"{hk['window']['label']} 打開管理介面", flush=True)
                else:
                    self.process(audio, mode, ctx)
            except Exception as e:
                print(f"處理失敗：{e!r}", flush=True)
                self.set_flash(f"❌ 處理失敗：{type(e).__name__}", 2)
            finally:
                if mode is not None:
                    self.state = "idle"

    def process(self, audio, mode, ctx):
        if mode == "edit" and not ctx.get("selection"):
            self.set_flash("編輯模式需要先選取文字", 2)
            return
        if is_too_short_or_silent(audio, self.cfg):
            self.set_flash("已略過（太短或無聲）", 1.5)
            return
        audio = clean_audio(audio, self.cfg)  # 歷史紀錄也存清理後的版本，分段時間才對得上播放
        t0 = time.time()
        raw, segments = transcribe(audio, self.cfg)
        t1 = time.time()
        if not raw:
            self.set_flash("已略過（沒有辨識到文字）", 1.5)
            return
        self.stage, self.stage_start = "llm", t1
        text, ok = llm_refine(raw, mode, self.cfg, ctx)
        t2 = time.time()
        text = apply_replacements(text, self.cfg)
        if mode == "edit" and not ok:
            self.set_flash("⚠️ DGX 未回應，未修改選取的文字", 2)
        else:
            self.last_text = text
            macos.paste(text)
            if ctx.get("send"):
                macos.press_return()
            self.set_flash(("✅ 已送出" if ctx.get("send") else "✅ 已貼上") if ok else "⚠️ DGX 未回應，已貼上原文",
                           0.8 if ok else 2)
        self.history.add(audio, mode=mode, app=ctx.get("app", ""), bundle=ctx.get("bundle", ""),
                         title=ctx.get("title", ""), duration=len(audio) / SAMPLE_RATE, raw=raw, text=text,
                         segments=segments, ok=int(ok), t_transcribe=t1 - t0, t_llm=t2 - t1,
                         model=self.cfg["asr_model"], selection=ctx.get("selection", "") if mode == "edit" else "")
        self.win.js("refresh()")


def run_app():
    App().run()


if __name__ == "__main__":
    run_app()
