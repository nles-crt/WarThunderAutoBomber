"""虚拟 Xbox 手柄封装

提供手柄初始化、按钮/摇杆操作、进程退出时自动清理。
War Thunder 按键映射（来自 战争雷霆自动炸战区按键.blk）:
  ID_BOMBS_SERIES    = LB + X   (joyButton 8 + 14)
  ID_CAMERA_BOMBVIEW  = Y        (joyButton 15)
  ID_TOGGLE_VIEW       = RB       (joyButton 9)
  ID_BAY_DOOR         = LT + RT  (joyButton 16 + 17)
  ID_CONTINUE         = A        (joyButton 12)
  ID_EJECT            = DPAD_UP  (joyButton 20)
"""

import time
from typing import Optional

try:
    import vgamepad as vg

    VGAME_OK = True
except ImportError:
    vg = None
    VGAME_OK = False

_gpad_ref: Optional["Gamepad"] = None


class Gamepad:
    """虚拟 Xbox 360 手柄"""

    def __init__(self):
        self._g = vg.VX360Gamepad()
        # 总开关（F12）。置 False 后所有飞行输出被丢弃 —— 这是整个程序里
        # 唯一的手柄输出汇聚点，所以急停只需要在这一个类上做闸门，
        # 不必去改 app.py 里几十个 gpad.tap(...) 调用。
        self.enabled = True
        global _gpad_ref
        _gpad_ref = self

    def set_enabled(self, on: bool):
        """总开关。关闭时先把摇杆/扳机复位再置位，避免飞机卡在满舵上。"""
        if not on and self.enabled:
            self.halt()
        self.enabled = bool(on)

    @staticmethod
    def _btn_name(button) -> str:
        names = {
            vg.XUSB_BUTTON.XUSB_GAMEPAD_A: "A",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_B: "B",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_X: "X",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_Y: "Y",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER: "LB",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER: "RB",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP: "DPAD_UP",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN: "DPAD_DOWN",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT: "DPAD_LEFT",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT: "DPAD_RIGHT",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_START: "START",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK: "BACK",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB: "L_THUMB",
            vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB: "R_THUMB",
        }
        return names.get(button, str(button))

    def update(self):
        self._g.update()

    def reset(self):
        self._g.reset()

    def left_trigger(self, val: float):
        if not self.enabled: return
        self._g.left_trigger_float(val)

    def right_trigger(self, val: float):
        if not self.enabled: return
        self._g.right_trigger_float(val)

    def left_joystick(self, x: float, y: float):
        if not self.enabled: return
        self._g.left_joystick_float(x_value_float=x, y_value_float=y)

    def right_joystick(self, x: float, y: float):
        if not self.enabled: return
        self._g.right_joystick_float(x_value_float=x, y_value_float=y)

    def press(self, button):
        if not self.enabled: return
        print(f"[手柄] 按下 {self._btn_name(button)}")
        self._g.press_button(button)

    def release(self, button):
        if not self.enabled: return
        print(f"[手柄] 松开 {self._btn_name(button)}")
        self._g.release_button(button)

    # ---- 组合操作 ----

    def tap(self, button, duration: float = 0.1):
        """按下 → 等待 → 松开"""
        if not self.enabled: return
        name = self._btn_name(button)
        print(f"[手柄] 点按 {name} ({duration:.1f}s)")
        self.press(button)
        self.update()
        time.sleep(duration)
        self.release(button)
        self.update()

    def press_lb_x(self):
        """LB+X 组合键（批量投弹触发），同时按下"""
        if not self.enabled: return
        print("[手柄] 按住 LB+X")
        self.press(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
        self.press(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
        self.update()

    def release_lb_x(self):
        """松开 LB+X，同时松开"""
        if not self.enabled: return
        print("[手柄] 松开 LB+X")
        self.release(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
        self.release(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
        self.update()

    def pull_bay_lever(self):
        """拉下弹舱拉杆（LT+RT）"""
        if not self.enabled: return
        print("[手柄] 拉下弹舱拉杆 (LT+RT)")
        self.left_trigger(1.0)
        self.right_trigger(1.0)
        self.update()
        time.sleep(0.15)
        self.left_trigger(0.0)
        self.right_trigger(0.0)
        self.update()

    def tap_rb(self):
        """RB 单独按键（视角切换 ID_TOGGLE_VIEW）"""
        if not self.enabled: return
        print("[手柄] 点按 RB")
        self.press(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
        self.update()
        time.sleep(0.3)
        self.release(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
        self.update()

    def return_to_third_person(self):
        """退出投弹视角/切回第三人称视角。"""
        if not self.enabled: return
        self.reset()
        self.update()
        time.sleep(0.08)
        self.tap_rb()

    def bail_out(self, hwnd=None):
        """跳伞（手柄键触发，需要先在 WT 中绑定这个键到"弹射跳伞"）

        默认使用 D-pad Up（方向键上），WT 里把它设成"弹射跳伞"即可。
        hwnd 参数保留但不再使用（键盘方式已改为手柄键）。
        """
        # 总开关关闭时同样不输出 —— 此时人已经接管了。
        if not self.enabled: return
        # 长按 D-pad Up 2 秒（WT 跳伞需要按住确认）
        btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP
        name = self._btn_name(btn)
        print(f"[手柄] 跳伞! 按住 {name} 2秒")
        self.press(btn)
        self.update()
        time.sleep(2.0)
        self.release(btn)
        self.update()

    # ---- 生命周期 ----

    def halt(self):
        """停止所有输出（摇杆归中、扳机关闭），但不注销。

        故意不加总开关守卫：set_enabled(False) 正是靠它先把舵面收回来。
        """
        self.reset()
        self.update()

    def destroy(self):
        """完全注销虚拟设备"""
        try:
            self.reset()
            self.update()
            time.sleep(0.05)
            self._g.unregister()
        except Exception:
            pass


def cleanup_all():
    """全局清理：复位 + 注销所有虚拟手柄（供 atexit/signal 调用）"""
    global _gpad_ref
    if _gpad_ref:
        _gpad_ref.destroy()
        _gpad_ref = None
