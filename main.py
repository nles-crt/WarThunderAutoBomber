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
        # PyQt5 必须在 from app import App 之后再导入：app 会先加载 cv2，
        # 两者都自带 Qt 插件，先 cv2 后 Qt 才不会撞平台插件。
        from gui import run_gui
    except ImportError as e:
        app.log(f"未安装 PyQt5（{e}），以无界面模式运行（pip install PyQt5 可启用 GUI）")
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            app.log("用户中断")
            app.stop()
    else:
        try:
            run_gui(app)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            # GUI 崩了不能让机器人跟着停 —— 后台线程全是 daemon，
            # 这里回退成常驻空转，机器人照常跑。
            app.log(f"GUI 异常退出: {e}，转为无界面模式")
            try:
                while True:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
        finally:
            app.log("用户中断")
            app.stop()
