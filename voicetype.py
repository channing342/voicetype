"""Voicetype：本地語音輸入。快捷鍵錄音 → mlx Whisper 辨識 → DGX LLM 整理 → 貼到目前 App。"""
import queue
import subprocess
import threading
import time

import numpy as np

from core import (CONFIG_PATH, KEY_ESC, MOD_SYMBOLS, MODE_NAMES, SAMPLE_RATE, KeyLogic, Recorder,
                  apply_replacements, event_mods, is_too_short_or_silent, llm_refine, load_config,
                  match_hotkey, save_config, transcribe)

ICONS = {"idle": "🎙", "recording": "🔴", "busy": "⏳", "paused": "⏸"}
STAGES = {"transcribe": "辨識中…", "llm": "DGX 整理中…"}
FAST, SLOW = 1 / 30, 0.5  # 有畫面要動時 30fps；閒置時 2 次/秒，避免空轉吃 CPU


def run_app():
    import rumps
    import Quartz
    from AppKit import NSApplication, NSMenu, NSMenuItem
    from pynput import keyboard
    from PyObjCTools import AppHelper

    import macos
    from history import History
    from hud import Hud
    from window import Api, MainWindow

    class App(rumps.App):
        def __init__(self):
            super().__init__("Voicetype", title=ICONS["idle"], quit_button=None)
            NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory：不顯示 Dock 圖示
            self._edit_menu()
            self.cfg = load_config()
            self.state, self.mode, self.capturing, self.last_text = "idle", None, None, ""
            self.ctx, self.stage, self.stage_start, self.flash = {}, None, 0.0, ("", 0.0)
            self.ctx_thread = threading.Thread()
            self.keys = KeyLogic(self.cfg["hold_threshold"])
            self.rec, self.hud, self.history = Recorder(), Hud(), History()
            self.history.purge(self.cfg["history_days"])
            self.asr_state = {"loading": True, "error": ""}
            self.win = MainWindow(Api(self.history, lambda: self.cfg, copy=macos.copy_text,
                                      on_asr_change=self.switch_asr, asr_status=lambda: dict(self.asr_state)))
            self.build_menu()
            self.timer = rumps.Timer(self.tick, SLOW)
            self.timer.start()
            keyboard.Listener(darwin_intercept=self.intercept).start()
            # ponytail: MLX 的 GPU stream 綁執行緒，所有辨識都在同一條 worker 執行
            self.jobs = queue.Queue()
            self.jobs.put((np.zeros(SAMPLE_RATE, np.float32), None, {}))  # 預熱模型
            threading.Thread(target=self.worker, daemon=True).start()

        def _edit_menu(self):
            """Accessory App 沒有主選單，WKWebView 裡的 ⌘C／⌘V／⌘A 需要 Edit 選單才會生效。"""
            main, edit = NSMenu.alloc().init(), NSMenu.alloc().initWithTitle_("Edit")
            for title, sel, key in (("Undo", "undo:", "z"), ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                                    ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
                edit.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, sel, key))
            holder = NSMenuItem.alloc().init()
            holder.setSubmenu_(edit)
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
            hotkey_menu = rumps.MenuItem("快捷鍵")
            for m in ("clean", "organize", "edit", "window"):
                hotkey_menu.add(rumps.MenuItem(f"錄製{MODE_NAMES[m]}快捷鍵（目前 {hk[m]['label']}）",
                                               callback=lambda _, m=m: self.capture(m)))
            self.menu = [
                *[rumps.MenuItem(f"開始／結束錄音：{MODE_NAMES[m]}（{hk[m]['label']}）",
                                 callback=lambda _, m=m: self.toggle(m)) for m in ("clean", "organize")],
                None,
                rumps.MenuItem("歷史紀錄…", callback=lambda _: self.win.show("history")),
                rumps.MenuItem("詞彙管理…", callback=lambda _: self.win.show("vocab")),
                rumps.MenuItem("設定（辨識模型）…", callback=lambda _: self.win.show("settings")),
                rumps.MenuItem("重新貼上上一次結果", callback=lambda _: self.last_text and macos.paste(self.last_text)),
                None,
                hotkey_menu, hud_menu, timeout_menu,
                rumps.MenuItem("暫停", callback=self.toggle_pause),
                None,
                rumps.MenuItem("開啟設定檔", callback=lambda _: subprocess.run(["open", "-t", str(CONFIG_PATH)])),
                rumps.MenuItem("重新載入設定", callback=self.reload),
                rumps.MenuItem("結束", callback=rumps.quit_application),
            ]

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
            if self.state in ("idle", "paused"):
                self.state = "paused" if self.state == "idle" else "idle"
                sender.state = int(self.state == "paused")

        def capture(self, mode):
            self.capturing = mode
            rumps.notification("Voicetype", "", "請按下新的快捷鍵（需含修飾鍵，Esc 取消）")

        # ---------- UI：rumps Timer 在主執行緒，所有畫面更新集中在這裡 ----------
        def tick(self, _):
            now = time.time()
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

        def set_flash(self, text, seconds):
            self.flash = (text, time.time() + seconds)

        # ---------- 錄音流程 ----------
        def toggle(self, mode):
            if self.state == "idle":
                self.start(mode)
            elif self.state == "recording":
                self.finish(mode)

        def start(self, mode):
            self.mode, self.rec.started, self.rec.levels, self.ctx = mode, time.time(), [], {}
            self.state = "recording"
            threading.Thread(target=self.rec.start, daemon=True).start()
            self.ctx_thread = threading.Thread(target=self.capture_context, args=(mode,), daemon=True)
            self.ctx_thread.start()

        def capture_context(self, mode):
            try:
                ctx = macos.frontmost_context(self.cfg["context"]["input_field"])
                # 只有用編輯快捷鍵開始時才允許 ⌘C 退路，其他模式只做不打擾的 AX 讀取
                ctx["selection"] = macos.read_selection(allow_copy=mode == "edit")
                self.ctx = ctx
            except Exception as e:
                print(f"讀取情境失敗：{e!r}", flush=True)

        def cancel(self, message):
            threading.Thread(target=self.rec.stop, daemon=True).start()
            self.set_flash(message, 1.2)
            self.state = "idle"

        def finish(self, mode):
            self.stage, self.stage_start = "transcribe", time.time()
            self.state = "busy"
            threading.Thread(target=self._finish, args=(mode,), daemon=True).start()

        def _finish(self, mode):
            audio = self.rec.stop()
            self.ctx_thread.join(2)  # 很短的錄音可能比讀取情境還快結束
            ctx = dict(self.ctx)
            if self.cfg["context"]["clipboard"]:
                ctx["clipboard"] = macos.clipboard_text()[:2000]
            self.jobs.put((audio, mode, ctx))

        # ---------- 按鍵：event tap 執行緒，必須立即返回；回傳 None = 吞掉 ----------
        def intercept(self, event_type, event):
            if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                return event
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            flags = Quartz.CGEventGetFlags(event)
            down = event_type == Quartz.kCGEventKeyDown
            repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
            now = time.time()

            if self.capturing:
                if down and not repeat:
                    self.save_capture(event, keycode, flags)
                return None
            if self.state == "paused":
                return event
            if keycode == KEY_ESC and self.state == "recording":
                if down:
                    self.cancel("已取消")
                return None
            if not down:
                if keycode == self.keys.down_key:  # 長按放開（修飾鍵可能已先放開，只比 keycode）
                    action = self.keys.up(keycode, now, self.state == "recording", self.mode)
                    if action:
                        self.finish(action[1])
                    return None
                return event if match_hotkey(self.cfg["hotkeys"], keycode, flags) is None else None
            mode = match_hotkey(self.cfg["hotkeys"], keycode, flags)
            if mode is None:
                return event
            if mode == "window":
                if not repeat:
                    AppHelper.callAfter(self.win.show, "history")
                return None
            if not repeat and self.state in ("idle", "recording"):
                action, m = self.keys.down(mode, keycode, now, self.state == "recording")
                self.start(m) if action == "start" else self.finish(m)
            return None

        def save_capture(self, event, keycode, flags):
            mode, self.capturing = self.capturing, None
            mods = event_mods(flags)
            if keycode == KEY_ESC:
                return
            if not mods:
                rumps.notification("Voicetype", "未變更", "快捷鍵需包含 ⌘ ⌥ ⌃ ⇧ 其中之一")
                return
            for other, o in self.cfg["hotkeys"].items():
                if other != mode and o["keycode"] == keycode and sorted(o["mods"]) == mods:
                    rumps.notification("Voicetype", "未變更", f"與{MODE_NAMES[other]}快捷鍵 {o['label']} 重複")
                    return
            _, ch = Quartz.CGEventKeyboardGetUnicodeString(event, 4, None, None)
            label = "".join(MOD_SYMBOLS[m] for m in ("ctrl", "alt", "shift", "cmd") if m in mods) + \
                ((ch or "").upper().strip() or f"key{keycode}")
            self.cfg["hotkeys"][mode] = {"keycode": keycode, "mods": mods, "label": label}
            save_config(self.cfg)
            self.build_menu()
            rumps.notification("Voicetype", "已更新", f"{MODE_NAMES[mode]}快捷鍵：{label}")

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
                        print(f"就緒：{hk['clean']['label']} 清理／{hk['organize']['label']} 整理／{hk['edit']['label']} 編輯"
                              f"（短按切換、長按說話，Esc 取消）；{hk['window']['label']} 打開管理介面", flush=True)
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
                self.set_flash("✅ 已貼上" if ok else "⚠️ DGX 未回應，已貼上原文", 0.8 if ok else 2)
            self.history.add(audio, mode=mode, app=ctx.get("app", ""), bundle=ctx.get("bundle", ""),
                             title=ctx.get("title", ""), duration=len(audio) / SAMPLE_RATE, raw=raw, text=text,
                             segments=segments, ok=int(ok), t_transcribe=t1 - t0, t_llm=t2 - t1,
                             model=self.cfg["asr_model"], selection=ctx.get("selection", "") if mode == "edit" else "")
            self.win.js("refresh()")

    App().run()


if __name__ == "__main__":
    run_app()
