import win32gui
import win32api
import win32con
import mss
import numpy as np
import cv2

_sct = mss.MSS()


def find_windows(name):
    """查找标题包含 name 的可见窗口，返回 [(hwnd, title), ...]"""
    out = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and name.lower() in win32gui.GetWindowText(hwnd).lower():
            out.append((hwnd, win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(cb, 0)
    return out


def get_rect(hwnd):
    """获取游戏客户区的 (left, top, width, height)，排除标题栏/边框。"""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    return {
        "left": screen_left,
        "top": screen_top,
        "width": screen_right - screen_left,
        "height": screen_bottom - screen_top,
    }


def capture(rect):
    """截取指定区域，返回 BGR numpy 数组"""
    img = np.array(_sct.grab(rect))
    return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


# VK 代码映射
VK = {
    "J": 0x4A,
    "ESC": 0x1B,
    "ENTER": 0x0D,
}


def send_key(hwnd, vk_code, hold_ms=100):
    """向指定窗口发送按键（WM_KEYDOWN + WM_KEYUP）

    参数:
        hwnd — 目标窗口句柄
        vk_code — 虚拟键码 (如 0x4A = J)
        hold_ms — 按住时长（毫秒）
    """
    import time
    win32gui.SendMessage(hwnd, win32con.WM_KEYDOWN, vk_code, 0)
    win32gui.SendMessage(hwnd, win32con.WM_CHAR, vk_code, 0)
    if hold_ms > 0:
        time.sleep(hold_ms / 1000.0)
    win32gui.SendMessage(hwnd, win32con.WM_KEYUP, vk_code, 0)
