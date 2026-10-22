"""錄音浮動視窗：Classic（波形＋狀態列）／Mini（只有波形）／None。原生 NSView 繪製，不搶焦點。"""
import math
import time

import objc
from AppKit import (NSAttributedString, NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSGraphicsContext, NSMakeRect,
                    NSPanel, NSScreen, NSStatusWindowLevel, NSView)

SIZES = {"classic": (260, 84), "mini": (150, 34), "mini_text": (210, 34)}
BARS = {"classic": 36, "mini": 14}
BAR_W, BAR_GAP = 3, 3
FULL_SCALE = 0.05  # RMS 到這個值就是滿格


def bar_heights(levels, n, busy=False, t=0.0):
    """把最近的 RMS 轉成 0–1 的柱高；處理中改成緩慢起伏的呼吸動畫。"""
    if busy:
        return [0.12 + 0.1 * (1 + math.sin(t * 5 + i * 0.6)) / 2 for i in range(n)]
    tail = ([0.0] * n + list(levels))[-n:]
    return [max(0.06, min(1.0, (v / FULL_SCALE) ** 0.7)) for v in tail]


class WaveView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(WaveView, self).initWithFrame_(frame)
        self.style, self.levels, self.status, self.busy, self.show_text = "classic", [], "", False, False
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
        for v in bar_heights(self.levels, n, self.busy, time.time()):
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
        v.show_text = style == "mini" and busy
        w, h = SIZES["mini_text" if v.show_text else style]
        f = NSScreen.mainScreen().frame()
        self.panel.setFrame_display_(NSMakeRect(f.origin.x + (f.size.width - w) / 2, f.origin.y + 120, w, h), False)
        v.setNeedsDisplay_(True)
        self.panel.orderFrontRegardless()

    def hide(self):
        self.panel.orderOut_(None)
