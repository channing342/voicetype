"""浮動視窗：錄音（Classic 波形＋狀態列／Mini 只有波形／None）與選取翻譯。原生 NSView 繪製，不搶焦點。"""
import math
import time

import objc
from AppKit import (NSAppearance, NSAttributedString, NSBackingStoreBuffered, NSBezierPath, NSButton, NSColor,
                    NSCursor, NSEvent, NSTrackingArea, NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSGraphicsContext,
                    NSInsetRect, NSMakeRect, NSObject, NSPanel, NSScreen, NSScrollView, NSStatusWindowLevel, NSTextField,
                    NSTextView, NSView, NSWorkspace)
from PyObjCTools import AppHelper

SIZES = {"classic": (260, 84), "mini": (150, 34), "mini_text": (210, 34)}
BARS = {"classic": 36, "mini": 14}
BAR_W, BAR_GAP = 3, 3
# dB 刻度（實測 MacBook 麥克風：安靜約 -61 dBFS、喇叭播放的語音 -40～-32、對著講話更大聲）
DB_FLOOR, DB_FULL, BAR_MIN = -55.0, -20.0, 0.06


def bar_heights(levels, n, busy=False, t=0.0, reduce_motion=False):
    """把最近的 RMS 轉成 0–1 的柱高（dB 刻度）；處理中是緩慢起伏的呼吸動畫，「減少動態效果」時改成靜止。"""
    if busy:
        if reduce_motion:
            return [0.17] * n
        return [0.12 + 0.1 * (1 + math.sin(t * 5 + i * 0.6)) / 2 for i in range(n)]
    tail = ([0.0] * n + list(levels))[-n:]
    return [max(BAR_MIN, min(1.0, (20 * math.log10(max(v, 1e-6)) - DB_FLOOR) / (DB_FULL - DB_FLOOR))) for v in tail]


def reduce_motion():
    return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())


class WaveView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(WaveView, self).initWithFrame_(frame)
        self.style, self.levels, self.status, self.busy, self.show_text = "classic", [], "", False, False
        self.reduce_motion = False
        return self

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        b = self.bounds()
        w, h = b.size.width, b.size.height
        radius = h / 2 if self.style == "mini" else 14
        shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(b, radius, radius)
        NSColor.colorWithWhite_alpha_(0.05, 0.94).setFill()
        shape.fill()

        strip = 26 if self.style == "classic" else 0
        if strip:
            NSGraphicsContext.saveGraphicsState()
            shape.addClip()
            NSColor.colorWithWhite_alpha_(0.18, 1.0).setFill()
            NSBezierPath.fillRect_(NSMakeRect(0, 0, w, strip))
            NSGraphicsContext.restoreGraphicsState()
            self._text(self.status, w / 2, strip / 2, 12, 0.85)

        if self.show_text:  # Mini 處理中：用文字取代波形
            self._text(self.status, w / 2, h / 2, 13, 0.95)
            return
        n = BARS[self.style]
        area_h = h - strip - 16
        mid = strip + (h - strip) / 2
        x = (w - (n * BAR_W + (n - 1) * BAR_GAP)) / 2
        NSColor.whiteColor().setFill()
        for v in bar_heights(self.levels, n, self.busy, time.time(), self.reduce_motion):
            bh = max(2.0, v * area_h)
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, mid - bh / 2, BAR_W, bh), BAR_W / 2, BAR_W / 2).fill()
            x += BAR_W + BAR_GAP

    @objc.python_method
    def _text(self, s, cx, cy, size, alpha):
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(size),
                 NSForegroundColorAttributeName: NSColor.colorWithWhite_alpha_(1.0, alpha)}
        a = NSAttributedString.alloc().initWithString_attributes_(s, attrs)
        sz = a.size()
        a.drawAtPoint_((cx - sz.width / 2, cy - sz.height / 2))


class Hud:
    def __init__(self):
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, *SIZES["classic"]), 1 << 7, NSBackingStoreBuffered, False)  # borderless + nonactivating
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setHasShadow_(True)
        self.panel.setCollectionBehavior_((1 << 0) | (1 << 8))  # canJoinAllSpaces | fullScreenAuxiliary
        self.view = WaveView.alloc().initWithFrame_(NSMakeRect(0, 0, *SIZES["classic"]))
        self.panel.setContentView_(self.view)

    def show(self, style, levels, status, busy):
        """style: classic / mini；busy=True 表示錄音結束、處理中。"""
        v = self.view
        v.style, v.levels, v.status, v.busy = style, levels, status, busy
        v.reduce_motion = reduce_motion() if busy else False
        v.show_text = style == "mini" and busy
        w, h = SIZES["mini_text" if v.show_text else style]
        f = NSScreen.mainScreen().frame()
        self.panel.setFrame_display_(NSMakeRect(f.origin.x + (f.size.width - w) / 2, f.origin.y + 120, w, h), False)
        v.setNeedsDisplay_(True)
        self.panel.orderFrontRegardless()

    def hide(self):
        self.panel.orderOut_(None)


# ---------- 選取翻譯的浮動視窗 ----------

TR_W, TR_MAX_H, TR_PAD, TR_BTN_H, TR_HEAD_H = 460, 400, 14, 24, 16
TR_MIN_W, TR_MIN_H, EDGE = 240, 130, 6  # 使用者縮放的最小尺寸（至少留兩行文字）；邊緣 6px 內按下就是縮放
TOP, LEFT, BOTTOM, RIGHT = 1, 2, 4, 8  # 跟 NSCursorFrameResizePosition 的位元相同
DRAG_MASK, UP_MASK, LEFT_UP = 1 << 6, 1 << 2, 2  # leftMouseDragged／leftMouseUp 的 mask 與事件類型
AUTORESIZE_W, AUTORESIZE_H, STICK_TOP, STICK_BOTTOM = 2, 16, 8, 32  # NSView autoresizingMask


def edge_at(x, y, w, h):
    """視窗內座標（原點左下）落在哪些邊緣，回傳 TOP／LEFT／BOTTOM／RIGHT 的位元組合，0 表示不在邊緣。"""
    return ((TOP if y >= h - EDGE else BOTTOM if y <= EDGE else 0) |
            (LEFT if x <= EDGE else RIGHT if x >= w - EDGE else 0))


def resized_frame(f, edges, dx, dy):
    """從 (x, y, w, h) 依拖動的邊緣與滑鼠位移算新的視窗範圍；對邊固定，不小於最小尺寸。"""
    x, y, w, h = f
    if edges & RIGHT:
        w = max(TR_MIN_W, w + dx)
    if edges & LEFT:
        nw = max(TR_MIN_W, w - dx)
        x, w = x + w - nw, nw
    if edges & TOP:
        h = max(TR_MIN_H, h + dy)
    if edges & BOTTOM:
        nh = max(TR_MIN_H, h - dy)
        y, h = y + h - nh, nh
    return x, y, w, h


def resize_cursor(edges):
    if hasattr(NSCursor, "frameResizeCursorFromPosition_inDirections_"):  # macOS 15+：有斜角游標
        return NSCursor.frameResizeCursorFromPosition_inDirections_(edges, 3)
    return NSCursor.resizeUpDownCursor() if edges in (TOP, BOTTOM) else NSCursor.resizeLeftRightCursor()
CLICK_MASK = (1 << 1) | (1 << 3)  # 滑鼠左鍵、右鍵按下


def first_line(text, n=80):
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:n] + ("…" if len(line) > n or "\n" in text.strip() else "")


class KeyPanel(NSPanel):
    """無邊框的 panel 預設不能成為 key window，譯文就不能用滑鼠選取；不搶焦點靠 nonactivating。"""

    def canBecomeKeyWindow(self):
        return True


APPEARANCES = {"light": "NSAppearanceNameAqua", "dark": "NSAppearanceNameDarkAqua"}  # system：跟著 macOS


class Background(NSView):
    """系統動態色會依 panel 的外觀（淺色／深色）自動換。邊緣按下是縮放，其他地方按下是拖移視窗。
    不用 movableByWindowBackground：那會讓視窗先吃掉滑鼠事件，邊緣就收不到。"""

    def isOpaque(self):
        return False

    def acceptsFirstMouse_(self, event):
        return True  # 視窗不是 key window 時，第一下就能拖或縮放

    def updateTrackingAreas(self):
        for area in self.trackingAreas():
            self.removeTrackingArea_(area)
        # mouseMoved | enteredAndExited | activeAlways | inVisibleRect：App 不在前景也要換游標
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), 0x02 | 0x01 | 0x80 | 0x200, self, None))

    def mouseMoved_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        edges = edge_at(p.x, p.y, self.bounds().size.width, self.bounds().size.height)
        (resize_cursor(edges) if edges else NSCursor.arrowCursor()).set()

    def mouseExited_(self, event):
        NSCursor.arrowCursor().set()

    def mouseDown_(self, event):
        win, b = self.window(), self.bounds()
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        edges = edge_at(p.x, p.y, b.size.width, b.size.height)
        if not edges:
            win.performWindowDragWithEvent_(event)
            return
        start, f = NSEvent.mouseLocation(), win.frame()
        f0 = (f.origin.x, f.origin.y, f.size.width, f.size.height)
        while True:
            e = win.nextEventMatchingMask_(DRAG_MASK | UP_MASK)
            m = NSEvent.mouseLocation()
            win.setFrame_display_(NSMakeRect(*resized_frame(f0, edges, m.x - start.x, m.y - start.y)), True)
            if e.type() == LEFT_UP:
                break

    def drawRect_(self, rect):
        shape = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSInsetRect(self.bounds(), 0.5, 0.5), 14, 14)
        NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.97).setFill()
        shape.fill()
        NSColor.separatorColor().setStroke()  # 淺色模式下跟白色背景的視窗分得開
        shape.stroke()


class Actions(NSObject):
    """NSButton 的 target；owner 是 TranslatePanel。"""

    def copy_(self, sender):
        self.owner.do_copy()

    def replace_(self, sender):
        self.owner.do_replace()

    def retry_(self, sender):
        self.owner.do_retry()


class TranslatePanel:
    """只能在主執行緒呼叫。anchor：選取文字的位置 (x, y, w, h)，原點在主螢幕左上角（AX 座標）；None 就放在滑鼠旁邊。"""

    def __init__(self, on_copy, on_replace, on_retry, theme=lambda: "system"):
        self.on_copy, self.on_replace, self.on_retry, self.theme = on_copy, on_replace, on_retry, theme
        self.shown, self.source, self.text, self.state, self.monitor, self.notice_seq = False, "", "", "", None, 0
        self.placed = self.sized = None  # 上次由程式設定的位置與尺寸；跟目前不同就是使用者拖過或縮放過
        self.panel = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, TR_W, 120), 1 << 7, NSBackingStoreBuffered, False)  # borderless + nonactivating
        self.panel.setAcceptsMouseMovedEvents_(True)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_((1 << 0) | (1 << 8))  # canJoinAllSpaces | fullScreenAuxiliary
        self.view = Background.alloc().initWithFrame_(NSMakeRect(0, 0, TR_W, 120))
        self.panel.setContentView_(self.view)

        self.head = NSTextField.labelWithString_("")
        self.head.setFont_(NSFont.systemFontOfSize_(11))
        self.head.setTextColor_(NSColor.secondaryLabelColor())
        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, TR_W - 2 * TR_PAD, 40))
        self.scroll.setDrawsBackground_(False)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setAutohidesScrollers_(True)
        self.scroll.setAutoresizingMask_(AUTORESIZE_W | AUTORESIZE_H)  # 使用者縮放時跟著視窗變大變小
        self.head.setAutoresizingMask_(AUTORESIZE_W | STICK_TOP)
        self.body = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, TR_W - 2 * TR_PAD, 40))
        self.body.setEditable_(False)
        self.body.setSelectable_(True)
        self.body.setDrawsBackground_(False)
        self.body.setTextContainerInset_((0, 0))
        self.body.textContainer().setLineFragmentPadding_(0)
        self.body.textContainer().setWidthTracksTextView_(True)
        self.body.setAutoresizingMask_(AUTORESIZE_W)
        self.body.setVerticallyResizable_(True)
        self.body.setMaxSize_((1e7, 1e7))
        self.scroll.setDocumentView_(self.body)

        self.actions = Actions.alloc().init()
        self.actions.owner = self
        self.buttons = {}
        for key, title in (("copy", "複製"), ("replace", "取代選取"), ("retry", "重試")):
            b = NSButton.buttonWithTitle_target_action_(title, self.actions, f"{key}:")
            b.setBezelStyle_(10)  # smallSquare：方角，避免膠囊形按鈕
            b.setAutoresizingMask_(STICK_BOTTOM)
            self.buttons[key] = b
        for v in (self.head, self.scroll, *self.buttons.values()):
            self.view.addSubview_(v)

    # ---- 狀態 ----
    def show_loading(self, source, anchor):
        self.source, self.text = source, ""
        self._render("loading", "翻譯中…", first_line(source), anchor, dim=True)

    def show_result(self, text, truncated, label=""):
        """label：標題列文字（例如「中文 → 英文」），也是拖移視窗的把手。"""
        if self.shown:  # 等 DGX 時使用者已經關掉就不再跳出來
            self.text = text
            self._render("result", label + ("（原文太長，只翻譯了前面的部分）" if truncated else ""), text, None)

    def show_error(self, message):
        if self.shown:
            self._render("error", "", message, None)

    def show_notice(self, message, anchor, seconds):
        self.source = self.text = ""
        self._render("notice", "", message, anchor)
        self.notice_seq += 1
        AppHelper.callLater(seconds, self._hide_notice, self.notice_seq)

    def hide(self):
        if self.monitor is not None:
            NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        self.shown, self.state = False, ""
        self.panel.orderOut_(None)

    def _hide_notice(self, seq):
        if self.state == "notice" and seq == self.notice_seq:
            self.hide()

    # ---- 按鈕 ----
    def selected_or_all(self):
        r = self.body.selectedRange()
        return self.text[r.location:r.location + r.length] if r.length else self.text

    def do_copy(self):
        self.on_copy(self.selected_or_all())
        self.buttons["copy"].setTitle_("已複製")
        AppHelper.callLater(1.2, lambda: self.buttons["copy"].setTitle_("複製"))

    def do_replace(self):
        text = self.text
        self.hide()
        self.on_replace(text)

    def do_retry(self):
        self.on_retry(self.source)

    # ---- 版面 ----
    def _render(self, state, head, body, anchor, dim=False):
        self.state = state
        visible = {"result": ("copy", "replace"), "error": ("retry",)}.get(state, ())
        for key, b in self.buttons.items():
            b.setHidden_(key not in visible)
        self.head.setStringValue_(head)
        self.body.setString_(body)
        self.body.setFont_(NSFont.systemFontOfSize_(14))
        self.body.setTextColor_(NSColor.secondaryLabelColor() if dim else NSColor.labelColor())
        name = APPEARANCES.get(self.theme())
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(name) if name else None)
        self.body.setSelectedRange_((0, 0))

        f = self.panel.frame()
        # 開著時使用者縮放過：之後換內容都維持使用者的尺寸，關掉再開才回到自動大小
        keep = self.shown and state != "notice" and (f.size.width, f.size.height) != self.sized
        inner = (f.size.width if keep else TR_W) - 2 * TR_PAD
        if state == "notice":  # 短提示：依文字寬度縮小
            inner = min(inner, NSAttributedString.alloc().initWithString_attributes_(
                body, {NSFontAttributeName: NSFont.systemFontOfSize_(14)}).size().width + 4)
        self.body.setFrame_(NSMakeRect(0, 0, inner, 10))
        lm, tc = self.body.layoutManager(), self.body.textContainer()
        tc.setContainerSize_((inner, 1e7))
        lm.ensureLayoutForTextContainer_(tc)
        text_h = lm.usedRectForTextContainer_(tc).size.height + 2
        head_h = TR_HEAD_H + 4 if head else 0
        btn_h = TR_BTN_H + 10 if visible else 0
        h = f.size.height if keep else min(TR_MAX_H, TR_PAD * 2 + head_h + btn_h + text_h)
        w = inner + 2 * TR_PAD
        # 先定視窗大小再擺子元件：反過來的話，改視窗大小時 autoresizing 會把剛擺好的元件再位移一次
        frame = self._frame(w, h, anchor)
        self.panel.setFrame_display_(frame, False)
        self.placed, self.sized = (frame.origin.x, frame.origin.y), (frame.size.width, frame.size.height)
        scroll_h = h - TR_PAD * 2 - head_h - btn_h
        self.scroll.setFrame_(NSMakeRect(TR_PAD, TR_PAD + btn_h, inner, scroll_h))
        self.body.setFrame_(NSMakeRect(0, 0, inner, max(text_h, scroll_h)))
        self.head.setFrame_(NSMakeRect(TR_PAD, h - TR_PAD - TR_HEAD_H, inner, TR_HEAD_H))
        self.head.setHidden_(not head)
        x = TR_PAD
        for key in visible:
            b = self.buttons[key]
            b.sizeToFit()
            bw = b.frame().size.width + 12
            b.setFrame_(NSMakeRect(x, TR_PAD, bw, TR_BTN_H))
            x += bw + 8
        self.body.scrollRangeToVisible_((0, 0))
        self.view.setNeedsDisplay_(True)
        if not self.shown:
            self.panel.orderFrontRegardless()  # 不 makeKey：焦點留在原本的 App
            self.monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(CLICK_MASK, lambda e: self.hide())
        self.shown = True

    def moved(self):
        o = self.panel.frame().origin
        return self.shown and self.placed is not None and (o.x, o.y) != self.placed

    def _frame(self, w, h, anchor):
        if self.shown and (anchor is None or self.moved()):  # 換內容或使用者拖過：留在原地，上緣固定
            f = self.panel.frame()
            return NSMakeRect(f.origin.x, f.origin.y + f.size.height - h, w, h)
        if anchor is None:
            m = NSEvent.mouseLocation()
            ax, ay, ah = m.x, m.y - 12, 12
        else:  # AX 座標原點在主螢幕左上角，Cocoa 在左下角
            top = NSScreen.screens()[0].frame().size.height
            ax, ay, ah = anchor[0], top - anchor[1] - anchor[3], anchor[3]
        screen = next((sc for sc in NSScreen.screens() if _contains(sc.frame(), ax, ay)), NSScreen.mainScreen())
        v = screen.visibleFrame()
        y = ay - h - 6 if ay - h - 6 >= v.origin.y else ay + ah + 6  # 優先放在選取文字下方，放不下就放上方
        x = min(max(ax, v.origin.x + 8), v.origin.x + v.size.width - w - 8)
        y = min(max(y, v.origin.y + 8), v.origin.y + v.size.height - h - 8)
        return NSMakeRect(x, y, w, h)


def _contains(f, x, y):
    return f.origin.x <= x <= f.origin.x + f.size.width and f.origin.y <= y <= f.origin.y + f.size.height
