"""War Thunder 自动轰炸 — 入口

启动后常驻后台，自动检测游戏窗口 → 识别场景 → 追踪战区 → 投弹。
Ctrl+C 或进程终止时自动清理虚拟手柄。
"""

import sys
import time
import atexit
import signal
from common.gamepad import cleanup_all
from app import App


def _signal_handler(signum, frame):
    cleanup_all()
    sys.exit(0)


atexit.register(cleanup_all)
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
try:
    signal.signal(signal.SIGBREAK, _signal_handler)
except Exception:
    pass

if __name__ == "__main__":
    app = App()
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        app.log("用户中断")
        app.stop()
