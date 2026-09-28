"""War Thunder 自动轰炸 — 参数调节 GUI + 游戏内半透明 HUD

主窗口（PyQt5）读写 config.json，并把改动**热应用**到正在运行的 App 实例；
HUD 是钉在游戏窗口某个角上的半透明置顶窗，实时显示机器人当前在做什么。

关于「热应用」为什么是 setattr(sys.modules["app"], 名字, 值) 而不是 reload：
    app.py 用的是值导入 `from common.config import (MIN_V, ...)`，那些名字在
    import 时就绑定到 app 的模块全局了，reload common.config 影响不到它们。
    但 app 里的函数是在**调用时**才从 app 的模块全局取这些名字，所以直接改
    app 的模块全局，对已经跑着的线程立刻生效。
    另有 4 个实例属性遮蔽了同名常量（sense / lock_side / bomb_cycles /
    bomb_count），必须一并改 —— 见 FIELDS 里的 inst 字段。

关于 HUD 的位置：用 win32 SetWindowPos 定位，坐标来源与 common/window.py 的
get_rect() 完全一致。故意不去调用 SetProcessDpiAwareness —— 那会改变现有
mss 截图/窗口矩形的坐标空间，可能弄坏已经跑通的识别流程。显示器缩放不是
100% 时位置可能偏一点，用 hud_offset_x/y 手动挪。
"""

import sys
import json
import signal

import win32gui
import win32con

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from common.config import CONFIG_PATH
from common.window import get_rect


# ==================== 字段表 ====================
# 每一项描述一个可在 GUI 里调的配置：
#   key   config.json 里的键名
#   label 界面标签
#   kind  int / float / bool / choice
#   rng   (min, max, step)；choice 时是 [(显示名, 实际值), ...]
#   glob  热应用时写哪个 app 模块全局（None = 不写）
#   inst  热应用时写哪个 App 实例属性（None = 不写）
#   note  额外提示（如"重启生效"）
FIELDS = [
    # ---- 投弹 ----
    dict(key="bomb_view_distance", label="投弹视角距离 (m)", kind="int",
         rng=(500, 20000, 100), glob="BOMB_VIEW_DISTANCE", group="投弹"),
    dict(key="bomb_count", label="单次投弹数量", kind="int",
         rng=(1, 50, 1), glob="BOMB_DEFAULT", inst="bomb_count", group="投弹"),
    dict(key="bomb_cycles", label="循环轰炸轮次", kind="int",
         rng=(0, 20, 1), glob="BOMB_CYCLES", inst="bomb_cycles", group="投弹",
         note="0 = 不自动收尾（手动 F12 停止）"),
    dict(key="pixel_deviation", label="投弹水平阈值 (px)", kind="int",
         rng=(1, 300, 1), glob="PIXEL_DEV", group="投弹"),
    dict(key="pixel_deviation_y", label="投弹垂直阈值 (px)", kind="int",
         rng=(1, 120, 1), glob="PIXEL_DEV_Y", group="投弹"),
    dict(key="bomb_view_sense", label="投弹视角灵敏度", kind="float",
         rng=(0.05, 1.0, 0.05), glob="BOMB_VIEW_SENSE", group="投弹"),
    dict(key="bomb_aim_offset_x", label="投弹准心水平偏移", kind="int",
         rng=(-300, 300, 1), glob="BOMB_AIM_OFFSET_X", group="投弹"),
    dict(key="bomb_aim_offset_y", label="投弹准心垂直偏移", kind="int",
         rng=(-300, 300, 1), glob="BOMB_AIM_OFFSET_Y", group="投弹"),
    dict(key="auto_bomb", label="自动开始投弹", kind="bool",
         glob="AUTO_BOMB", group="投弹", note="下次启动生效"),

    # ---- 追踪 ----
    dict(key="sense", label="摇杆灵敏度", kind="float",
         rng=(0.0, 1.0, 0.05), glob="SENSE_DEFAULT", inst="sense", group="追踪"),
    dict(key="min_v", label="摇杆最小输出", kind="float",
         rng=(0.0, 1.0, 0.01), glob="MIN_V", group="追踪"),
    dict(key="turn_speed", label="掉头转向速度", kind="float",
         rng=(0.0, 1.0, 0.05), glob="TURN_SPEED", group="追踪"),
    dict(key="aim_offset_x", label="准心水平偏移", kind="int",
         rng=(-300, 300, 1), glob="AIM_OFFSET_X", group="追踪"),
    dict(key="aim_offset_y", label="准心垂直偏移", kind="int",
         rng=(-300, 300, 1), glob="AIM_OFFSET_Y", group="追踪"),
    dict(key="guide_box_ratio", label="引导结束框占比", kind="float",
         rng=(0.005, 0.2, 0.005), glob="GUIDE_BOX_RATIO", group="追踪"),
    dict(key="lock_side", label="锁定目标方向", kind="choice",
         rng=[("最近", "nearest"), ("最左", "leftmost"), ("最右", "rightmost"), ("自动", "auto")],
         glob="LOCK_SIDE", inst="lock_side", group="追踪"),
    dict(key="stolen_timeout", label="目标丢失放弃超时 (s)", kind="int",
         rng=(1, 60, 1), glob="STOLEN_TIMEOUT", group="追踪"),

    # ---- 信号翻转 ----
    dict(key="track_right_x_sign", label="追踪 右摇杆X", kind="choice",
         rng=[("正常 (1)", 1), ("反向 (-1)", -1)], glob="TRACK_RIGHT_X_SIGN", group="信号翻转"),
    dict(key="track_right_y_sign", label="追踪 右摇杆Y", kind="choice",
         rng=[("正常 (1)", 1), ("反向 (-1)", -1)], glob="TRACK_RIGHT_Y_SIGN", group="信号翻转",
         note="俯冲方向反了就改这里"),
    dict(key="bomb_view_left_x_sign", label="投弹 左摇杆X", kind="choice",
         rng=[("正常 (1)", 1), ("反向 (-1)", -1)], glob="BOMB_VIEW_LEFT_X_SIGN", group="信号翻转"),
    dict(key="bomb_view_right_x_sign", label="投弹 右摇杆X", kind="choice",
         rng=[("正常 (1)", 1), ("反向 (-1)", -1)], glob="BOMB_VIEW_RIGHT_X_SIGN", group="信号翻转"),
    dict(key="bomb_view_right_y_sign", label="投弹 右摇杆Y", kind="choice",
         rng=[("正常 (1)", 1), ("反向 (-1)", -1)], glob="BOMB_VIEW_RIGHT_Y_SIGN", group="信号翻转"),

    # ---- 模型 ----
    dict(key="model_conf", label="检测置信度阈值", kind="float",
         rng=(0.05, 0.95, 0.05), glob="CONF_DEFAULT", special="yolo_conf", group="模型",
         note="之前是死配置，现已接到 YOLO 上"),
    dict(key="model_iou", label="检测 IOU 阈值", kind="float",
         rng=(0.05, 0.95, 0.05), glob="IOU_DEFAULT", special="yolo_iou", group="模型"),

    # ---- 导航 ----
    dict(key="fly_to_airfield", label="投弹后飞往机场", kind="bool",
         glob="FLY_TO_AIRFIELD", group="导航"),
    dict(key="airfield_arrival_ratio", label="机场抵达判定", kind="float",
         rng=(0.001, 0.1, 0.001), glob="AIRFIELD_ARRIVAL_RATIO", group="导航"),
    dict(key="nav_pitch_kv", label="导航 下降率增益", kind="float",
         rng=(0.0, 0.02, 0.001), glob="NAV_PITCH_KV", group="导航",
         note="仿真标定 0.003，加大前先跑 nav_replay selftest"),
    dict(key="nav_pitch_ka", label="导航 下滑线增益", kind="float",
         rng=(0.0, 0.008, 0.0005), glob="NAV_PITCH_KA", group="导航",
         note="设 0 会留下 1km 级的到位高度差"),
    dict(key="nav_yaw_kp", label="导航 航向增益 (1/s)", kind="float",
         rng=(0.02, 1.0, 0.01), glob="NAV_YAW_KP", group="导航"),
    dict(key="nav_yaw_kd", label="导航 航向阻尼 (1/s)", kind="float",
         rng=(0.0, 1.5, 0.05), glob="NAV_YAW_KD", group="导航",
         note="航线左右横跳就加大"),
    dict(key="nav_ias_ref", label="导航 目标表速 (km/h)", kind="int",
         rng=(150, 700, 10), glob="NAV_IAS_REF", group="导航"),

    # ---- 场景（识别节奏 + 排查用 log）----
    dict(key="scene_scan_interval", label="扫描周期 (s)", kind="float",
         rng=(1.0, 30.0, 0.5), glob="SCENE_SCAN_INTERVAL", group="场景",
         note="整屏 OCR 耗时从周期里扣"),
    dict(key="scene_log", label="写场景 log", kind="bool",
         glob="SCENE_LOG", group="场景", note="下次启动生效"),
    dict(key="scene_log_texts", label="log 记 OCR 全文", kind="bool",
         glob="SCENE_LOG_TEXTS", group="场景",
         note="排查误判全靠这列，关掉文件小很多"),
    dict(key="battle_log", label="记录战果", kind="bool",
         glob="BATTLE_LOG", group="场景",
         note="一局结算一行；勾选/取消立刻生效"),
    dict(key="unknown_log", label="记录未知画面", kind="bool",
         glob="UNKNOWN_LOG", group="场景",
         note="按内容去重，勾选/取消立刻生效"),
    dict(key="match_wait_warn", label="排队超时警告 (s)", kind="int",
         rng=(0, 3600, 30), glob="MATCH_WAIT_WARN", group="场景",
         note="只记日志不按键，0 = 不警告"),

    # ---- HUD（不写 app 全局，直接改 HUD 窗口）----
    dict(key="hud_enabled", label="显示 HUD", kind="bool",
         target="hud", group="HUD"),
    dict(key="hud_corner", label="停靠角", kind="choice",
         rng=[("右上", "topright"), ("左上", "topleft"),
              ("右下", "bottomright"), ("左下", "bottomleft")],
         target="hud", group="HUD"),
    dict(key="hud_opacity", label="底板不透明度", kind="float",
         rng=(0.15, 1.0, 0.05), target="hud", group="HUD"),
    dict(key="hud_scale", label="字号缩放", kind="float",
         rng=(0.6, 2.0, 0.1), target="hud", group="HUD"),
    dict(key="hud_offset_x", label="水平微调 (px)", kind="int",
         rng=(-1000, 1000, 5), target="hud", group="HUD"),
    dict(key="hud_offset_y", label="垂直微调 (px)", kind="int",
         rng=(-1000, 1000, 5), target="hud", group="HUD"),
    dict(key="show_log_lines", label="HUD 日志行数", kind="int",
         rng=(0, 30, 1), target="hud", group="HUD", note="0 = 不显示日志"),
    dict(key="follow_window", label="跟随游戏窗口", kind="bool",
         target="hud", group="HUD",
         note="窗口移动/缩放后识别不再失效"),
    dict(key="dive_angle_warn", label="俯冲角告警 (°)", kind="float",
         rng=(5.0, 90.0, 1.0), target="hud", group="HUD",
         note="仅 HUD 变红提示，不影响飞行"),
    dict(key="max_ias_warn", label="表速告警 (km/h)", kind="int",
         rng=(200, 1200, 10), target="hud", group="HUD",
         note="仅 HUD 变红提示，不影响飞行"),
    dict(key="hotkey_master", label="总开关热键", kind="choice",
         rng=[(f"F{i}", f"f{i}") for i in range(1, 13)],
         glob="HOTKEY_MASTER", group="HUD", note="重启生效"),
]

GROUP_ORDER = ["投弹", "追踪", "信号翻转", "模型", "导航", "场景", "HUD"]


# ==================== config.json 读写 ====================
# 直接用 json 读写原始文件：common/config.py 只在 import 时读一次、且不暴露写回
# 接口，而 config.json 里那些 _xxx 说明键必须原样保留（否则用户的文档注释会被抹掉）。

def load_raw_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[GUI] 读取 config.json 失败: {e}")
        return {}


def save_raw_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _default_for(spec):
    """字段的默认值：优先取 common/config.py 里 _DEFAULTS 的同名项。

    取不到时退回字段表自身描述的边界值。这里的兜底不能省 —— 一旦某个键漏在
    _DEFAULTS 外面，旧写法会去 spec["rng"] 上取，而 bool 字段根本没有 rng，
    直接 KeyError 打不开 GUI。所以宁可退化成边界值也不要抛。
    """
    import common.config as cfgmod
    d = cfgmod._DEFAULTS.get(spec["key"])
    if d is not None:
        return d
    kind = spec.get("kind")
    if kind == "bool":
        return False
    rng = spec.get("rng")
    if kind in ("int", "float") and rng:
        return rng[0]
    if rng:
        # choice: rng 是 [(显示名, 实际值), ...]
        return rng[0][1]
    return None


# ==================== 热应用 ====================

def apply_value(app_inst, hud, spec, value):
    """把一个值同时写到 config.json 之外的两个地方：app 模块全局 + App 实例。"""
    if spec.get("target") == "hud":
        hud.apply_config(spec["key"], value)
        return

    special = spec.get("special")
    if special and app_inst is not None:
        # detect/yolo.py 里 conf/iou 是实例属性，写全局没用
        setattr(app_inst.yolo, "conf" if special == "yolo_conf" else "iou", value)

    g = spec.get("glob")
    if g:
        app_mod = sys.modules.get("app")
        if app_mod is not None:
            setattr(app_mod, g, value)

    a = spec.get("inst")
    if a and app_inst is not None:
        setattr(app_inst, a, value)


# ==================== 游戏内 HUD ====================

class HudWindow(QWidget):
    """钉在游戏窗口角上的半透明置顶 HUD。鼠标穿透，不抢焦点。"""

    PAD = 10
    LINE_GAP = 3

    def __init__(self, app_inst):
        super().__init__()
        self.app = app_inst
        self.cfg = {s["key"]: _default_for(s) for s in FIELDS}

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool                       # 不出现在任务栏/Alt-Tab
            | Qt.WindowTransparentForInput  # 鼠标穿透
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._lines = []          # [(文本, 颜色)] 主状态区
        self._log_lines = []      # 最近日志

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)    # 5Hz 刷新内容，足够跟手

        self._place_timer = QTimer(self)
        self._place_timer.timeout.connect(self._reposition)
        self._place_timer.start(1000)   # 1Hz 跟随游戏窗口

        self.hide()

    # ---- 配置 ----

    def apply_config(self, key, value):
        self.cfg[key] = value
        if key == "hud_enabled" and not value:
            self._hide_hud()
        self._tick()

    # ---- 内容 ----

    def _tick(self):
        self._apply_font()
        self._lines = self._collect_lines()
        n = self.cfg.get("show_log_lines", 0) or 0
        self._log_lines = self._tail_log(n) if n > 0 else []
        # 尺寸必须在定位**之前**定下来：_reposition 要按宽高算右上角的 x，
        # 尺寸晚一拍的话 x 是用旧宽度算的，HUD 会有一秒挂在屏幕外，
        # 而且报给 App 的遮挡区域会跟窗口实际位置对不上。
        self._fit_size()
        self.update()
        self._reposition()

    def _fit_size(self):
        """按当前字体和文本内容算出窗口该多大，必要时 resize。

        日志行只按行数占位（宽度算它们没意义，正文才是决定宽度的那个），
        高度必须算上，否则日志会把最后几行挤出窗口。
        """
        fm = self.fontMetrics()
        texts = [t for t, _ in self._lines]
        texts += [""] * len(self._log_lines)
        text_w = max((fm.horizontalAdvance(t) for t in texts), default=200)
        w = text_w + self.PAD * 2
        h = len(texts) * (fm.height() + self.LINE_GAP) + self.PAD * 2
        if self.width() != w or self.height() != h:
            self.resize(w, h)
        return w, h

    def _apply_font(self):
        """字号在 _tick 里设，不要放进 paintEvent —— 在绘制中改字体会触发重绘递归。"""
        scale = float(self.cfg.get("hud_scale", 1.0) or 1.0)
        f = QFont("Consolas")
        f.setPointSizeF(max(7.0, 10.0 * scale))
        if f.pointSizeF() != self.font().pointSizeF():
            self.setFont(f)

    def _tail_log(self, n):
        try:
            with self.app._log_lock:
                lines = list(self.app.log_lines)[-n:]
        except Exception:
            return []
        return [ln.split("] ", 1)[-1] for ln in lines]   # 去掉时间戳前缀

    def _collect_lines(self):
        """组装 HUD 文本。返回 [(文本, QColor)]。"""
        a = self.app
        warn = QColor(255, 120, 120)
        ok = QColor(150, 240, 170)
        dim = QColor(190, 190, 190)
        hot = QColor(255, 200, 90)

        flight = getattr(a, "flight", None) or {}
        tinfo = getattr(a, "target_info", None) or {}
        ctrl = getattr(a, "_last_control_state", None) or {}

        if not getattr(a, "master_on", True):
            head, head_c = "⏸ 已暂停 (F12 恢复)", hot
        elif getattr(a, "running", False):
            head, head_c = "● 运行中", ok
        else:
            head, head_c = "○ 待机", dim

        lines = [(f"{head}      场景: {getattr(a, 'game_state', '?')}", head_c)]

        # 模式 / 轮次
        mode = ctrl.get("mode", "-")
        try:
            cycles, total = getattr(a, "cycle_count", 0), getattr(a, "bomb_cycles", 0)
            cyc = f"{cycles}/{total}" if total else f"{cycles}/∞"
        except Exception:
            cyc = "-"
        lines.append((f"模式: {mode:<12} 轮次: {cyc}", dim))

        # 目标
        if tinfo.get("dist_m") is not None:
            bd = tinfo.get("bearing_diff") or 0
            side = "左" if bd < 0 else "右"
            tgt = f"{tinfo['dist_m'] / 1000:.1f}km {side}偏 {abs(bd):.0f}°"
        else:
            tgt = "无 8111 数据"
        lock = getattr(a, "lock_side", "-")
        lines.append((f"目标: {tgt:<22} 锁定: {lock}", dim))

        # 高度 / 表速
        alt = flight.get("alt")
        ias = flight.get("ias") or flight.get("tas")
        alt_s = f"{alt:.0f}m" if isinstance(alt, (int, float)) else "-"
        ias_s = f"{ias:.0f}km/h" if isinstance(ias, (int, float)) else "-"
        ias_c = dim
        warn_ias = self.cfg.get("max_ias_warn")
        if isinstance(ias, (int, float)) and warn_ias and ias > warn_ias:
            ias_c = warn
        lines.append((f"高度: {alt_s:<12} 表速: {ias_s}", ias_c))

        # 升降率 / 实际俯冲角（超阈值变红，但**不做任何控制**）
        vs = flight.get("vs")
        vs_s = f"{vs:+.0f}m/s" if isinstance(vs, (int, float)) else "-"
        dive = flight.get("dive_angle")
        if isinstance(dive, (int, float)):
            dive_s = f"{dive:.0f}°"
        else:
            dive_s = "-"
        dive_c = dim
        warn_dive = self.cfg.get("dive_angle_warn")
        if (isinstance(dive, (int, float)) and warn_dive is not None
                and abs(dive) > warn_dive):
            dive_s += " ⚠"
            dive_c = warn
        lines.append((f"升降: {vs_s:<12} 实际俯冲角: {dive_s}", dive_c))

        return lines

    # ---- 定位 ----

    def _hide_hud(self):
        """隐藏 HUD，并让 App 别再遮挡那块画面。"""
        self.app.hud_rect = None
        self.hide()

    def _publish_hud_rect(self, rect, x, y):
        """把 HUD 的位置告诉 App，好让它从机器人自己的视野里涂黑这块。

        存的是**相对游戏客户区**的偏移而非绝对屏幕坐标：HUD 定位每秒才跟一次
        窗口，若存绝对坐标，窗口一移动就会有一秒钟的错位遮挡。
        """
        try:
            self.app.hud_rect = {
                "dx": int(x) - int(rect["left"]),
                "dy": int(y) - int(rect["top"]),
                "w": int(self.width()),
                "h": int(self.height()),
            }
        except Exception:
            self.app.hud_rect = None

    def _reposition(self):
        if not self.cfg.get("hud_enabled", True):
            self._hide_hud()
            return

        gw = getattr(self.app, "hwnd", None)
        if not gw or not win32gui.IsWindow(gw) or win32gui.IsIconic(gw) \
                or not win32gui.IsWindowVisible(gw):
            self._hide_hud()
            return

        try:
            rect = get_rect(gw)
        except Exception:
            self._hide_hud()
            return

        # 跟随窗口：把刷新到的矩形写回 App，顺带修掉「窗口一移动识别就全废」
        # 的老问题（App.rect 原来是启动时的一次性快照）。
        if self.cfg.get("follow_window", True) and self.app.rect != rect:
            self.app.rect = rect

        w, h = self.width(), self.height()
        corner = self.cfg.get("hud_corner", "topright")
        if corner.endswith("right"):
            x = rect["left"] + rect["width"] - w - 12
        else:
            x = rect["left"] + 12
        if corner.startswith("bottom"):
            y = rect["top"] + rect["height"] - h - 12
        else:
            y = rect["top"] + 12
        x += int(self.cfg.get("hud_offset_x", 0) or 0)
        y += int(self.cfg.get("hud_offset_y", 0) or 0)

        try:
            win32gui.SetWindowPos(
                int(self.winId()), win32con.HWND_TOPMOST, int(x), int(y),
                int(w), int(h),
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )
            if not self.isVisible():
                self.show()
            # 只有窗口真的显示出来了才遮挡：隐藏状态下不该浪费这块画面
            self._publish_hud_rect(rect, x, y)
        except Exception:
            self.app.hud_rect = None

    # ---- 绘制 ----

    def paintEvent(self, _ev):
        # 兜底：正常由 _tick 里的 _fit_size() 先定好尺寸，这里再算一次是为了
        # 首次绘制等极端情况。尺寸没变时它是空操作，不会引起重绘递归。
        self._fit_size()

        fm = self.fontMetrics()
        fm_h = fm.height()
        all_lines = list(self._lines)
        all_lines += [(t, QColor(150, 150, 150)) for t in self._log_lines]

        pad = self.PAD
        gap = self.LINE_GAP

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        opacity = float(self.cfg.get("hud_opacity", 0.65) or 0.65)
        bg = QColor(0, 0, 0)
        bg.setAlphaF(max(0.05, min(1.0, opacity)))
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 6.0, 6.0)
        p.fillPath(path, bg)

        y = pad + fm_h
        for text, color in all_lines:
            p.setPen(color)
            p.drawText(pad, y, text)
            y += fm_h + gap
        p.end()


# ==================== 主窗口 ====================

class MainWindow(QMainWindow):
    def __init__(self, app_inst):
        super().__init__()
        self.app = app_inst
        self.hud = HudWindow(app_inst)
        self.raw = load_raw_config()
        self.widgets = {}      # key -> (spec, getter_fn)

        self.setWindowTitle("War Thunder 自动轰炸 — 控制台")
        self.resize(700, 760)

        self.tabs = QTabWidget()
        self._build_field_tabs()
        self._build_status_tab()

        # 底部按钮
        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(8, 4, 8, 8)
        for text, fn, tip in [
            ("应用（写文件+热生效）", self.on_apply, "同时写入 config.json 并立即作用于运行中的程序"),
            ("保存", self.on_save, "只写 config.json"),
            ("重载", self.on_reload, "丢弃界面改动，重新从 config.json 读取"),
            ("恢复默认", self.on_restore, "回到 common/config.py 里的 _DEFAULTS"),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bl.addWidget(b)
        self.status_label = QLabel("")
        bl.addStretch(1)
        bl.addWidget(self.status_label)

        root = QWidget()
        rl = QVBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.tabs)
        rl.addWidget(bottom)
        self.setCentralWidget(root)

        self.hud._tick()
        self.hud._reposition()

        # 状态页刷新
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(300)

    # ---- 构建界面 ----

    def _build_field_tabs(self):
        groups = {}
        for spec in FIELDS:
            groups.setdefault(spec["group"], []).append(spec)

        for gname in GROUP_ORDER:
            specs = groups.get(gname)
            if not specs:
                continue
            page = QWidget()
            outer = QVBoxLayout(page)

            box = QGroupBox(gname)
            form = QFormLayout(box)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for spec in specs:
                row, getter, ctrl = self._make_row(spec)
                self.widgets[spec["key"]] = (spec, getter, ctrl)
                text = spec["label"]
                if spec.get("note"):
                    text += "  ⓘ"
                form.addRow(text, row)

            outer.addWidget(box)
            outer.addStretch(1)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, gname)

    def _make_row(self, spec):
        """按字段类型造控件，返回 (行控件, 取值函数, 真正的输入控件)。"""
        val = self.raw.get(spec["key"], _default_for(spec))
        kind = spec["kind"]

        if kind == "bool":
            cb = QCheckBox()
            cb.setChecked(bool(val))
            if spec.get("note"):
                cb.setToolTip(spec["note"])
            return cb, (lambda c=cb: c.isChecked()), cb

        if kind == "choice":
            combo = QComboBox()
            for label, v in spec["rng"]:
                combo.addItem(label, v)
            idx = combo.findData(val)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            if spec.get("note"):
                combo.setToolTip(spec["note"])
            return combo, (lambda c=combo: c.currentData()), combo

        # int / float：滑块 + 数字框联动
        lo, hi, step = spec["rng"]
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)

        is_int = kind == "int"
        spin = QSpinBox() if is_int else QDoubleSpinBox()
        spin.setRange(int(lo) if is_int else float(lo), int(hi) if is_int else float(hi))
        spin.setSingleStep(int(step) if is_int else float(step))
        if not is_int:
            spin.setDecimals(_decimals(step))
        spin.setValue(int(val) if is_int else float(val))
        spin.setFixedWidth(96)

        n_steps = max(1, int(round((hi - lo) / step)))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, n_steps)
        slider.setValue(_to_idx(float(val), lo, step, n_steps))

        def slider_moved(i):
            v = lo + i * step
            spin.setValue(int(round(v)) if is_int else round(v, _decimals(step)))

        def spin_changed(v):
            slider.blockSignals(True)
            slider.setValue(_to_idx(float(v), lo, step, n_steps))
            slider.blockSignals(False)

        slider.valueChanged.connect(slider_moved)
        spin.valueChanged.connect(spin_changed)

        lay.addWidget(slider, 1)
        lay.addWidget(spin)
        if spec.get("note"):
            box.setToolTip(spec["note"])
        return box, (lambda s=spin: s.value()), spin

    def _build_status_tab(self):
        page = QWidget()
        v = QVBoxLayout(page)

        self.status_box = QGroupBox("实时状态")
        self.status_form = QFormLayout(self.status_box)
        self.status_fields = {}
        for key, label in [
            ("state", "总状态"), ("scene", "游戏场景"), ("mode", "控制模式"),
            ("sticks", "摇杆输出"), ("cycle", "轰炸轮次"),
            ("target", "目标战区"), ("alt", "高度"), ("ias", "表速"),
            ("vs", "升降率"), ("dive", "实际俯冲角"),
        ]:
            lab = QLabel("-")
            self.status_fields[key] = lab
            self.status_form.addRow(label, lab)
        v.addWidget(self.status_box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFont(QFont("Consolas", 9))
        v.addWidget(QLabel("最近日志"))
        v.addWidget(self.log_view, 1)

        self.tabs.addTab(page, "状态")

    # ---- 取值 / 写值 ----

    def collect(self):
        out = {}
        for key, (_spec, getter, _ctrl) in self.widgets.items():
            try:
                out[key] = getter()
            except Exception:
                pass
        return out

    def push_to_widgets(self, cfg):
        for key, (spec, _getter, ctrl) in self.widgets.items():
            if key not in cfg or ctrl is None:
                continue
            val = cfg[key]
            kind = spec["kind"]
            try:
                if kind == "bool":
                    ctrl.setChecked(bool(val))
                elif kind == "choice":
                    i = ctrl.findData(val)
                    if i >= 0:
                        ctrl.setCurrentIndex(i)
                else:
                    ctrl.setValue(int(val) if kind == "int" else float(val))
            except Exception:
                pass

    def apply_all(self):
        """把界面上的值热应用到运行中的程序（不写文件）。"""
        vals = self.collect()
        for key, value in vals.items():
            apply_value(self.app, self.hud, self.widgets[key][0], value)
        return vals

    # ---- 按钮 ----

    def on_apply(self):
        vals = self.apply_all()
        self.raw.update(vals)          # 保留 _ 说明键
        try:
            save_raw_config(self.raw)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.app.log(f"[GUI] 参数已应用并保存: {_summary(vals)}")
        self._flash("已应用")

    def on_save(self):
        self.raw.update(self.collect())
        try:
            save_raw_config(self.raw)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.app.log("[GUI] 参数已写入 config.json（未热应用，重启后生效）")
        self._flash("已保存")

    def on_reload(self):
        self.raw = load_raw_config()
        self.push_to_widgets(self.raw)
        self.apply_all()
        self._flash("已重载")

    def on_restore(self):
        if QMessageBox.question(self, "恢复默认", "把所有参数恢复为代码里的默认值？") \
                != QMessageBox.Yes:
            return
        import common.config as cfgmod
        self.push_to_widgets(cfgmod._DEFAULTS)
        self._flash("已恢复默认（未保存）")

    def _flash(self, text):
        self.status_label.setText(text)
        QTimer.singleShot(2500, lambda: self.status_label.setText(""))

    # ---- 状态刷新 ----

    def refresh_status(self):
        a = self.app
        f = self.status_fields
        try:
            if not getattr(a, "master_on", True):
                f["state"].setText("⏸ 已暂停（F12 恢复）")
            elif getattr(a, "running", False):
                f["state"].setText("● 运行中")
            else:
                f["state"].setText("○ 待机（等待进入战斗）")
            f["scene"].setText(str(getattr(a, "game_state", "-")))

            ctrl = getattr(a, "_last_control_state", None) or {}
            f["mode"].setText(str(ctrl.get("mode", "-")))
            if ctrl:
                f["sticks"].setText(
                    f"lx={ctrl.get('lx', 0):+.2f} ly={ctrl.get('ly', 0):+.2f} "
                    f"rx={ctrl.get('rx', 0):+.2f} ry={ctrl.get('ry', 0):+.2f}"
                )

            total = getattr(a, "bomb_cycles", 0)
            f["cycle"].setText(
                f"{getattr(a, 'cycle_count', 0)}/{total if total else '∞'}"
            )

            ti = getattr(a, "target_info", None) or {}
            if ti.get("dist_m") is not None:
                bd = ti.get("bearing_diff") or 0
                f["target"].setText(
                    f"{ti['dist_m'] / 1000:.2f}km  "
                    f"{'左' if bd < 0 else '右'}偏 {abs(bd):.0f}°"
                )
            else:
                f["target"].setText("-")

            fl = getattr(a, "flight", None) or {}
            f["alt"].setText(_num(fl.get("alt"), "{:.0f} m"))
            ias = fl.get("ias") or fl.get("tas")
            f["ias"].setText(_num(ias, "{:.0f} km/h"))
            f["vs"].setText(_num(fl.get("vs"), "{:+.1f} m/s"))
            f["dive"].setText(_num(fl.get("dive_angle"), "{:.1f}°"))

            # 日志增量追加
            with a._log_lock:
                lines = list(a.log_lines)
            if lines:
                last = getattr(self, "_last_log", None)
                if last != lines[-1]:
                    self._last_log = lines[-1]
                    self.log_view.appendPlainText(lines[-1])
        except Exception:
            pass


def _num(v, fmt):
    return fmt.format(v) if isinstance(v, (int, float)) else "-"


def _decimals(step):
    s = f"{step:.6f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def _to_idx(value, lo, step, n_steps):
    if step <= 0:
        return 0
    return max(0, min(n_steps, int(round((value - lo) / step))))


def _summary(vals):
    """应用后打在日志里的几个关键值，只为让人一眼看出这次改了什么。"""
    keys = ["bomb_view_distance", "bomb_count", "bomb_cycles",
            "pixel_deviation", "bomb_view_sense"]
    return "  ".join(f"{k}={vals[k]}" for k in keys if k in vals)


# ==================== 入口 ====================

def run_gui(app_inst):
    """在主线程跑 Qt 事件循环。app_inst 的后台线程不受影响。"""
    qapp = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(app_inst)

    # Ctrl+C 支持。Qt 的 exec_() 整个跑在 C 里，Python 的信号处理器只有在解释器
    # 执行字节码时才轮得到 —— 所以光装 handler 没用，实测 Ctrl+C 后进程能接着跑
    # 好几分钟不退出。再挂一个空转 QTimer 每 200ms 回一次 Python，信号才送得到。
    #
    # 这里把所有「请停」信号都改成退出 Qt，而不是沿用 main.py 里 sys.exit() 那套：
    # 从 Qt 槽里抛 SystemExit 是未定义行为。让 exec_() 正常返回，main.py 的 finally
    # 会接着做 stop()，atexit 里的 cleanup_all() 负责收手柄。
    _stop = lambda *_: qapp.quit()
    for _sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        try:
            signal.signal(getattr(signal, _sig), _stop)
        except (AttributeError, ValueError, OSError):
            pass
    _signal_pump = QTimer()
    _signal_pump.timeout.connect(lambda: None)
    _signal_pump.start(200)

    win.show()
    app_inst.log("GUI 已启动")
    return qapp.exec_()
