#!/usr/bin/env python3
"""渲染时钟生物的静帧预览（离屏，不弹窗），用于检查配色与表情。

用法：
    python tools/preview.py [输出路径]

生成 docs/preview.png：左侧深色壁纸展示逃跑态，右侧浅色壁纸展示安静态，
方便检查透明盘面、粗指针和描边在两种背景上的可读性。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QFont, QFontDatabase, QImage, QLinearGradient, QPainter,
    QPainterPath, QRadialGradient,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from clock_creature import ClockCreature  # noqa: E402

W, H = 1040, 560
FONT_FAMILY = "Microsoft YaHei UI"


def ui_font(size: float, bold: bool = False) -> QFont:
    font = QFont(FONT_FAMILY, int(size))
    if bold:
        font.setBold(True)
    return font


def render_creature(creature: ClockCreature) -> QImage:
    img = QImage(creature.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    creature.render(img)
    return img


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "preview.png"
    app = QApplication([])  # noqa: F841

    # 离屏平台默认不带中文字体，手动加载系统雅黑
    global FONT_FAMILY
    for candidate in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
        if not Path(candidate).exists():
            continue
        fid = QFontDatabase.addApplicationFont(candidate)
        if fid >= 0:
            families = QFontDatabase.applicationFontFamilies(fid)
            if families:
                FONT_FAMILY = families[0]
                break

    specs = [
        {
            "caption": "鼠标靠近：瞪眼、警报读数、快速逃开",
            "time": datetime(2026, 10, 25, 22, 47, 12, 780000),
            "panic": 1.0,
            "vel": QPointF(620.0, -260.0),
            "center": (270, 300),
        },
        {
            "caption": "安静：缓慢游走、偶尔眨眼",
            "time": datetime(2026, 10, 25, 10, 9, 36, 420000),
            "panic": 0.0,
            "vel": QPointF(-42.0, -18.0),
            "center": (770, 300),
        },
    ]

    canvas = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)

    # 左侧深色 / 右侧浅色，顺便检查指针在两种壁纸上的对比度
    bg = QLinearGradient(0, 0, W, H)
    bg.setColorAt(0.00, QColor(20, 28, 44))
    bg.setColorAt(0.48, QColor(38, 52, 74))
    bg.setColorAt(0.52, QColor(214, 220, 228))
    bg.setColorAt(1.00, QColor(242, 244, 247))
    p.fillRect(QRectF(0, 0, W, H), bg)

    # 壁纸上几块模糊的「其他窗口」，用来确认透明与光晕效果
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 14))
    p.drawRoundedRect(QRectF(40, 60, 360, 120), 12, 12)
    p.drawRoundedRect(QRectF(80, 440, 300, 80), 12, 12)
    p.setBrush(QColor(0, 0, 0, 16))
    p.drawRoundedRect(QRectF(600, 70, 400, 140), 12, 12)
    p.drawRoundedRect(QRectF(650, 430, 320, 90), 12, 12)

    # 顶部标题
    p.setPen(QColor(255, 255, 255, 210))
    title_font = ui_font(15, bold=True)
    p.setFont(title_font)
    p.drawText(QRectF(40, 16, 700, 30), Qt.AlignLeft | Qt.AlignVCenter,
               "CHRONO-ORGANISM · 静帧预览")

    for spec in specs:
        creature = ClockCreature(diameter=180)
        creature._fixed_time = spec["time"]
        creature._panic = spec["panic"]
        creature._vx = spec["vel"].x()
        creature._vy = spec["vel"].y()
        creature._t = 12.0 if spec["panic"] else 3.0
        creature._eye_open = 1.0

        img = render_creature(creature)
        cx, cy = spec["center"]
        p.drawImage(QPointF(cx - img.width() / 2, cy - img.height() / 2), img)

        dark = cx < W / 2
        p.setPen(QColor(255, 255, 255, 225) if dark else QColor(30, 40, 55, 225))
        p.setFont(ui_font(11))
        p.drawText(QRectF(cx - 240, cy + 155, 480, 28),
                   Qt.AlignHCenter | Qt.AlignVCenter, spec["caption"])

        if spec["panic"]:
            # 画一个示意光标，说明「被什么吓到」
            mx, my = cx + 150, cy + 110
            glow = QRadialGradient(QPointF(mx, my), 34)
            glow.setColorAt(0.0, QColor(255, 255, 255, 90))
            glow.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(QPointF(mx, my), 34, 34)
            p.setBrush(QColor(20, 26, 36, 235))
            p.setPen(QColor(255, 255, 255, 230))
            cursor_path = QPainterPath()
            cursor_path.moveTo(mx, my)
            cursor_path.lineTo(mx + 1, my + 25)
            cursor_path.lineTo(mx + 8, my + 18)
            cursor_path.lineTo(mx + 16, my + 31)
            cursor_path.lineTo(mx + 22, my + 27)
            cursor_path.lineTo(mx + 14, my + 15)
            cursor_path.lineTo(mx + 23, my + 12)
            cursor_path.closeSubpath()
            p.drawPath(cursor_path)

    p.end()
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out), "PNG")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
