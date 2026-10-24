#!/usr/bin/env python3
"""clock_creature.py —— 桌面上的「时钟生物 / CHRONO-ORGANISM」。

一只无边框、逐像素透明、始终置顶、鼠标可穿透的小时钟。视觉是科研仪器 /
观测样本的风格：深色观测窗、极坐标网格、细分度盘、单色读数条；它自己缓慢
游走，鼠标靠近时会惊慌逃开，三根指针的颜色由它们各自指向的时刻决定。

性能设计（详见 README 与 tools/bench_runtime.py）：
* 静态部分（光晕、外壳、网格、度盘、刻度）只在启动时画一次，缓存为 QPixmap；
  每帧只画指针、眼睛、波形嘴、触须和读数条；
* 巡航 30 fps，检测到鼠标靠近时自动切到 60 fps，逃跑结束再降回 30 fps；
* 速度按屏幕宽度归一化：巡航 0.028 屏宽/秒，30 秒最多走 0.84 屏宽；
* 不使用持续增长的缓存/列表，内存占用在运行期间基本恒定。

退出方式：右键系统托盘图标 → 退出（鼠标穿透的窗口本身点不到）。
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

TAU = math.tau
APP_NAME = "ClockCreature"
APP_TITLE = "Chrono-Organism"

# ---- 手感参数 --------------------------------------------------------------
ROAM_FRACTION = 0.028          # 巡航速度 = 屏宽 × 0.028 px/s
ROAM_MIN = 24.0
FLEE_FRACTION = 0.42           # 受惊瞬时速度 = 屏宽 × 0.42，单独限幅
FLEE_MIN = 400.0
FLEE_MAX = 1250.0
DIAMETER_FRACTION = 0.13       # 表盘直径 = 屏幕短边 × 0.13
DIAMETER_MIN = 130
DIAMETER_MAX = 230
FLEE_ZONE_R = 2.2              # 警觉半径 = 2.2 R + max(70, 0.08 屏宽)
FLEE_ZONE_SCREEN = 0.08

CALM_FRAME_MS = 42             # 巡航 24 fps
PANIC_FRAME_MS = 16            # 逃跑 60 fps

# 仪器配色（指针颜色只在这几种色相之间移动，避免变成彩虹玩具）
ICE = QColor(208, 238, 240)
CYAN = QColor(104, 224, 214)
AMBER = QColor(255, 184, 96)
HOLD_GRAY = QColor(126, 146, 153)
MID_TEAL = QColor(74, 178, 170)      # 中调青色：深浅壁纸上都能看见
OUTLINE = QColor(2, 7, 11, 215)      # 指针/眼睛的深色描边

# 内部填充透明度：数值越小越透。0 = 完全透明（浅色壁纸下刻度可能消失）
GLASS_CENTER_ALPHA = 0
GLASS_EDGE_ALPHA = 0
DIAL_ALPHA = 0
READOUT_ALPHA = 0


# -----------------------------------------------------------------------------
# 小工具
# -----------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _smoothstep(v: float) -> float:
    v = _clamp(v, 0.0, 1.0)
    return v * v * (3.0 - 2.0 * v)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = _clamp(t, 0.0, 1.0)
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


def _rgba(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def _polar(center: QPointF, radius: float, angle: float) -> QPointF:
    """angle 从 12 点方向开始、顺时针为正（弧度）。"""
    return QPointF(
        center.x() + math.sin(angle) * radius,
        center.y() - math.cos(angle) * radius,
    )


def _hand_colors(now: datetime):
    """指针颜色由它当前指向的时刻决定，且只落在蓝—青—绿—琥珀区间内。

    * 时针：12 小时走完 210° → 35°（蓝 → 青 → 绿 → 琥珀）；
    * 分针：1 小时走完 190° → 35°；
    * 秒针：1 分钟走完 35° → 190°（琥珀 → 青），所以每分钟都能看到变化。
    """
    sec = now.second + now.microsecond / 1_000_000.0
    minute = now.minute + sec / 60.0
    hour = (now.hour % 12) + minute / 60.0

    hour_hue = 210.0 - 175.0 * (hour / 12.0)
    minute_hue = 190.0 - 155.0 * (minute / 60.0)
    second_hue = 35.0 + 155.0 * (sec / 60.0)

    hour_col = QColor.fromHslF((hour_hue % 360.0) / 360.0, 0.62, 0.72, 1.0)
    minute_col = QColor.fromHslF((minute_hue % 360.0) / 360.0, 0.72, 0.64, 1.0)
    second_col = QColor.fromHslF((second_hue % 360.0) / 360.0, 0.86, 0.58, 1.0)
    return hour_col, minute_col, second_col, sec, minute, hour


def _body_path(center: QPointF, R: float) -> QPainterPath:
    """静态的、略不规则的样本外壳；不随时间变形，便于整张缓存。"""
    path = QPainterPath()
    n = 96
    for i in range(n):
        a = TAU * i / n
        rr = R * (1.0 + 0.010 * math.sin(3.0 * a + 1.3)
                  + 0.006 * math.sin(5.0 * a + 0.4))
        x = center.x() + math.cos(a) * rr
        y = center.y() + math.sin(a) * rr
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _hand_path(center: QPointF, angle: float, length: float,
               half_width: float, bend: float = 0.0) -> QPainterPath:
    """有锥度的针状指针（angle 从 12 点顺时针）。"""
    ux, uy = math.sin(angle), -math.cos(angle)
    px, py = math.cos(angle), math.sin(angle)

    def at(dist: float, off: float) -> QPointF:
        return QPointF(center.x() + ux * dist + px * off,
                       center.y() + uy * dist + py * off)

    mid = length * 0.58
    c_off = bend * length
    path = QPainterPath()
    path.moveTo(at(0.0, -half_width))
    path.cubicTo(
        at(mid * 0.55, -half_width * 0.90),
        at(mid, -half_width * 0.28 + c_off),
        at(length, c_off * 0.55),
    )
    path.cubicTo(
        at(mid, half_width * 0.28 + c_off),
        at(mid * 0.55, half_width * 0.90),
        at(0.0, half_width),
    )
    path.closeSubpath()
    return path


def _stroke_hand(painter: QPainter, center: QPointF, angle: float,
                 length: float, half_width: float, color: QColor,
                 bend: float = 0.0, halo: bool = False,
                 outline: bool = True) -> None:
    path = _hand_path(center, angle, length, half_width, bend)
    if halo:
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(_rgba(color, 38), max(3.0, half_width * 4.0),
                            Qt.SolidLine, Qt.RoundCap))
        painter.drawPath(path)
    painter.setBrush(color)
    if outline:
        painter.setPen(QPen(OUTLINE, max(0.9, half_width * 0.55),
                            Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    else:
        painter.setPen(Qt.NoPen)
    painter.drawPath(path)


# -----------------------------------------------------------------------------
# 时钟生物
# -----------------------------------------------------------------------------
class ClockCreature(QWidget):
    def __init__(self, diameter: int | None = None):
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle(APP_TITLE)

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self._screen = screen
        self._bounds = QRectF(screen.availableGeometry())

        auto = int(round(min(self._bounds.width(), self._bounds.height())
                         * DIAMETER_FRACTION))
        auto = int(_clamp(auto, DIAMETER_MIN, DIAMETER_MAX))
        self._diameter = int(diameter) if diameter else auto
        self._radius = self._diameter / 2.0

        side = int(round(self._diameter * 1.55))
        side += side % 2  # 保证中心落在整数像素，缓存图与指针不会半像素错位
        self.resize(side, side)

        # 运动状态：从离鼠标最远的象限出生，避免一启动就被自己的光标吓到
        geo = self._bounds
        cursor = QCursor.pos()
        keep = self._radius * 1.02
        candidates = [
            (geo.left() + geo.width() * fx, geo.top() + geo.height() * fy)
            for fx, fy in ((0.28, 0.30), (0.72, 0.30), (0.28, 0.70), (0.72, 0.70))
        ]
        self._x, self._y = max(
            candidates,
            key=lambda pt: (pt[0] - cursor.x()) ** 2 + (pt[1] - cursor.y()) ** 2,
        )
        self._x = _clamp(self._x, geo.left() + keep, geo.right() - keep)
        self._y = _clamp(self._y, geo.top() + keep, geo.bottom() - keep)
        self._vx = 0.0
        self._vy = 0.0
        self._wander = random.uniform(0.0, TAU)
        self._ph = [random.uniform(0.0, TAU) for _ in range(4)]
        self._t = 0.0
        self._panic = 0.0
        self._paused = False

        # 表情状态
        self._eye_open = 1.0
        self._blinking = False
        self._blink_progress = 1.0
        self._blink_timer = random.uniform(2.0, 5.0)

        # 预览/调试可覆盖
        self._fixed_time: datetime | None = None

        # 静态图层与只读字体，都是固定大小、不会随运行增长
        self._bg: QPixmap | None = None
        self._font_readout = QFont("Consolas")
        self._font_readout.setPixelSize(max(9, int(round(self._radius * 0.135))))
        self._font_readout.setStyleHint(QFont.Monospace)
        self._fm = QFontMetrics(self._font_readout)
        self._w_time = self._fm.horizontalAdvance("00:00:00")
        self._w_speed = self._fm.horizontalAdvance("v=000")
        self._w_mode = self._fm.horizontalAdvance("DRIFT")
        self._gap = self._fm.horizontalAdvance(" ")
        self._pad = max(5, int(round(self._radius * 0.09)))
        self._readout_w = (self._w_time + self._w_speed + self._w_mode
                           + self._gap * 2 + self._pad * 2)
        self._readout_h = self._fm.height() + max(4, int(round(self._radius * 0.10)))

        self.move(round(self._x - side / 2), round(self._y - side / 2))

        self._last = time.monotonic()
        self._frames = 0
        self._started = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(CALM_FRAME_MS)
        self._timer.timeout.connect(self._tick)

    # ---- 生命周期 ---------------------------------------------------------
    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._started:
            return
        self._started = True
        self._apply_native_styles()
        self._last = time.monotonic()
        self._timer.start()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._bg = None

    def _apply_native_styles(self) -> None:
        """补上 Qt 没替我们设置的扩展样式：穿透、不抢焦点、不上任务栏。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = wintypes.HWND(int(self.winId()))
            gwl_exstyle = -20
            ws_ex_layered = 0x00080000
            ws_ex_transparent = 0x00000020
            ws_ex_toolwindow = 0x00000080
            ws_ex_noactivate = 0x08000000

            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
            get_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t

            style = get_long(hwnd, gwl_exstyle)
            style |= (ws_ex_layered | ws_ex_transparent
                      | ws_ex_toolwindow | ws_ex_noactivate)
            set_long(hwnd, gwl_exstyle, style)

            swp_nomove, swp_nosize = 0x0002, 0x0001
            swp_noactivate, swp_showwindow = 0x0010, 0x0040
            user32.SetWindowPos(
                hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
                swp_nomove | swp_nosize | swp_noactivate | swp_showwindow,
            )
        except Exception:
            pass

    # ---- 对外操作 ---------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if self._paused:
            self._vx = self._vy = 0.0

    def is_paused(self) -> bool:
        return self._paused

    def recenter(self) -> None:
        geo = self._bounds
        self._x = geo.center().x()
        self._y = geo.center().y()
        self._vx = self._vy = 0.0
        self._panic = 0.0
        self._place_window()

    def _place_window(self) -> None:
        self.move(round(self._x - self.width() / 2),
                  round(self._y - self.height() / 2))

    # ---- 每帧更新 ---------------------------------------------------------
    def _tick(self) -> None:
        now = time.monotonic()
        dt = _clamp(now - self._last, 0.0, 0.05)
        self._last = now
        self._advance(dt)
        self.update()

    def _advance(self, dt: float) -> None:
        self._t += dt
        self._update_face(dt)

        self._frames += 1
        if self._frames % 60 == 0:
            self._refresh_bounds()

        if self._paused:
            self._sync_frame_rate()
            return

        W = self._bounds.width()
        R = self._radius
        cursor = QCursor.pos()
        dx = self._x - cursor.x()
        dy = self._y - cursor.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-3:
            dx, dy, dist = 1.0, 0.0, 1.0

        # 鼠标靠近 → 恐慌值快速上升、缓慢消退
        zone = R * FLEE_ZONE_R + max(70.0, W * FLEE_ZONE_SCREEN)
        target = _smoothstep(1.0 - dist / zone)
        rate = 12.0 if target > self._panic else 1.35
        self._panic += (target - self._panic) * min(1.0, rate * dt)
        panic = self._panic
        self._sync_frame_rate()

        # 平时：低频噪声让游走方向缓慢打转
        t = self._t
        self._wander += (
            math.sin(t * 0.61 + self._ph[0]) * 0.90
            + math.sin(t * 0.27 + self._ph[1]) * 0.65
            + math.sin(t * 1.90 + self._ph[2]) * 0.20
        ) * dt
        dir_x, dir_y = math.cos(self._wander), math.sin(self._wander)

        if panic > 1e-3:
            # 背离鼠标 + 切向分量，避免顶在墙角
            away_x, away_y = dx / dist, dy / dist
            tx, ty = -away_y, away_x
            if tx * self._vx + ty * self._vy < 0.0:
                tx, ty = -tx, -ty
            curve = 0.55 * math.sin(t * 2.1 + self._ph[0])
            fdx, fdy = away_x + tx * curve, away_y + ty * curve
            fn = math.hypot(fdx, fdy) or 1.0
            fdx, fdy = fdx / fn, fdy / fn
            dir_x = dir_x * (1.0 - panic) + fdx * panic
            dir_y = dir_y * (1.0 - panic) + fdy * panic

        # 边界回避
        left, top = self._bounds.left(), self._bounds.top()
        right, bottom = self._bounds.right(), self._bounds.bottom()
        margin = R * 1.6
        ix = iy = 0.0
        if self._x - left < margin:
            ix += (margin - (self._x - left)) / margin
        if right - self._x < margin:
            ix -= (margin - (right - self._x)) / margin
        if self._y - top < margin:
            iy += (margin - (self._y - top)) / margin
        if bottom - self._y < margin:
            iy -= (margin - (bottom - self._y)) / margin
        if ix or iy:
            n = math.hypot(ix, iy)
            w = 1.4 + 2.2 * panic
            dir_x += ix / n * w
            dir_y += iy / n * w

        n = math.hypot(dir_x, dir_y) or 1.0
        dir_x, dir_y = dir_x / n, dir_y / n

        # 速度预算：巡航 30 秒 ≤ 0.84 屏宽；逃跑是短时例外
        cruise = max(ROAM_MIN, W * ROAM_FRACTION) * (
            0.62 + 0.38 * math.sin(t * 0.15 + self._ph[3])
        )
        flee = _clamp(W * FLEE_FRACTION, FLEE_MIN, FLEE_MAX)
        speed = cruise * (1.0 - panic) + flee * (panic ** 0.85)

        resp = 1.8 + 12.0 * panic
        k = min(1.0, resp * dt)
        self._vx += (dir_x * speed - self._vx) * k
        self._vy += (dir_y * speed - self._vy) * k

        self._x += self._vx * dt
        self._y += self._vy * dt

        keep = R * 1.02
        if self._x < left + keep:
            self._x = left + keep
            self._vx = abs(self._vx) * 0.35
        elif self._x > right - keep:
            self._x = right - keep
            self._vx = -abs(self._vx) * 0.35
        if self._y < top + keep:
            self._y = top + keep
            self._vy = abs(self._vy) * 0.35
        elif self._y > bottom - keep:
            self._y = bottom - keep
            self._vy = -abs(self._vy) * 0.35

        self._place_window()

    def _sync_frame_rate(self) -> None:
        want = PANIC_FRAME_MS if self._panic > 0.15 else CALM_FRAME_MS
        if self._timer.interval() != want:
            self._timer.setInterval(want)

    def _refresh_bounds(self) -> None:
        try:
            self._bounds = QRectF(self._screen.availableGeometry())
        except RuntimeError:
            self._screen = QGuiApplication.primaryScreen()
            self._bounds = QRectF(self._screen.availableGeometry())

    def _update_face(self, dt: float) -> None:
        if self._blinking:
            self._blink_progress += dt / 0.16
            if self._blink_progress >= 1.0:
                self._blinking = False
                self._blink_progress = 1.0
                self._eye_open = 1.0
            else:
                self._eye_open = 1.0 - math.sin(math.pi * self._blink_progress)
        else:
            self._blink_timer -= dt
            if self._blink_timer <= 0.0:
                self._blinking = True
                self._blink_progress = 0.0
                self._blink_timer = random.uniform(3.0, 7.5)

    # ---- 渲染：静态层 -----------------------------------------------------
    def _build_background(self) -> None:
        side = self.width()
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(max(1, round(side * dpr)), max(1, round(side * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)

        p = QPainter(pm)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        c = QPointF(side / 2.0, side / 2.0)
        self._paint_specimen(p, c, self._radius)
        p.end()
        self._bg = pm

    def _paint_specimen(self, p: QPainter, c: QPointF, R: float) -> None:
        body = _body_path(c, R)

        # 可选玻璃底；默认 0，即完全透明。设为非 0 可恢复淡玻璃盘
        if GLASS_CENTER_ALPHA or GLASS_EDGE_ALPHA:
            mid_alpha = (GLASS_CENTER_ALPHA + GLASS_EDGE_ALPHA) // 2
            fill = QRadialGradient(
                QPointF(c.x() - R * 0.30, c.y() - R * 0.35), R * 1.55)
            fill.setColorAt(0.0, QColor(16, 34, 44, GLASS_CENTER_ALPHA))
            fill.setColorAt(0.60, QColor(8, 18, 26, mid_alpha))
            fill.setColorAt(1.0, QColor(3, 8, 13, GLASS_EDGE_ALPHA))
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawPath(body)

        # 内部极坐标网格：不画 1.0R 外圈，避免又形成一圈“边框”
        p.save()
        p.setClipPath(body)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_rgba(MID_TEAL, 30), 1.0))
        for rr in (0.25, 0.50):
            p.drawEllipse(c, R * rr, R * rr)
        for deg in range(0, 360, 30):
            a = math.radians(deg)
            p.drawLine(_polar(c, R * 0.12, a), _polar(c, R * 0.690, a))
        p.setPen(QPen(_rgba(MID_TEAL, 48), 1.0))
        p.drawLine(QPointF(c.x() - R * 0.715, c.y()),
                   QPointF(c.x() + R * 0.715, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - R * 0.715),
                   QPointF(c.x(), c.y() + R * 0.715))
        p.restore()

        # 可选度盘淡色底；默认 0
        if DIAL_ALPHA:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(3, 9, 14, DIAL_ALPHA))
            p.drawEllipse(c, R * 0.755, R * 0.755)

        # 内圈 + 秒轨道：唯一的圆形参考线，不再画外框
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_rgba(MID_TEAL, 50), 1.0))
        p.drawEllipse(c, R * 0.600, R * 0.600)

        # 刻度：大刻度用深色描边 + 亮芯，小刻度用中调青色
        for i in range(60):
            major = i % 5 == 0
            cardinal = i % 15 == 0
            if cardinal:
                inner, outer = R * 0.630, R * 0.735
                col, width = QColor(220, 244, 244, 240), R * 0.020
            elif major:
                inner, outer = R * 0.665, R * 0.735
                col, width = QColor(200, 236, 236, 190), R * 0.013
            else:
                inner, outer = R * 0.700, R * 0.735
                col, width = _rgba(MID_TEAL, 135), R * 0.007
            a = i * TAU / 60.0
            p1, p2 = _polar(c, inner, a), _polar(c, outer, a)
            if major:
                p.setPen(QPen(_rgba(OUTLINE, 165), max(1.4, width + 1.8),
                              Qt.SolidLine, Qt.RoundCap))
                p.drawLine(p1, p2)
            p.setPen(QPen(col, max(0.6, width), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(p1, p2)

        # 12 点方向的定位缺口
        notch = QPainterPath()
        notch.moveTo(c.x() - R * 0.020, c.y() - R * 0.775)
        notch.lineTo(c.x() + R * 0.020, c.y() - R * 0.775)
        notch.lineTo(c.x(), c.y() - R * 0.720)
        notch.closeSubpath()
        p.setPen(QPen(OUTLINE, max(1.2, R * 0.014)))
        p.setBrush(QColor(208, 238, 240, 235))
        p.drawPath(notch)

    # ---- 渲染：每帧 -------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        if self._bg is None:
            self._build_background()

        now = self._fixed_time or datetime.now()
        hour_col, minute_col, second_col, _sec, _minute, _hour = _hand_colors(now)
        R = self._radius
        side = self.width()

        tremble = R * 0.010 * self._panic
        bob = math.sin(self._t * 1.5) * R * 0.010
        jx = math.sin(self._t * 33.0) * tremble
        jy = math.cos(self._t * 41.0) * tremble
        cx = int(round(side / 2.0 + jx))
        cy = int(round(side / 2.0 + bob + jy))

        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.translate(cx, cy)

        self._draw_antennae(p, R, second_col)
        self._draw_cilia(p, R, second_col)
        p.drawPixmap(-side // 2, -side // 2, self._bg)

        self._draw_mouth(p, R)
        self._draw_eyes(p, R, minute_col)
        self._draw_hands(p, R, hour_col, minute_col, second_col, now)
        self._draw_readout(p, R, now)
        p.end()

    def _draw_antennae(self, p: QPainter, R: float, second_col: QColor) -> None:
        t = self._t
        for side in (-1.0, 1.0):
            base = QPointF(side * R * 0.28, -R * 0.72)
            sway = math.sin(t * 2.0 + side) * R * (0.018 + 0.030 * self._panic)
            tip = QPointF(side * R * 0.52 + sway, -R * 1.24)
            ctrl = QPointF(side * R * 0.42, -R * 1.12)
            path = QPainterPath(base)
            path.quadTo(ctrl, tip)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_rgba(MID_TEAL, 230), max(1.1, R * 0.020),
                          Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
            # 传感器灯：颜色跟随秒针，靠近时变亮
            lamp = _rgba(second_col, 220 if self._panic > 0.2 else 180)
            p.setPen(Qt.NoPen)
            p.setBrush(_rgba(lamp, 40))
            p.drawEllipse(tip, R * 0.045, R * 0.045)
            p.setBrush(_rgba(OUTLINE, 210))
            p.drawEllipse(tip, R * 0.030, R * 0.030)
            p.setBrush(lamp)
            p.drawEllipse(tip, R * 0.021, R * 0.021)

    def _draw_cilia(self, p: QPainter, R: float, second_col: QColor) -> None:
        t = self._t
        col = _mix(_rgba(MID_TEAL, 225), _rgba(second_col, 210),
                   0.25 + 0.50 * self._panic)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(col, max(1.2, R * 0.040), Qt.SolidLine, Qt.RoundCap))
        for i in range(5):
            k = (i - 2) / 2.0
            root = QPointF(k * R * 0.32, R * 0.75)
            sway = math.sin(t * 3.0 + i * 1.25) * R * (0.022 + 0.050 * self._panic)
            tip = QPointF(k * R * 0.44 + sway, R * 1.11)
            ctrl = QPointF(k * R * 0.42 + sway * 0.4, R * 1.00)
            path = QPainterPath(root)
            path.quadTo(ctrl, tip)
            p.drawPath(path)

    def _draw_mouth(self, p: QPainter, R: float) -> None:
        base_y = R * 0.365
        half = R * 0.165
        p.setPen(QPen(_rgba(MID_TEAL, 85), 1.0))
        p.drawLine(QPointF(-half, base_y), QPointF(half, base_y))
        p.drawLine(QPointF(-half, base_y - R * 0.014),
                   QPointF(-half, base_y + R * 0.014))
        p.drawLine(QPointF(half, base_y - R * 0.014),
                   QPointF(half, base_y + R * 0.014))

        amp = R * (0.010 + 0.046 * self._panic)
        phase = self._t * (2.2 + 6.5 * self._panic)
        path = QPainterPath()
        n = 25
        for i in range(n):
            x = -half + 2.0 * half * i / (n - 1)
            u = x / R
            y = base_y + amp * (
                math.sin(phase + u * 11.0)
                + 0.38 * math.sin(phase * 1.3 + u * 23.0)
            )
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        col = _mix(CYAN, AMBER, self._panic)
        width = max(1.3, R * 0.020)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_rgba(OUTLINE, 150), width + 2.0,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.setPen(QPen(_rgba(col, 230), width,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)

    def _draw_eyes(self, p: QPainter, R: float, minute_col: QColor) -> None:
        cursor = QCursor.pos()
        if self._panic >= 0.35:
            lx, ly = cursor.x() - self._x, cursor.y() - self._y
        else:
            lx, ly = self._vx, self._vy
        ln = math.hypot(lx, ly)
        if ln < 1e-3:
            lx, ly, ln = 0.0, -1.0, 1.0
        lx, ly = lx / ln, ly / ln

        eye_dx = R * 0.255
        eye_y = -R * 0.270
        eye_r = R * 0.105 * (1.0 + 0.08 * self._panic)
        look = eye_r * 0.30
        open_ = self._eye_open

        for side in (-1.0, 1.0):
            ec = QPointF(side * eye_dx, eye_y)
            if open_ < 0.18:
                line_start = QPointF(ec.x() - eye_r * 0.9, ec.y())
                line_end = QPointF(ec.x() + eye_r * 0.9, ec.y())
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(_rgba(OUTLINE, 210), max(2.0, R * 0.026),
                              Qt.SolidLine, Qt.RoundCap))
                p.drawLine(line_start, line_end)
                p.setPen(QPen(QColor(206, 238, 240, 230),
                              max(1.0, R * 0.012), Qt.SolidLine, Qt.RoundCap))
                p.drawLine(line_start, line_end)
                continue

            # 镜筒：浅色巩膜 + 深色描边 + 外侧青色细环
            p.setBrush(QColor(214, 238, 240, 240))
            p.setPen(QPen(_rgba(OUTLINE, 225), max(1.4, R * 0.020)))
            p.drawEllipse(ec, eye_r * 0.95, eye_r * open_)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_rgba(CYAN, 150), max(0.6, R * 0.006)))
            p.drawEllipse(ec, eye_r * 1.04, eye_r * 1.04 * open_)

            px, py = ec.x() + lx * look, ec.y() + ly * look
            iris = eye_r * 0.56 * (1.0 - 0.15 * self._panic)
            squash = max(0.35, open_)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(12, 58, 66, 245))
            p.drawEllipse(QPointF(px, py), iris, iris * squash)
            p.setBrush(_rgba(minute_col, 200))
            p.setPen(QPen(_rgba(minute_col, 190), max(0.6, R * 0.007)))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(px, py), iris * 0.72, iris * 0.72 * squash)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(3, 8, 11, 250))
            p.drawEllipse(QPointF(px, py), iris * 0.40, iris * 0.40 * squash)
            p.setBrush(QColor(255, 255, 255, 225))
            hl = max(0.5, R * 0.010)
            p.drawEllipse(QPointF(px - iris * 0.28, py - iris * 0.32), hl, hl)

    def _draw_hands(self, p: QPainter, R: float, hour_col: QColor,
                    minute_col: QColor, second_col: QColor,
                    now: datetime) -> None:
        sec = now.second + now.microsecond / 1_000_000.0
        minute = now.minute + sec / 60.0
        hour = (now.hour % 12) + minute / 60.0
        hour_a = (hour / 12.0) * TAU
        minute_a = (minute / 60.0) * TAU
        second_a = (sec / 60.0) * TAU

        frac = sec % 1.0
        spring = math.exp(-frac * 4.5) * math.sin(frac * 26.0) * math.radians(0.8)
        wobble = math.radians(0.35 + 2.8 * self._panic)

        specs = (
            (hour_a, R * 0.405, R * 0.036, hour_col, False),
            (minute_a, R * 0.585, R * 0.024, minute_col, False),
            (second_a, R * 0.700, R * 0.013, second_col, True),
        )
        for idx, (angle, length, half_w, col, is_second) in enumerate(specs):
            a = angle + math.sin(self._t * 1.9 + idx * 1.7) * wobble
            if is_second:
                a += spring
                # 秒针尾巴 + 尾端小环（双层，浅色桌面上也看得见）
                tail = _polar(QPointF(0.0, 0.0), R * 0.150, a + math.pi)
                tail_w = max(1.2, R * 0.012)
                p.setPen(QPen(_rgba(OUTLINE, 170), tail_w + 2.0,
                              Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(0.0, 0.0), tail)
                p.setPen(QPen(_rgba(col, 215), tail_w,
                              Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(0.0, 0.0), tail)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(_rgba(OUTLINE, 200), max(1.6, R * 0.010)))
                p.drawEllipse(tail, R * 0.026, R * 0.026)
                p.setPen(QPen(_rgba(col, 205), max(0.8, R * 0.007)))
                p.drawEllipse(tail, R * 0.026, R * 0.026)
            bend = (math.sin(self._t * 1.7 + idx * 2.0) * 0.010
                    + math.sin(self._t * 6.5 + idx) * 0.014 * self._panic)
            _stroke_hand(p, QPointF(0.0, 0.0), a, length, half_w, col,
                         bend=bend, halo=is_second, outline=True)

        # 中心轴：深色外圈 + 亮芯
        p.setPen(QPen(_rgba(OUTLINE, 230), max(1.2, R * 0.012)))
        p.setBrush(QColor(5, 12, 17, 240))
        p.drawEllipse(QPointF(0.0, 0.0), R * 0.052, R * 0.052)
        p.setPen(Qt.NoPen)
        p.setBrush(ICE)
        p.drawEllipse(QPointF(0.0, 0.0), R * 0.017, R * 0.017)

    def _draw_readout(self, p: QPainter, R: float, now: datetime) -> None:
        if self._paused:
            mode, mode_col = "HOLD", HOLD_GRAY
        elif self._panic > 0.15:
            mode, mode_col = "EVADE", AMBER
        else:
            mode, mode_col = "DRIFT", CYAN

        speed = int(round(math.hypot(self._vx, self._vy)))
        time_text = f"{now.hour:02d}:{now.minute:02d}:{now.second:02d}"
        speed_text = f"v={min(999, speed):03d}"

        y = R * 1.315
        w, h = self._readout_w, self._readout_h
        if READOUT_ALPHA:
            pill = QRectF(-w / 2.0, y - h / 2.0, w, h)
            p.setBrush(QColor(4, 10, 16, READOUT_ALPHA))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(pill, h / 2.0, h / 2.0)

        p.setFont(self._font_readout)
        x = -w / 2.0 + self._pad
        baseline = y + self._fm.ascent() - self._fm.height() / 2.0

        def label(text: str, color: QColor, xpos: float) -> None:
            # 无底板时靠深色阴影保证浅色壁纸上也能读
            p.setPen(QColor(2, 7, 11, 175))
            p.drawText(QPointF(xpos + 1, baseline + 1), text)
            p.setPen(color)
            p.drawText(QPointF(xpos, baseline), text)

        label(time_text, _rgba(ICE, 240), x)
        x += self._w_time + self._gap
        label(speed_text, QColor(190, 225, 225, 200), x)
        x += self._w_speed + self._gap
        label(mode, _rgba(mode_col, 245), x)


# -----------------------------------------------------------------------------
# 托盘图标 / 启动
# -----------------------------------------------------------------------------
def _make_tray_icon() -> QIcon:
    icon = QIcon()
    for size in (32, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)

        now = datetime.now()
        hour_col, minute_col, second_col, sec, _m, _h = _hand_colors(now)
        c = QPointF(size / 2.0, size / 2.0)
        R = size * 0.44
        p.setPen(QPen(QColor(140, 232, 226, 150), max(0.8, size * 0.014)))
        p.setBrush(QColor(5, 12, 18, 250))
        p.drawEllipse(c, R, R)

        for i in range(12):
            a = i * TAU / 12.0
            p.setPen(QPen(QColor(208, 238, 240, 160 if i % 3 == 0 else 80),
                          max(0.6, size * (0.011 if i % 3 == 0 else 0.006)),
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(_polar(c, R * 0.78, a), _polar(c, R * 0.92, a))

        _stroke_hand(p, c, (sec / 60.0) * TAU, R * 0.62, size * 0.010,
                     second_col, halo=False, outline=False)
        h = (now.hour % 12) + now.minute / 60.0
        _stroke_hand(p, c, h / 12.0 * TAU, R * 0.38, size * 0.024,
                     hour_col, halo=False, outline=False)
        _stroke_hand(p, c, (now.minute / 60.0) * TAU, R * 0.52, size * 0.016,
                     minute_col, halo=False, outline=False)
        p.setPen(Qt.NoPen)
        p.setBrush(ICE)
        p.drawEllipse(c, size * 0.030, size * 0.030)
        p.end()
        icon.addPixmap(pm)
    return icon


def _build_tray(app: QApplication, creature: ClockCreature):
    tray = QSystemTrayIcon(_make_tray_icon(), app)
    tray.setToolTip("Chrono-Organism 时钟生物：鼠标靠近会逃跑；右键这里管理")
    menu = QMenu()

    pause = QAction("暂停游走", menu, checkable=True)
    pause.toggled.connect(creature.set_paused)
    menu.addAction(pause)

    recenter = QAction("回到屏幕中央", menu)
    recenter.triggered.connect(creature.recenter)
    menu.addAction(recenter)

    menu.addSeparator()
    quit_action = QAction("退出", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.show()
    return tray, menu, pause


def _write_crash_log() -> Path | None:
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
        path = base / APP_NAME / "error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(traceback.format_exc(), encoding="utf-8")
        return path
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CHRONO-ORGANISM：科研仪器风格的桌面时钟生物，透明、置顶、"
                    "鼠标穿透、会躲鼠标。")
    parser.add_argument("--size", type=int, default=None,
                        help="表盘直径（逻辑像素）；默认按屏幕短边自动取值")
    parser.add_argument("--no-tray", action="store_true",
                        help="不创建托盘图标（仅调试；此时只能在终端 Ctrl+C 退出）")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(_make_tray_icon())

    creature = ClockCreature(diameter=args.size)
    creature.show()

    tray = None
    if args.no_tray:
        print("已跳过托盘图标；按 Ctrl+C 退出。", file=sys.stderr)
    elif QSystemTrayIcon.isSystemTrayAvailable():
        tray, _menu, _pause = _build_tray(app, creature)
        app.aboutToQuit.connect(tray.hide)
    else:
        print("系统托盘不可用：请用 Ctrl+C 结束进程。", file=sys.stderr)

    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log = _write_crash_log()
        if log:
            print(f"启动失败，错误已写入：{log}", file=sys.stderr)
        raise
