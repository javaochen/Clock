#!/usr/bin/env python3
"""运行期基准：运动预算 + 逃跑响应 + CPU/内存（Windows）。

用法：
    python tools/bench_runtime.py [--seconds 5]

会做两件事：
1. 不显示窗口，模拟 30 秒巡航，检查路程是否 ≤ 一个屏宽；再把虚拟鼠标放到
   旁边，检查恐慌值和逃离距离；
2. 真机显示窗口 N 秒，统计帧率、单帧绘制耗时、单核 CPU 占比，以及工作集/
   私有字节的起止值（用来确认运行期没有明显增长）。

注意：第 2 步会短暂在屏幕中央显示时钟生物。
"""

from __future__ import annotations

import argparse
import ctypes
import math
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QPointF, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import clock_creature as cc  # noqa: E402


# --- Windows 进程内存 --------------------------------------------------------
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def process_memory() -> tuple[float, float, float] | None:
    """返回 (工作集 MB, 私有字节 MB, 峰值工作集 MB)。"""
    if sys.platform != "win32":
        return None
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return None
    mb = 1024.0 * 1024.0
    return (counters.WorkingSetSize / mb,
            counters.PagefileUsage / mb,
            counters.PeakWorkingSetSize / mb)


class _FakeCursor:
    pos_value = QPoint(0, 0)

    @classmethod
    def pos(cls) -> QPoint:
        return cls.pos_value


def motion_checks(app: QApplication) -> tuple[bool, list[str]]:
    lines: list[str] = []
    real_cursor = cc.QCursor
    cc.QCursor = _FakeCursor
    try:
        creature = cc.ClockCreature()
        bounds = creature._bounds
        creature._x = bounds.center().x()
        creature._y = bounds.center().y()
        creature._vx = creature._vy = 0.0

        # 让鼠标离得很远，避免触发逃跑
        _FakeCursor.pos_value = QPoint(
            int(bounds.left() - 2000), int(bounds.top() - 2000))

        start = QPointF(creature._x, creature._y)
        prev = QPointF(start)
        path = 0.0
        dt = 1.0 / 30.0
        for _ in range(30 * 30):
            creature._advance(dt)
            cur = QPointF(creature._x, creature._y)
            path += math.hypot(cur.x() - prev.x(), cur.y() - prev.y())
            prev = cur

        width = bounds.width()
        ok_path = path <= width + 1.0
        lines.append(
            f"cruise path 30s = {path:7.1f} px  (screen {width:.0f} px, "
            f"budget {width:.0f})  {'PASS' if ok_path else 'FAIL'}")

        # 逃跑：把鼠标放到旁边 12 px 处，跑 1.2 秒
        creature._x = bounds.center().x()
        creature._y = bounds.center().y()
        creature._vx = creature._vy = 0.0
        creature._panic = 0.0
        _FakeCursor.pos_value = QPoint(creature._x + 12, creature._y)
        d0 = 12.0
        dmax = d0
        panic_max = 0.0
        for _ in range(int(1.2 * 60)):
            creature._advance(1.0 / 60.0)
            d = math.hypot(creature._x - _FakeCursor.pos_value.x(),
                           creature._y - _FakeCursor.pos_value.y())
            dmax = max(dmax, d)
            panic_max = max(panic_max, creature._panic)
        ok_flee = panic_max > 0.8 and dmax > 40.0
        lines.append(
            f"flee response: max panic = {panic_max:.2f}, "
            f"distance {d0:.0f} -> {dmax:.0f} px  "
            f"{'PASS' if ok_flee else 'FAIL'}")
        return ok_path and ok_flee, lines
    finally:
        cc.QCursor = real_cursor


class _TimedCreature(cc.ClockCreature):
    def __init__(self):
        super().__init__()
        self.paint_time = 0.0
        self.paint_frames = 0

    def paintEvent(self, event):  # noqa: N802
        t0 = time.perf_counter()
        super().paintEvent(event)
        self.paint_time += time.perf_counter() - t0
        self.paint_frames += 1


def gui_bench(app: QApplication, seconds: float) -> list[str]:
    creature = _TimedCreature()
    creature.show()
    cpu0 = time.process_time()
    t0 = time.monotonic()
    samples: list[tuple[float, float, float]] = []
    result: list[str] = []

    def sample():
        mem = process_memory()
        if mem:
            samples.append((time.monotonic() - t0, mem[0], mem[1]))

    QTimer.singleShot(1000, sample)

    def finish():
        wall = time.monotonic() - t0
        cpu = time.process_time() - cpu0
        sample()
        result.append(
            f"frames = {creature.paint_frames} in {wall:.1f}s "
            f"({creature.paint_frames / wall:.1f} fps), "
            f"paint = {creature.paint_time / max(1, creature.paint_frames) * 1000:.2f} ms/frame")
        result.append(f"cpu = {cpu / wall * 100:.1f}% of one core")
        if samples:
            first, last = samples[0], samples[-1]
            peak = (process_memory() or (0, 0, 0))[2]
            result.append(
                f"working set = {first[1]:.1f} -> {last[1]:.1f} MB "
                f"(peak {peak:.1f} MB)")
            result.append(
                f"private bytes = {first[2]:.1f} -> {last[2]:.1f} MB")
        app.quit()

    QTimer.singleShot(int(seconds * 1000), finish)
    app.exec()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrono-Organism 运行期基准")
    parser.add_argument("--seconds", type=float, default=5.0,
                        help="真机显示窗口的秒数，默认 5")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)

    ok, motion_lines = motion_checks(app)
    print("== motion ==")
    for line in motion_lines:
        print("  " + line)

    print("== runtime ==")
    # 固定一个很远的虚拟鼠标，测的是「巡航」稳态；否则光标恰好靠近会切到 60fps。
    real_cursor = cc.QCursor
    cc.QCursor = _FakeCursor
    _FakeCursor.pos_value = QPoint(-5000, -5000)
    try:
        for line in gui_bench(app, max(1.0, args.seconds)):
            print("  " + line)
    finally:
        cc.QCursor = real_cursor

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
