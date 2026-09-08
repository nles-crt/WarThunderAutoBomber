"""
Xbox 360 键盘摇杆模拟器
pynput 全局键盘钩子，后台运行
"""
import vgamepad as vg
from pynput import keyboard
import threading
import time

# ============================================================
# 引擎
# ============================================================
class Engine:
    def __init__(self):
        self.g = vg.VX360Gamepad()
        self.running = True
        self._lock = threading.Lock()
        # 轴值
        self.lx = self.ly = self.rx = self.ry = 0.0
        self.lt = self.rt = 0.0
        self._stick = {}
        # 后台刷新
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            with self._lock:
                try: self.g.update()
                except: pass
            time.sleep(0.008)

    def _upd(self):
        with self._lock:
            try: self.g.update()
            except: pass

    def _apply(self):
        with self._lock:
            self.g.left_joystick_float(self.lx, self.ly)
            self.g.right_joystick_float(self.rx, self.ry)
            self.g.left_trigger_float(self.lt)
            self.g.right_trigger_float(self.rt)

    def btn(self, code, down):
        (self.g.press_button if down else self.g.release_button)(code)
        self._upd()

    def axis(self, name, val):
        if   name == 'lx': self.lx = val
        elif name == 'ly': self.ly = val
        elif name == 'rx': self.rx = val
        elif name == 'ry': self.ry = val
        elif name == 'lt': self.lt = val
        elif name == 'rt': self.rt = val
        self._apply()
        self._upd()

    def stop(self):
        self.running = False
        self.g.reset()
        try: self.g.update()
        except: pass


# ============================================================
# 全局变量
# ============================================================
eng = Engine()
X = vg.XUSB_BUTTON
_held = {}   # name -> True
_stick_state = {}

# ============================================================
# 按键映射
# ============================================================
# { key_name: (press_func, release_func) }
KEY_MAP = {}

def bind(name, code):
    """注册按钮映射"""
    KEY_MAP[name] = (
        lambda c=code: eng.btn(c, True),
        lambda c=code: eng.btn(c, False),
    )

# 动作
bind('z', X.XUSB_GAMEPAD_A)
bind('x', X.XUSB_GAMEPAD_B)
bind('c', X.XUSB_GAMEPAD_X)
bind('v', X.XUSB_GAMEPAD_Y)
# 肩键
bind('t', X.XUSB_GAMEPAD_LEFT_SHOULDER)
bind('g', X.XUSB_GAMEPAD_RIGHT_SHOULDER)
# D-Pad
bind('i', X.XUSB_GAMEPAD_DPAD_UP)
bind('k', X.XUSB_GAMEPAD_DPAD_DOWN)
bind('j', X.XUSB_GAMEPAD_DPAD_LEFT)
bind('l', X.XUSB_GAMEPAD_DPAD_RIGHT)
# 系统
bind('1', X.XUSB_GAMEPAD_BACK)
bind('2', X.XUSB_GAMEPAD_START)
bind('3', X.XUSB_GAMEPAD_GUIDE)
bind('4', X.XUSB_GAMEPAD_LEFT_THUMB)
bind('5', X.XUSB_GAMEPAD_RIGHT_THUMB)


def handle_press(key):
    """按键按下"""
    # 获取键名
    try:
        name = key.char      # 字母/数字键
    except AttributeError:
        name = key.name      # 特殊键 (esc, up, down 等)

    if name in _held:
        return
    _held[name] = True

    # 按钮映射
    if name in KEY_MAP:
        KEY_MAP[name][0]()
        return

    # 左摇杆 WASD
    STICK_L = {'w': 'ly', 's': 'ly', 'a': 'lx', 'd': 'lx'}
    if name in STICK_L:
        val = 1.0 if name in ('w', 'd') else -1.0
        _stick_state[name] = (STICK_L[name], val)
        _apply_stick()
        return

    # 右摇杆 方向键
    STICK_R = {'up': 'ry', 'down': 'ry', 'left': 'rx', 'right': 'rx'}
    if name in STICK_R:
        val = 1.0 if name in ('up', 'right') else -1.0
        _stick_state[name] = (STICK_R[name], val)
        _apply_stick()
        return

    # 扳机
    if name == 'q': eng.axis('lt', 1.0)
    elif name == 'e': eng.axis('rt', 1.0)


def handle_release(key):
    """按键松开"""
    try:
        name = key.char
    except AttributeError:
        name = key.name

    _held.pop(name, None)

    # 按钮松开
    if name in KEY_MAP:
        KEY_MAP[name][1]()
        return

    # 摇杆松开 (WASD / 方向键)
    if name in ('w','s','a','d','up','down','left','right'):
        _stick_state.pop(name, None)
        _apply_stick()
        return

    # 扳机松开
    if name == 'q': eng.axis('lt', 0.0)
    elif name == 'e': eng.axis('rt', 0.0)


def _apply_stick():
    """计算当前摇杆值"""
    ax = {'lx': 0.0, 'ly': 0.0, 'rx': 0.0, 'ry': 0.0}
    for axis, val in _stick_state.values():
        ax[axis] += val
    for a, v in ax.items():
        eng.axis(a, v)


# ============================================================
# 状态显示
# ============================================================
def status_loop():
    while eng.running:
        e = eng
        print(f"\r  LX:{e.lx:+5.2f} LY:{e.ly:+5.2f}  RX:{e.rx:+5.2f} RY:{e.ry:+5.2f}  LT:{e.lt:.2f} RT:{e.rt:.2f}  ", end='', flush=True)
        time.sleep(0.15)


# ============================================================
# 主程序
# ============================================================
def main():
    print("=" * 52)
    print("  Xbox 360 键盘摇杆模拟器")
    print("  全局热键，后台运行")
    print("=" * 52)
    print()
    print("  【摇杆】   W/A/S/D = 左摇杆    ↑/↓/←/→ = 右摇杆")
    print("  【扳机】   Q = LT  |  E = RT")
    print("  【动作】   Z = A   X = B   C = X   V = Y")
    print("  【肩键】   T = LB  |  G = RB")
    print("  【D-Pad】  I/J/K/L = 上/左/下/右")
    print("  【系统】   1=Back  2=Start  3=Guide  4=LS  5=RS")
    print()
    print("  ESC = 退出程序")
    print("  ────────────────────────────────────────────")

    # 状态显示
    threading.Thread(target=status_loop, daemon=True).start()

    # 启动 pynput 监听器 (阻塞)
    with keyboard.Listener(
        on_press=handle_press,
        on_release=handle_release,
    ) as listener:
        listener.join()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n\n  正在清理...")
        eng.stop()
        print("  已退出\n")
