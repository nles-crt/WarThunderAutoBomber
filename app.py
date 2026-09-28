"""War Thunder 自动轰炸主逻辑

工作流程:
  1. state_monitor_loop (目标周期 3s，见 SCENE_SCAN_INTERVAL) → OCR 场景识别 → 自动启停
  2. recog_loop (≤30fps) → YOLO 战区检测
  3. control_loop (≤33Hz) → 摇杆转向对准战区
  4. auto_bomb_sequence → 引导 → 投弹视角 → 对准 → 投弹 → 减速
"""

import re
import time
import threading
import os
import json
import hashlib
import collections

import math
import vgamepad as vg

from common.config import (
    MODEL_PATH,
    SENSE_DEFAULT,
    BOMB_DEFAULT,
    MIN_V,
    AUTO_BOMB,
    PIXEL_DEV,
    PIXEL_DEV_Y,
    GUIDE_BOX_RATIO,
    BOMB_VIEW_SENSE,
    FLY_TO_AIRFIELD,
    AIRFIELD_ARRIVAL_RATIO,
    # 导航到机场段的参数（单位都是物理量：度/秒、杆量每 m/s，见 common/config.py）
    NAV_TURN_RATE_MAX,
    NAV_YAW_KP,
    NAV_YAW_KD,
    NAV_YAW_DZ,
    NAV_OMEGA_TAU,
    NAV_YAW_MAX,
    NAV_AILERON_GAIN,
    NAV_PITCH_KV,
    NAV_PITCH_KA,
    NAV_PITCH_PUSH,
    NAV_PITCH_PULL,
    NAV_PITCH_RATE,
    NAV_THROTTLE_NOM,
    NAV_THROTTLE_K,
    NAV_THROTTLE_KV,
    NAV_THROTTLE_MIN,
    NAV_IAS_REF,
    NAV_DIFF_WINDOW,
    NAV_TARGET_ALT,
    BOMB_VIEW_DISTANCE,
    AIM_OFFSET_X,
    AIM_OFFSET_Y,
    BOMB_AIM_OFFSET_X,
    BOMB_AIM_OFFSET_Y,
    BOMB_VIEW_LEFT_X_SIGN,
    BOMB_VIEW_RIGHT_X_SIGN,
    BOMB_VIEW_RIGHT_Y_SIGN,
    TRACK_RIGHT_X_SIGN,
    TRACK_RIGHT_Y_SIGN,
    LOCK_SIDE,
    BOMB_CYCLES,
    TURN_SPEED,
    STOLEN_TIMEOUT,
    HOTKEY_MASTER,
    CONF_DEFAULT,
    IOU_DEFAULT,
    SCENE_SCAN_INTERVAL,
    SCENE_LOG,
    SCENE_LOG_TEXTS,
    BATTLE_LOG,
    UNKNOWN_LOG,
    MATCH_WAIT_WARN,
)
from common.window import find_windows, get_rect, capture
from common.utils import map_val
from common.nav_law import NavLaw
from common.results import parse as parse_battle_results, self_window_hits
from common.gamepad import Gamepad, VGAME_OK
from detect.yolo import YOLO
from detect.scene import detect_state, get_scene_button, get_keyword_actions, SceneDetector

try:
    from common.wt8111 import get_player, get_enemy_airfields, get_friendly_airfields, get_target_bearing, get_player_altitude, get_target_bombing_zone, get_map_size, get_bombing_zones, get_teammates, is_teammate_heading_to_zone, get_flight_state
    WT8111_OK = True
except ImportError:
    WT8111_OK = False


# ---- 引导阶段的俯冲限制（防"越接近越陡"的正反馈）----
# 追踪是纯视觉伺服：把战区拉到准心（画面中心）。但战区在地面上，机头指着它
# 就等于在俯冲，而**同样高度下越靠近战区，指向它所需要的俯冲角就越大** ——
# 于是越接近、机头压得越低、速度越快，最后近乎垂直扎下去、超速解体。
# 战区在画面上越掉越低，本身就说明机头已经压得够低了，这时候再加大低头指令
# 只会火上浇油。所以在目标掉到准心下方超过 BAND 之后，把低头指令线性收到 0：
# 飞机保持已有姿态（人做俯冲轰炸也是这么干的），俯冲角的进一步建立交给投弹视角。
# 抬头一侧不做衰减 —— 任何时候都要留满舵能把飞机拉起来。
GUIDE_DIVE_BAND = 0.10   # 目标低于准心超过画面的这个比例时，开始收回低头指令
GUIDE_DIVE_FADE = 0.30   # 再低这么多比例后，低头指令归零（0 = 保持姿态）


# "看不懂/不确定"的界面上，X 最快按一次的间隔（秒）。
#
# 大厅 / 战果汇总 / 需选载具 / 未识别 这四类界面都**不在 SCENE_BUTTONS 里**
# （那张表是"每轮扫描都按一次"的），按键统一走 `_press_x_throttled()`，共用
# 这一个时钟。为什么必须是一个全局时钟，写在那个方法的注释里。
UI_RETRY_S = 30

# 连续多少轮认不出场景才开始按 X。单帧误判不该触发按键，等几轮更稳
# （扫描周期默认 3 秒，所以 3 轮 ≈ 9 秒）。
UNKNOWN_ACK_STREAK = 3

# 按钮名 -> vgamepad 键位。节流阀和通用按键块共用这一份，
# 免得同一张表在文件里出现两遍、改了一处漏了另一处。
_PAD_BTN = {
    "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
    "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
    "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
}


def _nav_tunes():
    """导航段的整定值。**每步重建一次**：app.py 是按值 import 常量的，GUI 热改是
    setattr 到本模块全局，所以只有每次重新读一遍才能让改动立刻生效（用户看着日志
    调参时正需要这个）。"""
    return dict(
        turn_rate_max=NAV_TURN_RATE_MAX, yaw_kp=NAV_YAW_KP, yaw_kd=NAV_YAW_KD,
        yaw_dz=NAV_YAW_DZ, omega_tau=NAV_OMEGA_TAU, yaw_max=NAV_YAW_MAX,
        pitch_kv=NAV_PITCH_KV, pitch_ka=NAV_PITCH_KA,
        pitch_push=NAV_PITCH_PUSH, pitch_pull=NAV_PITCH_PULL, pitch_rate=NAV_PITCH_RATE,
        throttle_nom=NAV_THROTTLE_NOM, throttle_k=NAV_THROTTLE_K,
        throttle_kv=NAV_THROTTLE_KV, throttle_min=NAV_THROTTLE_MIN,
        ias_ref=NAV_IAS_REF, target_alt=NAV_TARGET_ALT, min_v=MIN_V,
    )


class App:
    def __init__(self):
        self.yolo = YOLO()
        self.rect = None
        # 窗口句柄。原来只在 _find_window 找到窗口时才赋值，没找到就没有这个属性，
        # 而 _target_info_loop 里 `if self.gpad and self.hwnd`（血量跳伞那条）会读它。
        # 虽然被 try/except 吞掉了，但补上初始化更干净。
        self.hwnd = None
        self.running = False
        self.lock = threading.Lock()
        self.dets = []
        self.fshape = None

        self.sense = SENSE_DEFAULT
        self.gpad = None
        self.game_state = "未知"

        # 引导阶段
        self.guide_mode = False

        # 投弹视角
        self.aim_lock = False
        self.throttle = 0.0
        self.aggression = 0.0
        self.bombs_depleted = False
        self._navigating = False
        self._last_log_coord = 0.0
        self._smooth_rx = 0.0
        self._smooth_ry = 0.0
        self._track_ix = 0.0
        self._track_iy = 0.0
        self._track_last_t = None
        self._fine_lx = 0.0
        self._fine_rx = 0.0
        self._fine_ry = 0.0
        self._bomb_dx = 0.0
        self._bomb_dy = 0.0
        self._bomb_ix = 0.0
        self._bomb_iy = 0.0
        self._bomb_last_dx = None
        self._bomb_last_dy = None
        self._bomb_last_t = None
        self._bomb_control_enabled = False
        self._last_aim_state = None
        self._last_control_state = None
        self._combat_texts_dumped = False  # 进战斗后是否已 dump 过整屏 OCR 文本
        self._lobby_since = None   # 进入大厅的时间，用于诊断"卡了多久"
        # 共用的按键节流时钟，见 _press_x_throttled()。0 = 立刻可以按。
        # **任何地方都不要重置它** —— 那正是"重置→立刻又按"死循环的来源。
        self._retry_next = 0.0
        # 本轮按下的键（主循环每轮把它的 actions 指过来），handler 内部
        # 按的键也记在这里，写进场景 log 供排查。
        self._actions = []
        # 本局是否已经拉过弹舱杆。LT+RT 是"切换"不是"打开"，判断舱门状
        # 态没有可靠信号（HUD 里是图标不是文字，8111 也不给），所以改成
        # 一局只拉一次：出生时舱门必关，拉一次必然变成开。
        self._bay_pulled = False

        # ---- 总开关（F12）----
        # 故意独立于 self.running：running 被 _drop_bombs / _finish_bomb_cycles
        # 等多处内部逻辑写 False，又在投弹结束后被重新置 True，
        # 拿它当"用户按了急停"的判断会被这些路径互相打架。
        self.master_on = True

        # ---- 供 GUI / HUD 读取的实时状态 ----
        # log() 里追加，HUD 显示最近几行。原实现只写 stdout 和文件，
        # 没有任何内存缓冲，外部想看日志只能去读文件。
        self.log_lines = collections.deque(maxlen=200)
        self._log_lock = threading.Lock()
        self.flight = {}        # 海拔/表速/升降率/实际俯冲角，见 _update_flight_state
        self.target_info = {}   # 目标战区距离/方位，见 _target_info_loop
        # HUD 悬浮窗相对游戏客户区的落点 {dx, dy, w, h}，由 gui.HudWindow 写入；
        # 用相对偏移而不是绝对屏幕坐标，这样游戏窗口移动时遮挡区域不会错位。
        self.hud_rect = None
        self._flight_prev = None  # 上次采样 (t, alt, nx, ny)
        self._map_m = 130000.0    # 地图边长（米），用于把归一化位移换算成米

        # ---- 辅助锁定模式 ----
        self.lock_side = LOCK_SIDE  # "leftmost" / "rightmost" / "auto"
        self.locked_target = None   # {nx, ny, wx, wy} from 8111
        self.bomb_cycles = BOMB_CYCLES  # 循环次数，0=无限
        self.cycle_count = 0        # 当前已完成轮次
        self._cycle_switches = 0    # 本轮已因"被抢"切换目标的次数
        self.turning_around = False # 是否在掉头阶段
        self.cycle_active = False   # 循环轰炸模式进行中

        # ---- 场景检测器 ----
        self.scene_detector = SceneDetector(log=self.log, stabilize_frames=2)
        self._unknown_streak = 0    # 连续"未知"次数（统计用，也进场景 log）
        self._scene_since = time.time()   # 当前场景的进入时刻，用于算停留时长
        self._queue_since = None          # 进入排队匹配的时刻
        self._queue_warned = False        # 排太久是否已经警告过（只警告一次）
        self._crew_lock_cancelled = False # 机组锁定界面是否已经按过取消

        os.makedirs("logs", exist_ok=True)
        _ts = time.strftime('%Y%m%d_%H%M%S')
        self.log_f = open(f"logs/{_ts}.log", "a", encoding="utf-8")
        # 结构化场景记录：一行一个 JSON，含 OCR 全文。控制台那行 `场景: X`
        # 看不出"为什么是这个场景"，这份文件才是排查误判的地方。
        try:
            self.scene_log_f = (open(f"logs/scenes_{_ts}.jsonl", "a", encoding="utf-8")
                                if SCENE_LOG else None)
        except OSError as e:
            self.scene_log_f = None
            self.log(f"场景 log 无法创建，已跳过: {e}")

        # 战果记录（logs/results_*.jsonl，一局结算画面一行）。**懒开文件**：这样
        # BATTLE_LOG 在 GUI 里勾选/取消能立刻生效（每局重读），而且关着的时候
        # 不会留下一个空文件。
        self.battle_log_path = f"logs/results_{_ts}.jsonl"
        self.battle_log_f = None
        self._battle_log_failed = False

        # 未知画面记录（logs/unknown_*.jsonl）。和战果记录同样是**懒开文件**，
        # 开关热生效、关着不留空文件。
        self.unknown_log_path = f"logs/unknown_{_ts}.jsonl"
        self.unknown_log_f = None
        self._unknown_log_failed = False
        self._unknown_last_md5 = None   # 上一条记录的 md5，用来按内容去重

        self.bomb_count = BOMB_DEFAULT

        self._load_model()
        self._find_window()
        self._init_gamepad()
        self._start_hotkey_listener()
        threading.Thread(target=self._state_monitor_loop, daemon=True).start()
        if WT8111_OK:
            threading.Thread(target=self._target_info_loop, daemon=True).start()
        self.log("启动完成，等待进入战斗...")

    # ==================== 基础设施 ====================

    def log(self, s):
        msg = f"[{time.strftime('%H:%M:%S')}] {s}"
        print(msg)
        self.log_f.write(msg + "\n")
        self.log_f.flush()
        # 内存环形缓冲供 GUI/HUD 显示。log 从多个线程并发调用，
        # deque.append 本身是原子的，加锁是为了和 GUI 的读取快照不打架。
        with self._log_lock:
            self.log_lines.append(msg)

    # ---- 总开关 ----

    def _toggle_master(self):
        """F12：切断/恢复所有手柄输出。可在任意线程被热键回调触发。"""
        self.master_on = not self.master_on
        if self.gpad:
            # Gamepad 内部是唯一输出汇聚点，关掉它等于一次性掐断
            # 所有按键和摇杆，不必去改几十个调用点。
            self.gpad.set_enabled(self.master_on)
        if self.master_on:
            self.log("▶ 总开关已恢复，机器人重新接管")
        else:
            self.log("⏸ 总开关已关闭（F12 恢复），手柄输出已切断、摇杆回中")

    def _start_hotkey_listener(self):
        """全局热键监听（pynput）。独立线程，与游戏是否在前台无关。"""
        try:
            from pynput import keyboard
        except ImportError:
            self.log("未安装 pynput，F12 总开关不可用（pip install pynput）")
            return

        key_name = (HOTKEY_MASTER or "f12").strip().lower()
        key_obj = getattr(keyboard.Key, key_name, None)
        if key_obj is None:
            self.log(f"未知热键名 {key_name!r}，总开关不可用")
            return

        def on_press(key):
            if key == key_obj:
                self._toggle_master()

        def on_release(key):
            return None

        try:
            listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            listener.daemon = True
            listener.start()
            self.log(f"总开关热键已就绪: {key_name.upper()}（按一下切断输出，再按恢复）")
        except Exception as e:
            self.log(f"热键监听启动失败: {e}")

    def _load_model(self):
        if os.path.exists(MODEL_PATH):
            try:
                self.yolo.load(MODEL_PATH)
                self.log(f"模型已加载: {os.path.basename(MODEL_PATH)}")
            except Exception as e:
                self.log(f"模型加载失败: {e}")
        else:
            self.log(f"模型文件不存在: {MODEL_PATH}")

    def _find_window(self):
        ws = find_windows("War Thunder") or find_windows("Thunder")
        if not ws:
            self.log("未找到游戏窗口")
            return
        self.hwnd, t = ws[0]
        self.rect = get_rect(self.hwnd)
        self.log(f"窗口: {t}  {self.rect['width']}x{self.rect['height']}")

    def _init_gamepad(self):
        if not VGAME_OK:
            self.log("无 vgamepad 库")
            return
        try:
            self.gpad = Gamepad()
            self.log("虚拟手柄已常驻")
        except Exception as e:
            self.log(f"手柄初始化失败: {e}")

    # ==================== 生命周期 ====================

    def start(self):
        if not self.yolo.model or not self.rect:
            self.log("模型或窗口未就绪")
            return
        if self.running:
            self.log("已在运行中")
            return
        # 把 config 里的模型阈值真正接到 YOLO 上。detect/yolo.py 里写死了
        # conf=0.5 / iou=0.45，而 config.json 的 model_conf / model_iou 一直
        # 没人读（CONF_DEFAULT / IOU_DEFAULT 定义了但无人导入），是死配置。
        self.yolo.conf = CONF_DEFAULT
        self.yolo.iou = IOU_DEFAULT
        self.running = True
        threading.Thread(target=self._recog_loop, daemon=True).start()
        threading.Thread(target=self._control_loop, daemon=True).start()
        if AUTO_BOMB:
            threading.Thread(target=self._auto_bomb_sequence, daemon=True).start()
        self.log("开始追踪+控制")

    def stop(self):
        """停止循环，复位手柄"""
        self.running = False
        self.guide_mode = False
        self.aim_lock = False
        self._bomb_control_enabled = False
        if self.gpad:
            self.gpad.halt()
        self.log("已停止识别")

    # ==================== 识别循环 ====================

    def _recog_loop(self):
        last = 0
        while self.running:
            now = time.time()
            if now - last < 1 / 30:
                time.sleep(0.005)
                continue
            last = now
            if not self.rect:
                time.sleep(0.1)
                continue
            frame = capture(self.rect)
            if frame is None:
                continue
            # 先遮掉我们自己的 HUD 悬浮窗，再裁游戏自带的下方 40px
            self._mask_hud(frame)
            # 去掉底部 40px（HUD 区域），减少误检
            if frame.shape[0] > 40:
                frame = frame[:-40, :, :]
            dets, _ = self.yolo.run(frame)
            with self.lock:
                self.dets = dets
                self.fshape = (frame.shape[1], frame.shape[0])

    # ==================== HUD 遮挡 ====================

    def _mask_hud(self, frame):
        """把 HUD 悬浮窗盖住的那块画面涂黑，免得机器人读到 HUD 自己的字。

        HUD 上写着「表速: 520km/h」「高度: 2840m」，而 detect/scene.py 判定「战斗中」
        只要命中 "km/h"/"m/s"/"表速"/"高度"/"速度" 里**任意一个**。HUD 不遮掉的话，
        它一显示，OCR 就会从 HUD 上读到这些字 → 机库也被判成战斗中 → 机器人一直
        拉弹舱、进投弹视角，正是之前修过的那个毛病又借 HUD 回来了。

        只涂黑不裁剪：检测框坐标是按 frame 尺寸算的，动尺寸会连累一堆换算。
        hud_rect 存的是相对游戏客户区的偏移，所以窗口移动时也不会错位。
        """
        hr = getattr(self, "hud_rect", None)
        if not hr:
            return
        h, w = frame.shape[:2]
        # 按未裁剪的原点算右下角，再分别夹取，否则 HUD 探出窗口边缘时会多遮一截
        dx, dy = int(hr["dx"]), int(hr["dy"])
        x1, x2 = max(0, dx), min(w, dx + int(hr["w"]))
        y1, y2 = max(0, dy), min(h, dy + int(hr["h"]))
        if x2 > x1 and y2 > y1:
            frame[y1:y2, x1:x2] = 0

    # ==================== 场景监控 ====================
    #
    # 周期由 config 的 scene_scan_interval 控制（默认 3 秒），且是**目标周期**：
    # 每轮实测耗时（含整屏 OCR）会从周期里扣掉，否则实际间隔会变成
    # "设定值 + OCR 耗时"，设成 3 秒也快不起来。

    def _state_monitor_loop(self):
        """Scene monitoring loop using SceneDetector"""
        while True:
            if not self.rect:
                time.sleep(1)
                continue

            # 本轮计时起点。放在窗口检查之后：那个分支只是空转，
            # 不该把 1 秒等待算进周期里。
            tick_start = time.monotonic()
            tick_wall = time.time()
            frame = capture(self.rect)
            if frame is None:
                time.sleep(0.5)
                continue

            # 不遮 HUD 的话，OCR 会读到 HUD 上的「表速/高度/km/h」并把机库判成战斗中
            self._mask_hud(frame)

            new_scene, texts = self.scene_detector.scan(frame)

            # 排队浮层是**半透明盖在机库上**的：社区/商店/科技树照样读得到，
            # 所以 raw 会在 匹配中/大厅/未知 之间来回跳；而消抖的候选计数只在**当前
            # 场景自己出现的那一帧**被清零（scene.py 的 _stabilize_scene）—— 只要排队
            # 浮层和当前场景每隔一帧交替一次，「匹配中」就永远攒不到 2 分，
            # scene 一直停在 未知 / 大厅 不动 ——
            # 所有靠 new_scene 的守卫跟着一起失效：未知连涨 3 轮按 X（_unknown_streak）、
            # 大厅每 30 秒按 X（_handle_lobby），干的都是**取消机器人自己刚开的匹配**，
            # 取消完再按 X 重新排队 —— 就是用户报的「停下重新匹配」。
            #
            # 所以这里用**消抖前**的 raw 做唯一的排队判定：这一帧的原始判决是
            # 匹配中，本轮就按匹配中处理。_DECISIVE 本来就是「命中即胜、不看分数」，
            # 是消抖把它架空了，这一行让它真正生效。
            if self.scene_detector.last_raw == "匹配中":
                new_scene = "匹配中"

            actions = []            # 本轮按下的键，写进场景 log 供排查
            # 同一个 list 对象交给 handler，这样 handler 内部按的键也进
            # 场景 log 的 actions 列 —— "这个 X 是谁按的"必须查得到。
            self._actions = actions

            if new_scene != self.game_state:
                changed, prev_dwell = True, time.time() - self._scene_since
                prev_scene = self.game_state
                self.game_state = new_scene
                self._scene_since = time.time()
                # 切换本身的日志由 scene.py 的 _log_decision 打（带分数明细），
                # 这里不再重复打一行。

                # 离开排队：结算等待时长，并把状态清干净，
                # 否则下次排队会沿用这一次的 _queue_since。
                if prev_scene == "匹配中" and self._queue_since:
                    self.log(f"排队结束，共等待 {time.time() - self._queue_since:.0f}s")
                    self._queue_since = None
                    self._queue_warned = False
                # 离开机组锁定：允许下次锁定时重新按一次取消。
                # 不在这里复位的话，取消键一辈子只按得了一次。
                if prev_scene == "机组锁定":
                    self._crew_lock_cancelled = False
            else:
                changed, prev_dwell = False, None

            if new_scene == "未知":
                self._unknown_streak += 1
                # 认不出来就按 X —— 但**要等几轮**，单帧误判（结算浮层闪一下、
                # OCR 偶发丢词）不该触发按键；而且走全局节流，30 秒最多一次。
                #
                # 这里以前是**每轮都按 X**，并且只要不在出击流程里还额外补 B 和 A。
                # B 是取消/退出、A 是确认，在没看懂的画面上按它们会退出战斗、掐掉
                # 正在进行的匹配、或者确认掉一个本不该确认的弹窗 —— 用户两次报的
                # "循环执行 xb" 都是这里。X 是"确定/关闭"，最坏也只是关掉一个弹窗。
                if self._unknown_streak >= UNKNOWN_ACK_STREAK:
                    self._press_x_throttled("未识别场景 → 按 X 尝试关闭弹窗")
            else:
                self._unknown_streak = 0

            # 场景分发
            if new_scene == "战斗中":
                self._handle_combat(texts)
            elif new_scene == "大厅":
                self._handle_lobby(texts)
            elif new_scene == "选择载具":
                self._handle_select_vehicle(texts)
            elif new_scene == "需选载具":
                self._handle_needs_vehicle(texts)
            elif new_scene in ("战果汇总", "退出中"):
                self._handle_post_battle(texts)
            elif new_scene == "加载中":
                self._handle_loading()
            elif new_scene == "无重生基地":
                self._handle_no_spawn()
            elif new_scene == "观战模式":
                self._handle_spectate()
            elif new_scene == "机场补给":
                self._handle_airfield_supply(texts)
            elif new_scene == "匹配中":
                self._handle_matchmaking(texts)
            elif new_scene == "机组锁定":
                self._handle_crew_lock(texts)
            elif new_scene == "选择新的研发项":
                self._handle_mods_screen(new_scene)
            elif new_scene == "购买改装件确认":
                self._handle_buy_prompt(new_scene)

            # 战果记录：一次结算画面只记一条，所以卡 `changed`（进入那一帧）而不是
            # 每帧都记 —— 理由见 _battle_log_write 的 docstring。
            if changed and new_scene == "战果汇总":
                self._battle_log_write(texts, tick_wall)

            # 通用按键：**排队和机组锁定一律跳过**。
            # 排队时任何按键都可能把正在进行的匹配取消掉；机组锁定由 handler
            # 自己精确按一次 B，通用按键会跟着再按一遍变成死循环。
            if (self.gpad and not self.aim_lock and not self.cycle_active
                    and new_scene not in ("匹配中", "机组锁定",
                                          "选择新的研发项", "购买改装件确认")):
                btn_map = _PAD_BTN
                btn = get_scene_button(new_scene)
                pressed = False
                # 大厅的"取消"弹窗判断搬进了 _handle_lobby —— 大厅已经不在
                # SCENE_BUTTONS 里，`btn` 对它恒为 None，这个分支不会命中。
                if btn and btn in btn_map:
                    self.gpad.tap(btn_map[btn])
                    actions.append(f"tap:{btn}")
                    pressed = (btn == "X")
                # 关键词触发：如果场景键已经按了 X，就不再重复按 X
                for kw, kw_btn in get_keyword_actions(texts):
                    if kw_btn not in btn_map or (pressed and kw_btn == "X"):
                        continue
                    # X 一律走全局节流阀。机库自己的"参战"按钮文字就是 `加入战斗`
                    # （logs/scenes_20260928_140707.jsonl idx0/2/3 的机库全文里都
                    # 有这四个字），所以这条路径以前在机库里每 3 秒按一次 X ——
                    # 和 SCENE_BUTTONS["大厅"] 是同一个洞，只是标签是
                    # tap:X(加入战斗) 而不是 tap:X，所以上一轮排查时漏掉了。
                    if kw_btn == "X":
                        self._press_x_throttled(f"关键词 {kw} → 按 X",
                                                f"tap:X({kw})")
                        continue
                    self.gpad.tap(btn_map[kw_btn])
                    actions.append(f"tap:{kw_btn}({kw})")

            # 未知画面的记录。放在通用按键块**之后** —— actions 那时候才收齐，
            # 而"未知画面上按了什么键"正是排查这类问题的第一条线索
            # （X+B+A 那场 7 分钟的风暴就是从 actions 列反推出来的）。
            if new_scene == "未知":
                self._unknown_log_write(texts, tick_wall, actions)

            elapsed = time.monotonic() - tick_start
            self._scene_log_write(
                evt="change" if changed else "scan",
                scene=new_scene, texts=texts,
                streak=self._unknown_streak,
                dwell=prev_dwell, actions=actions,
                tick_wall=tick_wall, tick_ms=int(elapsed * 1000),
            )

            # 目标周期：扣掉本轮已经花掉的时间（主要是整屏 OCR）
            time.sleep(max(0.0, SCENE_SCAN_INTERVAL - elapsed))

    def _scene_log_write(self, evt, scene, texts, streak, dwell, actions,
                         tick_wall=None, tick_ms=None):
        """写一行 JSONL 场景记录。scene_log 关掉时是空操作。

        存在的意义是回答"机器人当时到底看到了什么"——控制台日志只有一行
        `场景: X`，而排障需要的是那一刻的 OCR 全文、分数明细和按了哪些键。

        时间戳打的是**本轮起点**（tick_wall），不是写入时刻。写入发生在
        补偿 sleep **之前**，用写入时刻的话相邻两行的间隔会是
        `周期 - 上轮OCR + 本轮OCR`，看着在 1.8~3.7s 之间乱跳，
        而实际周期是准的 —— 想直接看周期就会被这行时间戳骗到。
        """
        f = self.scene_log_f
        if f is None:
            return
        try:
            now = time.time()
            rec = {
                "evt": evt,
                "t": time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(tick_wall or now))
                     + f".{int((tick_wall or now) * 1000) % 1000:03d}",
                "ts": round(tick_wall or now, 3),
                "scene": scene,
                "raw": self.scene_detector.last_raw,
                "scores": self.scene_detector.last_scores,
                "gated_out": self.scene_detector.last_gated,
                "ocr_ms": self.scene_detector.last_ocr_ms,
                "streak": streak,
                "queue_s": (round(now - self._queue_since, 1)
                            if self._queue_since else None),
                "actions": actions,
            }
            if tick_ms is not None:
                rec["tick_ms"] = tick_ms
            if dwell is not None:
                rec["dwell_s"] = round(dwell, 1)
            if SCENE_LOG_TEXTS:
                rec["texts"] = texts
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        except Exception as e:
            # 场景 log 是诊断用的，绝不能因为它把监控循环搞挂
            self.log(f"场景 log 写入失败: {e!r}")

    def _battle_log_write(self, texts, tick_wall=None):
        """把一局结算画面（场景 `战果汇总`）的 OCR 结果写成一行 JSON。

        一条记录 = **一次结算画面，不是一帧**。调用点用 `changed` 卡住"进入该场景
        那一帧"：结算画面在显示期间完全静止（logs/scenes_20260928_162128.jsonl 里
        8 帧的 texts 的 md5 完全相同），所以进入帧就等于整屏内容。

        一次停留的**尾巴**不能要：那次的最后一帧 raw 已经变成「大厅」，只是
        `_stabilize_scene` 要连续 2 次命中才换锁，那一帧 81 个 token 全是机库的文字。

        解析（common/results.py）出的字段少几个不会丢数据 —— 原始 texts 永远进 JSON。
        """
        if not BATTLE_LOG:
            return                  # 每局重读模块全局 ⇒ GUI 里勾选立刻生效
        if self.battle_log_f is None and not self._battle_log_failed:
            try:
                self.battle_log_f = open(self.battle_log_path, "a", encoding="utf-8")
            except OSError as e:
                # 打不开就记一次然后闭嘴，否则每局都在日志里刷同一条
                self._battle_log_failed = True
                self.log(f"战果记录无法创建，已跳过: {e}")
        f = self.battle_log_f
        if f is None:
            return
        try:
            now = time.time()
            wall = tick_wall or now
            map_name, fields = parse_battle_results(texts)
            hits = self_window_hits(texts)
            rec = {
                # 时间戳口径和 _scene_log_write 一致：tick 开始那一刻，不是写盘那一刻
                "t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall))
                     + f".{int(wall * 1000) % 1000:03d}",
                "ts": round(wall, 3),
                "scene": "战果汇总",
                "map": map_name,
                "fields": fields,
                # 机器人自己的窗口挡在游戏上时，OCR 读到的是它自己（实例见
                # logs/scenes_20260928_114345.jsonl）。这里是**标记不是过滤**：
                # 数据照存，统计时按 suspect 筛掉即可，也免得误判悄悄丢一局战果。
                "suspect": bool(hits),
                "suspect_hits": hits,
                "texts": texts,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        except Exception as e:
            # 和场景 log 一样：诊断用的东西绝不能把监控循环搞挂
            self.log(f"战果记录写入失败: {e!r}")

    def _unknown_log_write(self, texts, tick_wall=None, actions=None):
        """把一张"没认出来"的画面记进 logs/unknown_*.jsonl。**按内容去重**。

        为什么不像 _battle_log_write 那样卡 `changed`（进入场景那一帧）：
        `未知` 不是"一张画面"，而是**一段时间**，这段时间里的画面还会变。
        实测 logs/scenes_20260928_114629.jsonl：

            11:46:34  raw=未知   「该等级的所有改装件均研发完毕！」弹窗
            11:46:37  raw=未知   「选择新的研发项」改装件界面   ← 就是它
            11:46:43  raw=未知   又一张弹窗

        三帧 raw 都是"未知"，场景锁从头到尾没动过。照 `changed` 记的话，中间那张
        ——本次新加场景的正样本——根本不会被写下来。所以这里比的是**本帧 texts 的
        md5 和上一条记录的 md5**，不同才写。

        `_unknown_last_md5` **不随场景变化复位**，所以整份文件读起来就是
        "机器人遇到过的每一张没认出来的画面，按首次出现的顺序各一条"。

        连 raw / scores / gated_out 一起记是有意的：它们直接回答"差几分认出来"、
        "是不是被门控拦下的"，比光有 OCR 全文有用得多 —— 关键词表就是照这个补的。
        """
        if not UNKNOWN_LOG:
            return                  # 每次重读模块全局 ⇒ GUI 里勾选立刻生效
        if self.unknown_log_f is None and not self._unknown_log_failed:
            try:
                self.unknown_log_f = open(self.unknown_log_path, "a",
                                          encoding="utf-8")
            except OSError as e:
                self._unknown_log_failed = True
                self.log(f"未知画面记录无法创建，已跳过: {e}")
        f = self.unknown_log_f
        if f is None:
            return
        try:
            key = hashlib.md5(
                "\x1f".join(texts or []).encode("utf-8")).hexdigest()[:8]
            if key == self._unknown_last_md5:
                return
            self._unknown_last_md5 = key
            now = time.time()
            wall = tick_wall or now
            rec = {
                # 时间戳口径和另外两份 log 一致：tick 起点，不是写盘那一刻
                "t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall))
                     + f".{int(wall * 1000) % 1000:03d}",
                "ts": round(wall, 3),
                "scene": "未知",
                "md5": key,
                "raw": self.scene_detector.last_raw,
                "scores": self.scene_detector.last_scores,
                "gated_out": self.scene_detector.last_gated,
                "streak": self._unknown_streak,
                "actions": actions or [],
                "texts": texts or [],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        except Exception as e:
            # 和另外两份 log 一样：诊断用的东西绝不能把监控循环搞挂
            self.log(f"未知画面记录写入失败: {e!r}")

    def _press_throttled(self, btn, why, label):
        """节流阀的通用形式：`btn` 取 'X' / 'B' / 'A'。真按了返回 True。

        键位不同，但**时钟只有一个**（`self._retry_next`）。抽这一层出来，
        是为了让「退出这个界面」的两个新 handler 复用同一个钟（一个按 X、一个按 B）
        —— 各写一套计时必然会漏一处，这是这个仓库已经踩过的坑。
        """
        if not self.gpad:
            return False
        now = time.time()
        if now < self._retry_next:
            return False
        self._retry_next = now + UI_RETRY_S
        self.log(why)
        self.gpad.tap(_PAD_BTN[btn])
        self._actions.append(label)
        return True

    def _press_x_throttled(self, why, label="tap:X"):
        """所有"看不懂/不确定"的界面共用的 X 节流阀。返回 True 表示这次真按了。

        四类界面都要按 X —— 大厅（加入战斗）、战果汇总/退出中（离开结算）、
        需选载具（按确定关提示）、未识别（关弹窗）—— 但都不能按快：

        - 按快了会把正在进行的匹配掐掉：实测 logs/scenes_20260928_131739.jsonl
          13:34:59 → 13:48:02 按了 260 多次 X，一次战斗都没进；
        - 按快了会把机器人卡在弹窗里：logs/scenes_20260928_140707.jsonl 里
          「需选载具」弹出后每 3 秒被重开一次，在屏幕上挂了 2 分钟。

        各写一套计时必然会漏一处，所以只留**一个全局时钟**。关键在
        `self._retry_next` **没有任何路径会重置它** —— 也就不存在
        "换了场景 → 计时归零 → 立刻又按"这种循环。

        代价：从一个界面切到另一个界面时，第一次按键最坏要等满 UI_RETRY_S。
        这是有意的取舍，UI_RETRY_S 本来就是这个重试周期。

        实现体搬到了 `_press_throttled`（只是把键位参数化），这里保留原签名和
        原行为 —— 四个既有调用方一处都不用动。
        """
        return self._press_throttled("X", why, label)


    # ---- Scene handlers ----

    def _reset_sortie_state(self):
        """离开战斗/换载具时复位"每局一次"的状态。

        弹舱杆和整屏 OCR dump 都是以"一次出击"为单位的：重生一架新飞机后
        舱门又是关的，得允许再拉一次。
        """
        self._bay_pulled = False
        self._combat_texts_dumped = False

    def _handle_combat(self, texts):
        """In combat: auto start"""
        self._lobby_since = None  # 离开大厅
        if (texts and not self._combat_texts_dumped
                and any("炸弹" in t for t in texts)):
            # 每局战斗只打一次整屏 OCR 文本，用于核对 HUD 措辞
            # 以及场景锚点词有没有漏词
            self._combat_texts_dumped = True
            self.log(f"战斗中 OCR 文本: {texts}")

        if not self.running:
            if self.bombs_depleted:
                if not self._navigating and not self.cycle_active:
                    self.log("\u672c\u5c40\u70b8\u5f39\u5df2\u6295\u5b8c\uff0c\u8df3\u8fc7\u81ea\u52a8\u542f\u52a8")
            else:
                self.log("\u8fdb\u5165\u6218\u6597 \u2192 \u81ea\u52a8\u542f\u52a8\u8ffd\u8e2a")
                self.start()

    def _handle_lobby(self, texts):
        """Lobby: reset bomb state + auto re-join after timeout"""
        self._reset_sortie_state()  # 新一局：弹舱杆 / OCR dump 都复位
        if self.bombs_depleted:
            self.bombs_depleted = False
            self.log("\u70b8\u5f39\u72b6\u6001\u5df2\u91cd\u7f6e")
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()
            return

        # 排队浮层是**盖在机库上**的：社区/商店/科技树照样读得到，所以排队时
        # 经常被判成"大厅"。这时候按键就是把正在进行的匹配取消掉，整段跳过。
        #
        # 主判据已经上移到 _state_monitor_loop：raw 是"匹配中"就整轮按匹配中走，
        # 排队时这个 handler 根本进不来 —— 连 self.running 那段的 stop() 也一起挡掉了。
        # 这里只留兜底：它只认匹配中四个关键词里的一个"正在等待游戏"，
        # raw 一个词都没读到的极端情况下靠它再挡一层。
        if any("正在等待游戏" in t for t in texts):
            self._lobby_since = None
            return

        # 加入战斗的 X 走全局节流（UI_RETRY_S 秒最多一次，见 _press_x_throttled）。
        #
        # **这组键必须自己控频，不能交给"每轮扫描按一次"的通用表。** 大厅原本在
        # SCENE_BUTTONS 里映射到 X，于是一个 3 秒的扫描周期就按一次 X；后来又在
        # elapsed > 60 的分支里清零计时，30~60 秒那一段同样是每 3 秒一下。实测
        # logs/scenes_20260928_131739.jsonl：13:34:59 → 13:48:02 一直在按 X，
        # 260 多次，机器人在大厅里转圈出不去，中间还夹着 B（>60s 的"退回主菜单"分支）。
        now = time.time()
        if self._lobby_since is None:
            self._lobby_since = now     # 只用于下面那行"已停留 Ns"的日志
        # 挡路的弹窗（有"取消"）先用 B 关掉。**只在"这次确实要按 X"时才关**，
        # 否则它自己会变成每轮一次的 B 循环 —— B 是取消/退出，按多了就是死循环。
        if self.gpad and "取消" in texts and now >= self._retry_next:
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.3)
            self._actions.append("tap:B")
            time.sleep(0.5)
        self._press_x_throttled(
            f"大厅已停留 {now - self._lobby_since:.0f}s → 尝试加入战斗")

    def _handle_matchmaking(self, texts):
        """排队等匹配：一个键都不按，只记录等了多久。

        排队界面上任何按键都可能把匹配取消掉（B 是取消、X 要看当前焦点），
        而排队本身不是错误状态，干等就行了。所以这里刻意什么都不做，
        只把等待时长记下来——排太久说明卡住了，那是要人来看的事，不是机器人
        自己能靠按键解决的。这也是通用按键逻辑跳过"匹配中"的原因。
        """
        if self._queue_since is None:
            self._queue_since = time.time()
            self._queue_warned = False
            self.log("排队匹配中，等待开始（不按键）")
            return

        waited = time.time() - self._queue_since
        if MATCH_WAIT_WARN and not self._queue_warned and waited > MATCH_WAIT_WARN:
            self._queue_warned = True
            self.log(f"排队已 {waited / 60:.1f} 分钟仍未开始 —— 只记录不干预，"
                     f"若持续无进展请手动检查")

    def _handle_crew_lock(self, texts):
        """机组锁定（飞机被拉断后）。按取消退掉界面，然后等飞机解锁。

        用户明确要求**不要选飞机**，只把界面取消掉等它自己解锁。
        B 在这个仓库里一律当取消/返回（_handle_lobby 按 B 退回主菜单、
        `请选择符合要求`→B、`研发完毕`→B），X 才是确认，所以这里用 B。
        只处理一次：通用按键逻辑每轮扫描都按，会变成按键死循环，所以
        "机组锁定"也被排除在通用按键之外。这里的 `_crew_lock_cancelled`
        同时充当"这一轮锁定已经处理过"的标志，被主循环在**离开**该场景时复位。
        它必须在 `if self.gpad` **外面**置位：没有手柄时停机和状态复位照样只该
        做一次（之前写在里面，导致这两个动作每 3 秒重复一遍）。
        """
        if self._crew_lock_cancelled:
            return
        self._crew_lock_cancelled = True

        self._reset_sortie_state()
        self._queue_since = None
        if self.running:
            self.log("离开战斗 → 自动停止")
            self.stop()

        if self.gpad:
            self.log("机组锁定界面 → 按 B 取消，等待飞机解锁（不选飞机）")
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.3)

    def _handle_mods_screen(self, scene):
        """「选择新的研发项」改装件界面：按 **X** 退出。

        **这里原来按 B，实机确认 B 在这个界面上不生效。** 两个界面因此拆成了两个
        handler —— 它们的 X 含义相反（这里是"确认选择"，购买弹窗是"确认购买"），
        绑在同一个函数里就只能共用一个键，而这两个键恰好不能共用。

        X 在这里是"确认选择"：机器人会替玩家定下研发项。这是明知代价的取舍 ——
        B 退不出去、界面一直挂着，比替玩家定错一个研发项更糟。要改回去就改这一行。

        走 `_press_throttled` 而不是 `_crew_lock_cancelled` 那种"一次就够"的守卫：
        按一次正常就退出去了，但万一没吃到，节流阀会让它 30 秒后再试一次，
        不会永久卡死 —— 和「需选载具」的"保持重试而不是停机"是同一个取舍。

        按键由 handler 独占，所以这个场景留在通用按键的排除元组里：否则界面上
        那个按钮 `购买所有研发完毕的改装件（1100条）` 里含的 `研发完毕` 会跟着
        再按一遍（修复前就是这么 X 和 B 一起按的，
        见 logs/scenes_20260928_131739.jsonl 13:17:53）。
        """
        self._press_throttled(
            "X", f"{scene} → 按 X 退出", f"tap:X({scene})")

    def _handle_buy_prompt(self, scene):
        """「还有改装件未购入。要立刻购买吗？」弹窗：按 **B** 取消。

        机器人不替玩家花银狮 —— 这是用户明确要求的，也是这里按 B 而不是 X 的
        唯一原因（X 在这个弹窗上是"确认购买"，会真的花掉 1520 象）。和
        `_handle_mods_screen` 分开写正是为了这个：两个界面的 X 含义相反，
        共用一个 handler 就只能共用一个键。

        节流阀的理由同上。另外不拿 `是` / `否` 当关键词：样本帧里 `否` 被 OCR
        读成了 `香`，那两个按钮认不出来。
        """
        self._press_throttled(
            "B", f"{scene} → 按 B 取消购买", f"tap:B({scene})")

    def _handle_select_vehicle(self, texts):
        """Vehicle select: auto join"""
        self._reset_sortie_state()
        self._lobby_since = None  # 离开大厅
        if self.bombs_depleted:
            self.bombs_depleted = False
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()
        # 这个 X **故意不走节流阀**：出击界面上它就是"加入战斗！"，按下去才会
        # 排队，一次没成当然要立刻再按。补记进 _actions —— 事故里 idx79 显示
        # actions=[] 却确实按了 X，就是这里漏记，害得排查时只能反推。
        if self.gpad and not any("\u53d6\u6d88" in t for t in texts):
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
            self._actions.append("tap:X(选择载具)")

    def _handle_needs_vehicle(self, texts):
        """「开始任务前必须先点选一台可供出击的载具。」弹窗。

        在**不可重生**地图上阵亡后按结果浮层的「加入战斗！」就会弹这个框，
        框里只有一个「确定」。它的意思是阵容里没有一台可出击的载具 ——
        机器人没法替玩家挑（载具列表里没有"哪台可用"的信号），所以按一次
        确定把提示关掉，然后过 UI_RETRY_S 再试一次加入。

        用户明确要求**保持重试而不是停机**：停机等于整局自动化就停在这儿等人。
        按键走 _press_x_throttled，所以这里每 30 秒最多按一次 ——
        修复前是每轮扫描一次 X（还补 B/A），实测 logs/scenes_20260928_140707.jsonl
        这个框在屏幕上挂了 2 分钟，被砸了 40 次 X + 3 次 B + 3 次 A。
        """
        self._reset_sortie_state()
        self._lobby_since = None  # 不在大厅
        if self.bombs_depleted:
            self.bombs_depleted = False
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()
        self._press_x_throttled(
            "阵容里没有可出击的载具 → 按确定关掉提示，30 秒后重试加入")

    def _handle_post_battle(self, texts):
        """Post-battle: skip results"""
        self._reset_sortie_state()
        if self.bombs_depleted:
            self.bombs_depleted = False
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()
        # 结算/结果界面的 X 改由这里节流按（"战果汇总"已从 SCENE_BUTTONS 摘掉）。
        # 在阵亡后的结果浮层上 X 可能是「加入战斗！」——每 3 秒按一次就会每 3 秒
        # 重开一次「需选载具」弹窗，所以必须走节流。退出中 走的是同一个分支。
        self._press_x_throttled("战果汇总 → 按 X 离开结算画面")

    def _handle_loading(self):
        """Loading: no-op"""
        pass

    def _handle_no_spawn(self):
        """No spawn: stop tracking + press X to continue"""
        self._reset_sortie_state()
        self.log("\u65e0\u53ef\u7528\u91cd\u751f\u57fa\u5730")
        if self.running:
            self.stop()
        self.cycle_active = False
        if self.gpad:
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)

    def _handle_spectate(self):
        """Spectate: stop + press X to continue"""
        if self.running:
            self.stop()
        self.cycle_active = False
        if self.gpad:
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)

    def _handle_airfield_supply(self, texts):
        """Airfield supply: press A, reset bomb state"""
        self.log("\u673a\u573a\u8865\u7ed9\u4e2d")
        self.bombs_depleted = False


    def _update_flight_state(self):
        """采样海拔/表速，并由高度变化率反推实际俯冲角，供 HUD 显示。

        8111 的 /state 里**没有俯仰角字段**，所以只能这样算：

            实际俯冲角 = atan2(下降高度, 水平位移)     正=下降

        水平位移由 /map_obj.json 的归一化坐标乘地图边长得到。采样频率就是
        本循环的 1Hz —— 对"看着 HUD 判断俯冲是否过陡"这个用途足够了。
        """
        try:
            st = get_flight_state()
            pos = get_player()
        except Exception:
            return
        if not st:
            return

        now = time.time()
        alt = st.get("alt")
        if not isinstance(alt, (int, float)):
            self.flight = {**self.flight, "ias": st.get("ias"), "alt": None}
            return
        alt = float(alt)

        if pos and isinstance(pos.get("x"), (int, float)):
            self._map_m = float(get_map_size() or 130000.0)
            nx, ny = float(pos["x"]), float(pos["y"])
        else:
            nx = ny = None

        prev = self._flight_prev
        vs = gs = dive = None
        if prev and nx is not None:
            dt = now - prev["t"]
            if dt > 0.4:
                d_alt = alt - prev["alt"]
                # 归一化坐标的 x/y 是地图格子的两个轴，各自乘地图边长
                dx_m = (nx - prev["nx"]) * self._map_m
                dy_m = (ny - prev["ny"]) * self._map_m
                horiz = math.hypot(dx_m, dy_m)
                vs = -d_alt / dt                       # 正=上升
                gs = horiz / dt                        # 地速 m/s
                if horiz > 1.0:
                    dive = math.degrees(math.atan2(-d_alt, horiz))

        self._flight_prev = {"t": now, "alt": alt, "nx": nx, "ny": ny}
        self.flight = {
            "alt": alt,
            "ias": st.get("ias"),
            "tas": st.get("tas"),
            "vs": vs,
            "gs": gs,
            "dive_angle": dive,
            "t": now,
        }

    def _target_info_loop(self):
        """通过 8111 API 每秒刷新战区距离/方位与飞行状态（供 HUD/GUI 读取）"""
        while True:
            if self.game_state == "战斗中" and self.running:
                # 血量监控 — 残血自动跳伞
                try:
                    health = get_health()
                    if health is not None and health < 0.25:
                        self.log(f"⚠️ 机体严重受损 血量{health:.0%}，跳伞下一把!")
                        self.stop()
                        if self.gpad and self.hwnd:
                            self.gpad.bail_out()
                            return
                except Exception:
                    pass

                try:
                    info = get_target_bombing_zone()
                    if info:
                        self.target_info = {**info, "t": time.time()}
                        side = "左" if info["bearing_diff"] < 0 else "右"
                        self.log(
                            f"目标战区 {info['dist_m']/1000:.1f}km  "
                            f"({side}偏 {abs(info['bearing_diff']):.0f}°)  "
                            f"夹角 {info['angle']:.0f}°"
                        )
                    else:
                        self.target_info = {}
                        self.log("8111: 无有效战区（无数据或角度过大）")
                except Exception:
                    pass

                self._update_flight_state()
            else:
                # 离开战斗就清空，避免 HUD 上挂着上一局的陈旧数字
                self.target_info = {}
                self._flight_prev = None

            time.sleep(1)

    # ==================== 控制循环 ====================

    def _aim_point(self, fw, fh, bomb_view=False):
        """当前视角下的准心/十字线参考点。"""
        ox = BOMB_AIM_OFFSET_X if bomb_view else AIM_OFFSET_X
        oy = BOMB_AIM_OFFSET_Y if bomb_view else AIM_OFFSET_Y
        ax = max(0, min(fw - 1, fw / 2 + ox))
        ay = max(0, min(fh - 1, fh / 2 + oy))
        return ax, ay

    def _target_error(self, dets, fw, fh, bomb_view=False):
        """统一目标误差：YOLO 战区目标点 - 当前准心参考点。"""
        ax, ay = self._aim_point(fw, fh, bomb_view=bomb_view)
        target = self._get_center_target(dets, fw, fh, bomb_view=bomb_view)
        if target is None:
            return None
        tx, ty = target["cx"], target["cy"]
        return {
            "target": target,
            "fw": fw,
            "fh": fh,
            "aim_x": ax,
            "aim_y": ay,
            "tx": tx,
            "ty": ty,
            "dx": tx - ax,
            "dy": ty - ay,
            "bomb_view": bomb_view,
        }

    def _tracking_stick(self, err):
        """普通/引导视角的追踪输出。"""
        fw, fh = err["fw"], err["fh"]
        dx, dy = err["dx"], err["dy"]
        now = time.time()
        dt = 0.03
        if self._track_last_t is not None:
            dt = max(0.02, min(0.08, now - self._track_last_t))
        self._track_last_t = now

        agg = max(0.0, self.aggression)
        thresh = 0.015 + agg * 0.025
        scl = 0.10 + agg * 0.15
        speed = 1.0 if (abs(dx) > fw * thresh or abs(dy) > fh * thresh) else self.sense

        rx = map_val(dx, fw / 2, speed, MIN_V, scl)
        ry = map_val(-dy, fh / 2, speed, MIN_V, scl)

        nx = dx / max(fw / 2, 1)
        ny = dy / max(fh / 2, 1)
        if abs(dx) > 3:
            self._track_ix += nx * dt * 0.70
            self._track_ix = max(-0.16, min(0.16, self._track_ix))
        else:
            self._track_ix *= 0.70

        if abs(dy) > 3:
            self._track_iy += ny * dt * 0.55
            self._track_iy = max(-0.18, min(0.18, self._track_iy))
        else:
            self._track_iy *= 0.70

        raw_rx, raw_ry = rx, ry
        raw_rx += self._track_ix
        raw_ry -= self._track_iy
        if abs(raw_rx) < 0.001 and abs(dx) > 3:
            raw_rx = math.copysign(MIN_V * 0.45, dx)
        if abs(raw_ry) < 0.001 and abs(dy) > 3:
            raw_ry = math.copysign(MIN_V * 0.45, -dy)
        if abs(dy) > 3 and abs(raw_ry) > 0:
            raw_ry = math.copysign(max(abs(raw_ry), MIN_V * 0.35), raw_ry)

        max_y = 0.42 if self.guide_mode else 0.28
        raw_ry = max(-max_y, min(max_y, raw_ry))
        raw_rx = max(-0.55, min(0.55, raw_rx))

        # 掐掉俯冲正反馈（见文件顶部 GUIDE_DIVE_* 的说明）。必须放在上面那个
        # "最小输出" 兜底之后，否则刚被抬到 MIN_V*0.35 的低头指令又会被这里
        # 的衰减覆盖不到，等于没衰减。
        # raw_ry < 0 是低头、dy > 0 是目标在准心下方，两者同号才是"越压越陡"。
        if self.guide_mode and raw_ry < 0 and dy > 0:
            over = (dy - fh * GUIDE_DIVE_BAND) / max(1.0, fh * GUIDE_DIVE_FADE)
            over = min(1.0, max(0.0, over))
            raw_ry *= 1.0 - over
            # 顺手泄掉俯冲积分。衰减把输出按住的同时积分还在往上顶：`_track_iy`
            # 只在 |dy| <= 3 时才衰减，而这里恰恰是 dy 正大的时候，等于一路顶到
            # 上限 0.18。等目标转回准心附近，攒下的这 0.18 会一次性放出来，
            # 又是一记满杆压头 —— 正是这里要避免的事。
            self._track_iy *= 1.0 - over

        self._smooth_rx = self._slew(self._smooth_rx, raw_rx, 0.03)
        self._smooth_ry = self._slew(self._smooth_ry, raw_ry, 0.025)
        return self._smooth_rx, self._smooth_ry

    def _control_output(self, err):
        """按当前模式生成唯一一组手柄输出。"""
        if self.aim_lock:
            if self._bomb_control_enabled:
                lx, rx, ry = self._bomb_aim_outputs(err["dx"], err["dy"], err["fw"], err["fh"])
                mode = "bomb_fine"
            else:
                rx, ry = self._tracking_stick(err)
                lx = 0.0
                mode = "bomb_coarse"
            return {
                "mode": mode,
                "lx": lx * BOMB_VIEW_LEFT_X_SIGN,
                "ly": self.throttle,
                "rx": rx * BOMB_VIEW_RIGHT_X_SIGN,
                "ry": ry * BOMB_VIEW_RIGHT_Y_SIGN,
            }

        rx, ry = self._tracking_stick(err)
        if self.guide_mode:
            return {
                "mode": "guide",
                "lx": rx,
                "ly": self.throttle,
                "rx": rx * TRACK_RIGHT_X_SIGN,
                "ry": ry * TRACK_RIGHT_Y_SIGN,
            }
        return {
            "mode": "track",
            "lx": rx,
            "ly": self.throttle,
            "rx": rx * TRACK_RIGHT_X_SIGN,
            "ry": ry * TRACK_RIGHT_Y_SIGN,
        }

    def _apply_control_output(self, out):
        if not self.gpad:
            return
        lx = max(-1.0, min(1.0, out["lx"]))
        ly = max(-1.0, min(1.0, out["ly"]))
        rx = max(-1.0, min(1.0, out["rx"]))
        ry = max(-1.0, min(1.0, out["ry"]))
        self.gpad.left_joystick(lx, ly)
        self.gpad.right_joystick(rx, ry)
        self.gpad.update()

    def _neutral_control(self):
        out = {"mode": "neutral", "lx": 0.0, "ly": self.throttle, "rx": 0.0, "ry": 0.0}
        self._last_control_state = out
        self._last_aim_state = None
        self._apply_control_output(out)

    def _log_control(self, err, out):
        now = time.time()
        if now - self._last_log_coord < 1.0:
            return
        self._last_log_coord = now
        target = err["target"]
        raw_point = ""
        if target.get("box_cx") != err["tx"] or target.get("box_cy") != err["ty"]:
            raw_point = (
                f" 原框中心:({int(target.get('box_cx', err['tx']))},"
                f"{int(target.get('box_cy', err['ty']))})"
            )
        self.log(
            f"{out['mode']} 目标点:({int(err['tx'])},{int(err['ty'])}) "
            f"准心点:({int(err['aim_x'])},{int(err['aim_y'])}) "
            f"框:{int(target['w'])}x{int(target['h'])}{raw_point} "
            f"置信:{target['conf']:.2f} 偏差:({int(err['dx'])},{int(err['dy'])}) "
            f"手柄 L:({out['lx']:.2f},{out['ly']:.2f}) R:({out['rx']:.2f},{out['ry']:.2f})"
        )

    def _control_loop(self):
        while self.running:
            if not self.master_on:
                # 总开关关闭（F12）：Gamepad 已经复位且丢弃全部输出，这里空转
                # 即可 —— 既不算控制量，也不发 8111 请求。
                self._last_control_state = {
                    "mode": "master_off", "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0,
                }
                time.sleep(0.1)
                continue
            with self.lock:
                dets = list(self.dets)
                fw, fh = self.fshape or (1, 1)

            if not dets:
                # YOLO 无检测时，尝试用 8111 航向偏差进行粗调转向
                if self.guide_mode and WT8111_OK and not self.aim_lock:
                    try:
                        info = get_target_bombing_zone()
                        if info and abs(info["bearing_diff"]) > 15:
                            stick_x = map_val(info["bearing_diff"], 90, 1.0, MIN_V, 0.55)
                            out = {
                                "mode": "guide",
                                "lx": stick_x,
                                "ly": self.throttle,
                                "rx": stick_x * TRACK_RIGHT_X_SIGN,
                                "ry": 0.0,
                            }
                            self._last_aim_state = None
                            self._last_control_state = out
                            self._apply_control_output(out)
                            time.sleep(0.03)
                            continue
                    except Exception:
                        pass
                self._neutral_control()
                time.sleep(0.03)
                continue

            err = self._target_error(dets, fw, fh, bomb_view=self.aim_lock)
            if err is None:
                # YOLO 有检测但无有效目标，同样尝试 8111 粗调
                if self.guide_mode and WT8111_OK and not self.aim_lock:
                    try:
                        info = get_target_bombing_zone()
                        if info and abs(info["bearing_diff"]) > 15:
                            stick_x = map_val(info["bearing_diff"], 90, 1.0, MIN_V, 0.55)
                            out = {
                                "mode": "guide",
                                "lx": stick_x,
                                "ly": self.throttle,
                                "rx": stick_x * TRACK_RIGHT_X_SIGN,
                                "ry": 0.0,
                            }
                            self._last_aim_state = None
                            self._last_control_state = out
                            self._apply_control_output(out)
                            time.sleep(0.03)
                            continue
                    except Exception:
                        pass
                self._neutral_control()
                time.sleep(0.03)
                continue

            out = self._control_output(err)
            self._last_aim_state = {**err, "time": time.time()}
            self._last_control_state = out
            self._apply_control_output(out)
            self._log_control(err, out)
            time.sleep(0.03)

    # ==================== 全自动投弹 ====================

    def _close_weapon_selector(self):
        """按 B 关闭武器选择模式，避免它吞掉批量投弹的 LB+X。

        调用点在**进投弹视角时**（见 _auto_bomb_sequence 的阶段 B 开头），不是投弹前。
        原来放在 _drop_bombs 里、离 LB+X 只剩 0.25 秒 —— 那是最不该按 B 的位置：
        投弹本来就是一套按键，B 挤进来只会多一层冲突。

        WT 键位（见 wt_controls/*.blk）里 B 单独绑定 ID_EXIT_SHOOTING_CYCLE_MODE，
        投弹视角下武器选择界面若开着，LB+X 不会触发投弹。
        注意：B 同时还绑着 ID_TACTICAL_MAP（战术地图），若发现按 B 会弹地图，
        把这条注释掉并改绑其它键。当前配置里
        USEROPT_HOLD_BUTTON_FOR_TACTICAL_MAP:b=yes，即**长按**才开地图，
        这里用的是 tap(0.1) 短按，正常不该触发；但这条没在游戏里验证过。
        """
        if not self.gpad:
            return
        self.log("关闭武器选择模式 (B)")
        self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.1)
        time.sleep(0.25)

    def _drop_bombs(self, count, keep_running=False):
        """快速点按 LB+X 多次，每颗炸弹触发一次

        keep_running=True: 不设置 running=False，用于循环轰炸模式

        这里**只做投弹这一件事**：两个调用点都在投弹视角的瞄准循环里，
        aim_lock 早已为 True，所以不需要（以前也没有真正生效过）自己按 Y 进视角；
        武器选择模式也已经在进视角时就关掉了，不能再在这里按 B。

        **不再碰 `bombs_depleted`。** 它的含义是**本局轰炸流程已收尾**，只由
        `_finish_bomb_cycles` 置位；"投了一轮"和"炸弹用尽"是两件事 —— 以前这里
        投完一轮就置 True，阶段 C 再把它清掉，于是 `has_next` 恒成立、循环永远
        走不到收尾，自毁不执行。
        """
        if not self.gpad:
            self.log("虚拟手柄不可用，无法投弹")
            return
        if not keep_running:
            self.running = False
        for i in range(count):
            self.gpad.press_lb_x()
            time.sleep(0.15)
            self.gpad.release_lb_x()
            time.sleep(0.12)

    def _get_center_target(self, dets, fw, fh, bomb_view=False):
        """优先选择最左和最右的两个战区，取其中距离准心最近的那个。

        辅助锁定模式（lock_side）：
          leftmost  → 强制选最左战区
          rightmost → 强制选最右战区
          没有锁定偏好时使用原来的双目标逻辑。
        """
        if not dets:
            return None

        # ---- 过滤掉太小（非战区）的检测框 ----
        min_size = 25
        dets = [d for d in dets if d.get("w", 0) >= min_size and d.get("h", 0) >= min_size]
        if not dets:
            return None

        # ---- 辅助锁定模式：按锁定方向选目标 ----
        if self.lock_side == "leftmost":
            # 选最左边的战区（确保 x 方向偏好，同时选置信度高的）
            best = min(dets, key=lambda d: (d["cx"], -d.get("conf", 0)))
            return best
        elif self.lock_side == "rightmost":
            # 选最右边的战区
            best = max(dets, key=lambda d: (d["cx"], d.get("conf", 0)))
            return best

        # ---- 原逻辑：双目标择优 ----
        if len(dets) < 2:
            cx, cy = self._aim_point(fw, fh, bomb_view=bomb_view)
            return min(
                dets,
                key=lambda d: (
                    (d["cx"] - cx) ** 2 + (d["cy"] - cy) ** 2
                ) / max(d.get("conf", 0.1), 0.1),
            )
        left = min(dets, key=lambda d: d["cx"])
        right = max(dets, key=lambda d: d["cx"])
        cx, cy = self._aim_point(fw, fh, bomb_view=bomb_view)
        return min(
            [left, right],
            key=lambda d: (d["cx"] - cx) ** 2 + (d["cy"] - cy) ** 2,
        )

    def _reset_bomb_aim_controller(self):
        """重置投弹视角姿态控制器。"""
        self._fine_lx = 0.0
        self._fine_rx = 0.0
        self._fine_ry = 0.0
        self._bomb_dx = 0.0
        self._bomb_dy = 0.0
        self._bomb_ix = 0.0
        self._bomb_iy = 0.0
        self._bomb_last_dx = None
        self._bomb_last_dy = None
        self._bomb_last_t = None
        self._bomb_control_enabled = False

    def _select_lock_target(self, exclude_zones=None):
        """使用 8111 API 选择锁定目标战区。

        根据 lock_side 选择:
          nearest   → 距离玩家最近的战区（默认）
          leftmost  → 最左战区（归一化 x 最小）
          rightmost → 最右战区（归一化 x 最大）
          auto      → 第一个找到的战区

        自动跳过"有队友正在飞向"的战区（is_teammate_heading_to_zone），
        避免和队友抢战区。

        参数:
            exclude_zones — 额外排除的战区坐标列表 [{nx, ny}, ...] 或 None

        返回 {nx, ny, wx, wy} 或 None
        """
        if not WT8111_OK:
            self.log(f"锁定模式: 8111 不可用，使用 YOLO 纯视觉追踪")
            return None

        all_zones = get_bombing_zones()
        if not all_zones:
            self.log(f"锁定模式: 8111 未找到任何战区，使用 YOLO 追踪")
            return None

        # 过滤掉有队友在飞向的战区
        clean_zones = []
        skipped_contest = 0
        for z in all_zones:
            # 检查 exclude_zones（如已炸过的战区）
            if exclude_zones:
                excluded = any(
                    abs(z["x"] - ez["nx"]) < 0.001 and abs(z["y"] - ez["ny"]) < 0.001
                    for ez in exclude_zones
                )
                if excluded:
                    continue
            # 检查队友是否也在飞向这个战区
            count, _ = is_teammate_heading_to_zone(z["x"], z["y"], angle_threshold=45)
            if count > 0:
                skipped_contest += 1
                continue
            clean_zones.append(z)

        if skipped_contest > 0:
            self.log(f"跳过 {skipped_contest} 个有队友在飞向的战区")

        if not clean_zones:
            self.log(f"所有战区都有队友在飞向，选择最不拥挤的")
            # 退而求其次：选队友最少的
            scored = []
            for z in all_zones:
                count, _ = is_teammate_heading_to_zone(z["x"], z["y"], angle_threshold=45)
                scored.append((count, z))
            scored.sort(key=lambda x: x[0])  # 按队友数量升序
            clean_zones = [z for _, z in scored]

        if self.lock_side == "nearest":
            # 选距离玩家最近的战区
            try:
                player = get_player()
            except Exception:
                player = None
            if player:
                px, py = player["x"], player["y"]
                target = min(clean_zones, key=lambda z: (z["x"] - px) ** 2 + (z["y"] - py) ** 2)
                side_name = "最近"
            else:
                target = clean_zones[0]
                side_name = "首个"
        elif self.lock_side == "leftmost":
            target = min(clean_zones, key=lambda z: z["x"])
            side_name = "最左"
        elif self.lock_side == "rightmost":
            target = max(clean_zones, key=lambda z: z["x"])
            side_name = "最右"
        else:  # auto
            target = clean_zones[0]
            side_name = "首个"

        # 获取世界坐标
        try:
            map_info = __import__("common.wt8111", fromlist=["fetch_json"]).fetch_json("/map_info.json")
        except Exception:
            map_info = None
        if map_info and map_info.get("valid"):
            mmax = map_info["map_max"]
            mmin = map_info["map_min"]
            map_sx = max(1.0, mmax[0] - mmin[0])
            map_sy = max(1.0, mmax[1] - mmin[1])
            wx = mmin[0] + map_sx * target["x"]
            wy = mmin[1] + map_sy * target["y"]
        else:
            wx = wy = 0.0

        self.locked_target = {
            "nx": target["x"],
            "ny": target["y"],
            "wx": wx,
            "wy": wy,
        }
        self.log(f"锁定目标: {side_name}战区 ({target['x']:.4f}, {target['y']:.4f})")
        return self.locked_target

    def _heading_to_locked_target(self):
        """计算当前航向与锁定目标之间的偏差（度）

        返回 (heading_error, dist_m) 或 None
        """
        if not self.locked_target or not WT8111_OK:
            return None
        try:
            player = get_player()
            if not player:
                return None
            bearing, dist_norm = get_target_bearing(
                player["x"], player["y"],
                self.locked_target["nx"], self.locked_target["ny"]
            )
            error = (bearing - player["heading"] + 540) % 360 - 180
            # 距离用世界坐标
            map_size = get_map_size()
            dist_m = dist_norm * map_size
            return error, dist_m
        except Exception:
            return None

    def _navigate_to_locked_target(self):
        """引导阶段：使用 8111 飞向锁定目标（替代 YOLO-only 引导）

        如果检测到队友也在飞向同一个战区（持续存在），
        自动切换目标到另一个战区。

        返回 True 表示已接近目标（可以进投弹视角）
        """
        view_distance = max(800, BOMB_VIEW_DISTANCE)
        last_log = 0.0
        guide_start = time.time()
        close_count = 0

        # 队友争抢检测
        _last_contest_check = 0.0
        _contest_streak = 0

        self.log(f"引导飞向锁定目标...")

        while self.running and self.cycle_active:
            self.aggression = 0.7

            # ---- 队友争抢检测（每 2 秒一次） ----
            now = time.time()
            if now - _last_contest_check >= 2.0 and self.locked_target:
                _last_contest_check = now
                try:
                    count, _ = is_teammate_heading_to_zone(
                        self.locked_target["nx"], self.locked_target["ny"],
                        angle_threshold=45
                    )
                    if count > 0:
                        _contest_streak += 1
                        if _contest_streak >= 3:  # 连续 3 次（6秒）检测到有队友同向
                            self.log(f"队友也在飞向当前战区，且持续 {_contest_streak*2}s → 切换目标")
                            # 重新选择目标（排除当前目标）
                            old_target = self.locked_target
                            self._select_lock_target(exclude_zones=[old_target])
                            if self.locked_target:
                                self.log(
                                    f"切换后新目标: ({self.locked_target['nx']:.4f}, "
                                    f"{self.locked_target['ny']:.4f})"
                                )
                                # 重置计时
                                guide_start = time.time()
                                close_count = 0
                                _contest_streak = 0
                            else:
                                self.log("无替代目标，维持原目标")
                                self.locked_target = old_target
                                _contest_streak = 0
                    else:
                        _contest_streak = 0  # 队友不再飞向这里，清零
                except Exception:
                    pass

            # 8111 测距：接近锁定目标
            if WT8111_OK and self.locked_target:
                try:
                    info = self._heading_to_locked_target()
                    if info:
                        error, dist_m = info
                        # 已接近 → 进投弹视角
                        if dist_m <= view_distance:
                            self.log(f"距离锁定目标 {dist_m:.0f}m ≤ {view_distance}m → 进入投弹视角")
                            return True
                        # 航向已对准 → 允许提前一点切视角（切视角本身要 ~1s，
                        # 按 250m/s 算也就提前 250m，这里给 500m 余量）。
                        # 原来这里是 view_distance * 1.5，10km 阈值时等于 15km，
                        # 只要机头对着目标就在 15km 外切投弹视角，太远了。
                        if abs(error) < 15 and dist_m < view_distance + 500:
                            close_count += 1
                            if close_count >= 5:  # 连续 5 次都在范围内
                                self.log(
                                    f"正对且接近锁定目标（{dist_m:.0f}m, 偏{error:.0f}°）→ 进入投弹视角"
                                )
                                return True
                        else:
                            close_count = 0
                except Exception:
                    pass

            now = time.time()
            if now - last_log >= 1.0:
                last_log = now
                guide_elapsed = time.time() - guide_start
                if WT8111_OK and self.locked_target:
                    try:
                        info = self._heading_to_locked_target()
                        if info:
                            error, dist_m = info
                            side = "左" if error < 0 else "右"
                            self.log(f"引导中 {guide_elapsed:.0f}s: {dist_m/1000:.1f}km ({side}偏 {abs(error):.0f}°)")
                        else:
                            self.log(f"引导中 {guide_elapsed:.0f}s: 等待距离 ≤ {view_distance}m")
                    except Exception:
                        self.log(f"引导中 {guide_elapsed:.0f}s: 等待距离 ≤ {view_distance}m")
                else:
                    self.log(f"引导中 {guide_elapsed:.0f}s: 等待距离 ≤ {view_distance}m")

            time.sleep(0.2)

        return False

    def _turn_around_to_target(self):
        """掉头飞向锁定目标：缓慢转向直到大致面对目标

        使用第三人称视角，利用 8111 航向偏差慢速转弯。
        返回 True 表示已完成转向
        """
        if not WT8111_OK or not self.locked_target:
            self.log("掉头: 8111 或锁定目标不可用，无法掉头")
            return False

        # 切回第三人称
        self.aim_lock = False
        self.guide_mode = True
        self.throttle = 0.5  # 掉头时中等油门

        if self.gpad:
            self.gpad.return_to_third_person()

        self.log("掉头: 开始缓慢转向...")
        turn_start = time.time()
        last_log = 0.0
        aligned_frames = 0

        while self.running and self.cycle_active:
            try:
                info = self._heading_to_locked_target()
                if info is None:
                    time.sleep(0.5)
                    continue
                error, dist_m = info
            except Exception:
                time.sleep(0.5)
                continue

            now = time.time()
            elapsed = now - turn_start
            if now - last_log >= 2.0:
                last_log = now
                side = "左" if error < 0 else "右"
                self.log(f"掉头中 {elapsed:.0f}s: {dist_m/1000:.1f}km ({side}偏 {abs(error):.0f}°)")

            # 已经基本面对目标（±20°以内）→ 完成掉头
            if abs(error) < 20:
                aligned_frames += 1
                if aligned_frames >= 3:  # 连续 3 帧确认
                    self.log(f"掉头完成（偏 {error:.0f}°），距离 {dist_m/1000:.1f}km")
                    return True
            else:
                aligned_frames = 0

            # 生成转向摇杆值
            if self.gpad:
                # 缓转弯：TURN_SPEED 控制速度，error 决定方向
                turn_stick = TURN_SPEED * (-1.0 if error > 0 else 1.0)
                # 误差大时转快点，接近时转慢点
                turn_scale = min(abs(error) / 45.0, 1.0)
                turn_stick *= turn_scale
                turn_stick = math.copysign(max(abs(turn_stick), TURN_SPEED * 0.4), turn_stick)
                turn_stick = max(-0.5, min(0.5, turn_stick))

                self.gpad.left_joystick(turn_stick, self.throttle)
                self.gpad.right_joystick(0, 0)
                self.gpad.update()

            # 超时保护（60秒转不过去就放弃）
            if elapsed > 60:
                self.log("掉头超时 60s，放弃")
                return False

            time.sleep(0.1)

        return False

    def _slew(self, cur, target, step):
        return cur + max(-step, min(step, target - cur))

    def _bomb_aim_outputs(self, dx, dy, fw, fh):
        """把投弹视角偏差转换为平滑姿态/视角输出。"""
        now = time.time()
        dt = 0.03
        if self._bomb_last_t is not None:
            dt = max(0.02, min(0.08, now - self._bomb_last_t))

        if self._bomb_last_dx is None:
            vx = 0.0
            vy = 0.0
        else:
            vx = ((dx - self._bomb_last_dx) / max(fw / 2, 1)) / dt
            vy = ((dy - self._bomb_last_dy) / max(fh / 2, 1)) / dt

        self._bomb_last_dx = dx
        self._bomb_last_dy = dy
        self._bomb_last_t = now

        nx = dx / max(fw / 2, 1)
        ny = dy / max(fh / 2, 1)

        # 低通滤波：YOLO 框抖动时不直接把抖动打进飞机姿态。
        self._bomb_dx = self._bomb_dx * 0.70 + nx * 0.30
        self._bomb_dy = self._bomb_dy * 0.70 + ny * 0.30

        tune = 0.75 + BOMB_VIEW_SENSE
        fine_x_deadzone = 2
        fine_y_deadzone = 2

        if abs(dx) > fine_x_deadzone:
            # 积分项专门消除“总差一点”的固定偏差。
            self._bomb_ix += nx * dt * 1.20
            self._bomb_ix = max(-0.18, min(0.18, self._bomb_ix))
        else:
            self._bomb_ix *= 0.65

        if abs(dy) > fine_y_deadzone:
            self._bomb_iy += ny * dt * 0.65
            self._bomb_iy = max(-0.12, min(0.12, self._bomb_iy))
        else:
            self._bomb_iy *= 0.70

        # 水平：投弹镜右摇杆负责最后精修，左摇杆只做较大偏差的飞机姿态辅助。
        heading_cmd = (self._bomb_dx * 0.18 + vx * 0.004) * tune
        sight_x_cmd = (self._bomb_dx * 1.10 + vx * 0.010 + self._bomb_ix) * tune

        # 垂直：投弹镜俯仰独立修正。dy 为正表示目标在中心下方，沿用原控制符号取反。
        sight_y_cmd = -(self._bomb_dy * 0.72 + vy * 0.014 + self._bomb_iy) * tune

        if abs(dx) > PIXEL_DEV * 2 and abs(heading_cmd) > 0:
            heading_cmd = math.copysign(max(abs(heading_cmd), MIN_V * 0.30), heading_cmd)
        if abs(dx) > fine_x_deadzone and abs(sight_x_cmd) > 0:
            sight_x_cmd = math.copysign(max(abs(sight_x_cmd), MIN_V * 0.55), sight_x_cmd)
        if abs(dy) > PIXEL_DEV_Y and abs(sight_y_cmd) > 0:
            sight_y_cmd = math.copysign(max(abs(sight_y_cmd), MIN_V * 0.45), sight_y_cmd)

        # 偏差越接近中心，输出上限越低，防止穿越中心后反复摆。
        near_x = abs(dx) <= PIXEL_DEV * 2
        near_y = abs(dy) <= PIXEL_DEV_Y * 2
        max_lx = 0.08 if near_x else 0.24
        max_rx = 0.28 if near_x else 0.42
        max_ry = 0.16 if near_y else 0.32

        heading_cmd = max(-max_lx, min(max_lx, heading_cmd))
        sight_x_cmd = max(-max_rx, min(max_rx, sight_x_cmd))
        sight_y_cmd = max(-max_ry, min(max_ry, sight_y_cmd))

        # 输出斜率限制：飞机姿态变化慢一点，视角变化稍快一点。
        self._fine_lx = self._slew(self._fine_lx, heading_cmd, 0.010)
        self._fine_rx = self._slew(self._fine_rx, sight_x_cmd, 0.055)
        self._fine_ry = self._slew(self._fine_ry, sight_y_cmd, 0.032)

        return self._fine_lx, self._fine_rx, self._fine_ry

    def _wait_master_on(self):
        """F12 关掉总开关时原地等，而不是当成"目标被抢"或"本局结束"。

        总开关关闭时 `_control_loop` 提前 return，**不再更新 `_last_aim_state`**
        （见 `_control_loop` 开头那段）。投弹视角的瞄准循环看到的是一个过期状态，
        于是 STOLEN_TIMEOUT 秒后误判成"目标已丢失（战区可能被抢）" → 换目标 →
        轮次收尾 → 飞往机场自毁 —— 而按 F12 的意思恰恰是"立刻全部停下"。

        日志证据：投弹视角里按 F12 的三次（10:30:39 / 11:22:24 / 13:02:10）
        **全部**触发了这条误判，最近那次直接把整局带进了自毁。引导阶段按 F12
        不会误判（引导循环不看 `_last_aim_state`），但会继续飞——12:44:45 那次
        F12 关闭期间照样进了投弹视角，所以引导循环也要调这个。

        返回 False 表示等待期间离开了战斗或已停止，调用方应当退出。
        """
        if self.master_on:
            return True
        self.log("总开关关闭（F12）→ 暂停轰炸流程（不是目标丢失），等待恢复")
        while not self.master_on:
            if not self.running or not self.cycle_active:
                return False
            time.sleep(0.1)
        self.log("总开关恢复 → 继续轰炸流程")
        return True

    def _auto_bomb_sequence(self):
        """全自动投弹循环（带辅助锁定 + 循环轰炸）

        流程:
          1. 选择锁定目标（最左/最右战区）
          2. 循环 bomb_cycles 轮:
             a. 引导阶段 → 飞向锁定目标
             b. 进入投弹视角 → 精确瞄准 → 投弹
             c. 退出投弹视角，切回第三人称
             d. 如果还有下一轮: 掉头重新导航
          3. 最后一轮结束: 飞往机场或减速巡航
        """
        # ---- 选择锁定目标 ----
        self.locked_target = None
        if self.lock_side in ("nearest", "leftmost", "rightmost", "auto"):
            self._select_lock_target()

        self.cycle_active = True
        self.cycle_count = 0
        self.bombs_depleted = False
        total_cycles = self.bomb_cycles if self.bomb_cycles > 0 else 9999

        max_switch_per_cycle = 2   # 同一轮内最多因"被抢"切换几次目标
        finished = False
        # 注意：切换目标会 continue 回循环顶部，所以重试计数只能在
        # "确认进入新一轮"时清零（见下面 has_next 分支），不能放在循环开头。
        self._cycle_switches = 0

        while self.cycle_active and self.running:
            # ---- 本局是否已被判定收尾 ----
            # `bombs_depleted` 现在只在收尾/中止时置位（不再是"某轮投完了"），
            # 所以这里是个兜底，循环的正常出口是下面的轮次判断。
            if self.bombs_depleted:
                self.log("本局轰炸已收尾，停止循环")
                finished = True
                break

            self.cycle_count += 1
            self.log(f"===== 第 {self.cycle_count}/{total_cycles} 轮轰炸 =====")

            # ---------------------------------------------------------------
            # 阶段 A: 引导 — 飞向目标
            # ---------------------------------------------------------------
            self.guide_mode = True
            self.throttle = 1.0
            self.log("油门全开")
            self._reset_bomb_aim_controller()

            guide_ok = False
            if WT8111_OK and self.locked_target:
                # 使用 8111 导航到锁定目标
                guide_ok = self._navigate_to_locked_target()
            else:
                # 无 8111 或锁定目标：用原 YOLO 引导逻辑
                view_distance = max(800, BOMB_VIEW_DISTANCE)
                self.log(f"引导阶段: YOLO 视觉追踪...")
                last_guide_log = 0.0
                guide_start = time.time()
                last_dist_time = guide_start   # 最近一次拿到 8111 测距的时间

                while self.running and self.cycle_active:
                    # F12 关闭总开关时停在这里。引导循环本身不看 `_last_aim_state`，
                    # 所以不会误判成"目标被抢"，但它会照常飞、照常满足 8111 距离
                    # 条件开投弹视角（实测 12:44:45 按 F12，12:44:47 就进了）——
                    # 而总开关的意思是"全部停下"。
                    if not self._wait_master_on():
                        break

                    self.aggression = 0.7

                    # 8111 距离检测（即使无锁定目标也可用）
                    if WT8111_OK:
                        try:
                            info = get_target_bombing_zone()
                            if info:
                                last_dist_time = time.time()
                                if info["dist_m"] <= BOMB_VIEW_DISTANCE:
                                    self.log(f"距离战区 {info['dist_m']:.0f}m ≤ {BOMB_VIEW_DISTANCE}m → 进入投弹视角")
                                    guide_ok = True
                                    break
                        except Exception:
                            pass

                    # YOLO 框占比检测：检测框面积 / 屏幕面积 ≥ GUIDE_BOX_RATIO
                    # 适用于 8111 未找到战区时的备用退出条件
                    with self.lock:
                        local_dets = list(self.dets)
                        local_fw, local_fh = self.fshape or (1, 1)
                    if local_dets:
                        best = max(local_dets, key=lambda d: d.get("conf", 0))
                        bw, bh = best.get("w", 0), best.get("h", 0)
                        ratio = (bw * bh) / (local_fw * local_fh)
                        if ratio >= GUIDE_BOX_RATIO:
                            self.log(f"YOLO 框占比 {ratio:.4f} ≥ {GUIDE_BOX_RATIO}，进入投弹视角")
                            guide_ok = True
                            break

                    # 超时兜底：只在连续 60s 拿不到 8111 测距时才按时间强切。
                    # 有测距时必须等距离条件满足 —— 否则飞机还在 16km 外、按
                    # 400km/h 要飞 100s 才到 5km，60s 一到就被强切进投弹视角，
                    # 表现就是"离得很远就开了投弹视角"，而且调小阈值也没用
                    # （超时会先触发）。
                    if time.time() - last_dist_time > 60:
                        self.log("引导超时 60s（期间无 8111 测距），强制进入投弹视角")
                        guide_ok = True
                        break

                    now = time.time()
                    if now - last_guide_log >= 1.0:
                        last_guide_log = now
                        guide_elapsed = time.time() - guide_start
                        self.log(f"引导中 {guide_elapsed:.0f}s: 等待战区靠近...")

                    time.sleep(0.2)

                if not guide_ok and self.running and self.cycle_active:
                    # YOLO 引导未触发，但 running 仍在 → 强制进入投弹视角
                    self.log("YOLO 引导未触发，强制进入投弹视角")
                    guide_ok = True

            self.guide_mode = False

            if not self.running or not self.cycle_active:
                self.log("引导已中断（离开战斗或手动停止）")
                self.aim_lock = False
                self.aggression = 0.0
                if not self.cycle_active:
                    self.bombs_depleted = True
                break

            # ---------------------------------------------------------------
            # 阶段 B: 投弹视角 — 瞄准 → 投弹
            # ---------------------------------------------------------------
            # ---- 开弹舱（一局一次）----
            # **在这里开，不在进战斗时开。** 舱门开着飞会一直增加阻力，而引导阶段
            # 可能长达 100 秒以上（实测 logs/20260928_125002.log 102 秒），那段时间
            # 舱门没有任何用处；从这里到真正投弹还有几十秒（实测 28 秒），足够开完。
            #
            # 一局只拉一次：LT+RT 是"开/关"**切换**，没有可靠的状态回读（HUD 上那是
            # 图标不是文字，8111 也不提供），多拉一次就是把刚开的门又关上 —— 旧代码
            # 每轮扫描都拉，第 2 次正好把门关回去。出生时舱门必关 → 拉一次即开。
            #
            # 判 `master_on`：`pull_bay_lever()` 在总开关关闭时是空操作，不判就会把
            # `_bay_pulled` 白白置上，整局都不再开舱。
            if self.gpad and not self._bay_pulled and self.master_on:
                self.log("进入投弹视角 → 打开弹舱 (LT+RT)")
                self.gpad.pull_bay_lever()
                self._bay_pulled = True
            self.aim_lock = True
            self._reset_bomb_aim_controller()
            self._bomb_control_enabled = True
            self.log("进入投弹视角")
            if self.gpad:
                self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)
            time.sleep(0.5)
            self.throttle = 0.55
            # 提前关掉武器选择模式：它开着的话后面 LB+X 不会触发投弹。
            # 放在这里而不是 _drop_bombs 里，是因为从这里到真正投弹还有几十秒
            # （实测等 |dy| <= pixel_deviation_y 用了 28 秒），B 的副作用有时间散掉，
            # 也不会和投弹那套按键挤在同一瞬间。
            # 每个轰炸轮次只进一次投弹视角，所以这里天然就是"每轮按一次"。
            self._close_weapon_selector()

            self.log("投弹视角：持续跟踪并修正偏差，稳定对准后投弹")
            dropped = False
            target_stolen = False
            # 总开关一直关着、期间又离开了战斗/手动停止 → 中断，不是"本轮结束"。
            # 与 `finished` 的区别见循环出口：`finished` 会触发飞往机场（自毁）。
            interrupted = False
            stable_hits = 0
            last_target_log = 0.0
            stale_since = None
            prev_dy = None

            while self.running:
                now = time.time()
                state = self._last_aim_state
                out = self._last_control_state or {}
                if not state or not state.get("bomb_view") or now - state.get("time", 0) > 0.25:
                    # 先摘掉 F12：总开关关闭时 `_control_loop` 不再更新
                    # `_last_aim_state`，这里看到的"过期"是总开关造成的，不是目标
                    # 丢了。不摘的话 STOLEN_TIMEOUT 秒后就会判成"战区被抢"。
                    if not self.master_on:
                        if not self._wait_master_on():
                            interrupted = True
                            break
                        # 暂停期间的时间不能算进丢失计时，否则一恢复就立刻超时
                        stale_since = None
                        continue
                    if stale_since is None:
                        stale_since = now
                    if now - stale_since > 1.5:
                        self._reset_bomb_aim_controller()
                        self._bomb_control_enabled = True
                    if now - stale_since > STOLEN_TIMEOUT:
                        self.log(f"目标连续 {STOLEN_TIMEOUT}s 未见"
                                 f"（战区被抢 / 检测丢失）→ 放弃当前目标")
                        target_stolen = True
                        break
                    stable_hits = 0
                    time.sleep(0.05)
                    continue
                stale_since = None
                self._bomb_control_enabled = True

                dx, dy = state["dx"], state["dy"]
                aligned = abs(dy) <= PIXEL_DEV_Y
                crossed = (
                    prev_dy is not None
                    and prev_dy * dy <= 0
                    and abs(dy) <= PIXEL_DEV_Y * 2
                )

                now2 = time.time()
                if now2 - last_target_log >= 0.3:
                    last_target_log = now2
                    ok_y = abs(dy) <= PIXEL_DEV_Y
                    why = []
                    if not ok_y: why.append(f"垂直偏差{int(dy)}>{PIXEL_DEV_Y}")
                    self.log(
                        f"投弹条件 {'✓' if aligned else '✗'} "
                        f"偏差({int(dx)},{int(dy)}) "
                        f"{' 未通过: ' + ' '.join(why) if why else ''}"
                    )

                if aligned:
                    stable_hits += 1
                    if stable_hits >= 1:
                        self.log(f"进入投弹窗口（偏差 {int(dx)},{int(dy)}px）→ 投弹")
                        self._drop_bombs(self.bomb_count, keep_running=True)
                        dropped = True
                        break
                elif crossed:
                    self.log(
                        f"穿越投弹线（偏差 {int(dx)},{int(dy)}px，上一帧dy {int(prev_dy)}px）→ 投弹"
                    )
                    self._drop_bombs(self.bomb_count, keep_running=True)
                    dropped = True
                    break
                else:
                    stable_hits = 0
                prev_dy = dy

                if now - last_target_log >= 1.0:
                    last_target_log = now
                    target = state["target"]
                    self.log(
                        f"投弹状态 偏差:({int(dx)},{int(dy)}) "
                        f"目标点:({int(state['tx'])},{int(state['ty'])}) "
                        f"准心点:({int(state['aim_x'])},{int(state['aim_y'])}) "
                        f"框:{int(target['w'])}x{int(target['h'])} "
                        f"模式:{out.get('mode', '?')} "
                        f"连续:{stable_hits}"
                    )

                time.sleep(0.03)

            self._reset_bomb_aim_controller()

            # ---- 总开关关闭期间中断：直接退出轮次循环 ----
            # **绝不能落到阶段 C**：那里会给 `finished` 置位，而
            # `finished → _finish_bomb_cycles() → _navigate_to_airfield`，
            # 也就是"人已经按了 F12 叫停、飞机还自己起飞去机场自毁"。
            # 这条路径也不该算作"本轮结束"——`dropped` 保持原值即可，
            # 因为中断的轮次既不前进也不收尾。
            if interrupted:
                self.log("轰炸流程中断（总开关关闭期间离开战斗 / 手动停止）")
                self.aim_lock = False
                self.guide_mode = False
                self.aggression = 0.0
                break

            # ---- 战区被抢：切换目标重新引导（不计入已完成轮次） ----
            if target_stolen:
                self.aim_lock = False
                self.guide_mode = False
                self.aggression = 0.0
                self.throttle = 0.5

                self._cycle_switches += 1
                if self._cycle_switches > max_switch_per_cycle:
                    # 反复丢失目标（多半是已经飞过战区了）→ 本轮按完成处理。
                    # 这里不能再 cycle_count -= 1：否则轮次计数来回加减，流程
                    # 会永远卡在最后一轮，走不到飞往机场（自毁）那一步。
                    self.log(
                        f"本轮已切换 {self._cycle_switches - 1} 次目标仍丢失 → 本轮按完成处理"
                    )
                    # 落到阶段 C，由它统一退出投弹视角
                else:
                    # 退出投弹视角 → 切回第三人称
                    if self.gpad:
                        time.sleep(0.8)
                        self.gpad.return_to_third_person()

                    # 重新选择锁定目标（其他战区）
                    self.locked_target = None
                    if self.lock_side in ("nearest", "leftmost", "rightmost", "auto"):
                        if WT8111_OK:
                            self._select_lock_target()

                    # 降 cycle_count 使本轮不计入已完成次数
                    self.cycle_count -= 1
                    if self.locked_target:
                        nx = self.locked_target.get("nx", "?")
                        self.log(f"切换目标 → 战区 {nx}")
                    else:
                        self.log("切换目标 → YOLO 视觉追踪")
                    # 回到引导阶段（跳过阶段 C 的其余部分）
                    continue

            # ---------------------------------------------------------------
            # 阶段 C: 投弹后处理
            # ---------------------------------------------------------------
            self.aim_lock = False
            self.guide_mode = False
            self.aggression = 0.0

            # 退出投弹视角 → 切回第三人称
            if self.gpad:
                time.sleep(0.8)
                self.gpad.return_to_third_person()

            if dropped:
                self.log(f"第 {self.cycle_count} 轮投弹完成")
            else:
                self.log(f"第 {self.cycle_count} 轮未投弹（可能未锁定）")

            # 判断是否还有下一轮 —— 只看轮次和"本轮到底投下弹没有"，不看存量。
            #
            # `dropped` 是**终止性保证**，不是优化。没有它，一轮里目标被抢到切换
            # 次数用尽的路径会先把 `cycle_count -= 1` 抵消掉，于是
            # `cycle_count < total_cycles` 恒成立：实测第 2 轮打了三次
            # "第 2/2 轮轰炸"，投弹视角进进出出、就是不去机场。加上它之后
            # 每次循环只有两种出口 —— 投下弹（轮次前进）或收尾（飞往机场），
            # 不存在第三种。顺带也修掉"一轮完全没投出去还要空飞下一轮"。
            has_next = (
                self.cycle_active
                and self.running
                and dropped
                and self.cycle_count < total_cycles
            )

            if has_next:
                # 掉头 → 下一轮
                self.log(f"准备第 {self.cycle_count + 1} 轮轰炸...")
                self.throttle = 0.5
                self._cycle_switches = 0   # 新一轮，重试计数清零

                # 重新选择下一个锁定目标（排除已炸过的战区）
                if self.lock_side in ("nearest", "leftmost", "rightmost", "auto"):
                    if WT8111_OK:
                        self.locked_target = None
                        self._select_lock_target()
                        if self.locked_target:
                            nx = self.locked_target.get("nx", "?")
                            self.log(f"下一轮目标 → 战区 {nx}")
                        else:
                            self.log("下一轮: 无可用锁定目标，使用 YOLO 视觉追踪")

                if WT8111_OK and self.locked_target:
                    turned = self._turn_around_to_target()
                    if not turned:
                        self.log("掉头失败，尝试继续...")
                        # 即使掉头失败也继续试
                else:
                    # 无 8111：简单掉头（反向推杆一段时间）
                    self.log("无 8111 数据: 执行简单掉头")
                    if self.gpad:
                        turn_dir = -1.0 if self.lock_side == "rightmost" else 1.0
                        for _ in range(60):  # ~6秒
                            if not self.running:
                                break
                            self.gpad.left_joystick(turn_dir * TURN_SPEED, 0.5)
                            self.gpad.right_joystick(0, 0)
                            self.gpad.update()
                            time.sleep(0.1)

                # 重新启动运行（_drop_bombs 可能设置了 running=False）
                if not self.running:
                    self.running = True
                    # 重启控制循环
                    threading.Thread(target=self._recog_loop, daemon=True).start()
                    threading.Thread(target=self._control_loop, daemon=True).start()

                self.log(f"开始第 {self.cycle_count + 1} 轮引导...")

            else:
                # 最后一轮结束（或本轮没投出去 —— 战区已炸完/抢不到，再掉头也是空飞）
                if not dropped:
                    self.log(f"第 {self.cycle_count} 轮未投下弹，继续空飞没有意义 → 本局收尾")
                self.log("所有轰炸轮次完成")
                finished = True
                break

        # 轮次跑满 → 统一收尾（飞往机场自毁 或 减速巡航）。
        # **只看 `finished`**：收尾的判据现在是"配置的轮次跑完了"，不再是"OCR
        # 读到存量归零"。后半个条件（cycle_active and bombs_depleted）随 OCR 退出
        # 决策链一起去掉 —— 它覆盖的是"场景线程读到归零 → stop() → 循环因
        # running=False 退出"那条路，而那条路已经不存在了。
        #
        # 再要求 `running and cycle_active`：`finished` 也可能是在**离开战斗之后**
        # 才置上的 —— 阶段 C 走完时 running 已经变 False，`has_next` 因此为假，
        # 于是落到"最后一轮结束"那条 else 上。原来"手动停止绝不触发自毁"的说法
        # 只对引导阶段成立（那条路在阶段 C 之前就 break 了），对投弹视角之后的
        # 路径不成立。加上这个门，机库里/战斗结束后不会再拉起 `_navigate_to_airfield`。
        if finished and self.running and self.cycle_active:
            self._finish_bomb_cycles()

    def _finish_bomb_cycles(self):
        """循环轰炸收尾：飞往敌方机场（自毁）或原地减速巡航。

        原来这段只挂在"最后一轮正常结束"这一条路径上，炸弹提前用尽会直接
        break 出循环、离开战斗会被 running=False 掐掉循环 —— 两种情况下都
        不会飞机场。现在统一从 _auto_bomb_sequence 收口到这里。
        """
        self.cycle_active = False
        self.guide_mode = False
        self.aim_lock = False
        self._bomb_control_enabled = False
        self.aggression = 0.0

        if self.gpad:
            if FLY_TO_AIRFIELD and WT8111_OK:
                self.gpad.left_joystick(0, 0.7)
                self.gpad.right_joystick(0, 0)
            else:
                self.gpad.left_joystick(0, -0.5)
                self.gpad.right_joystick(0, 0)
            self.gpad.update()

        if FLY_TO_AIRFIELD and WT8111_OK:
            self.log("轰炸轮次结束，开始飞往敌方机场（自毁）...")
            threading.Thread(target=self._navigate_to_airfield, daemon=True).start()
        else:
            self.log("轰炸轮次结束，减速巡航")

        # 停掉识别/控制循环，避免和 _navigate_to_airfield 抢摇杆输出
        self.running = False
        self.bombs_depleted = True

    # ==================== 导航到机场 ====================

    @staticmethod
    def _num(v):
        """8111 的数值字段 → float（可能是 int/float，也可能是逗号当小数点的字符串）"""
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.replace(",", "."))
            except ValueError:
                return None
        return None

    def _navigate_to_airfield(self):
        """使用 8111 API 飞往敌方主基地机场（低空掠过，让防空炮打）

        这是一个**下滑道耦合器**，不是"按距离推杆"的开环定时器：进场时锁定一条从
        当前位置连到"机场上方 NAV_TARGET_ALT"的直线，整段导航咬住它。计划线锁定的
        意义在于它是**固定的** —— 飞机偏出去就有一个持续存在的高度差把它拉回来，
        而"每步重算到机场的视线角"等于永远在追"我现在在哪"那条线，只有速率项没有
        位置项（原实现就是这样，所以航迹角一路发散到期望值的两倍）。

        三个通道全部反馈**实测物理量**：
          纵向  实测下降率 vs 计划下降率，再加一项偏离计划线的高度差
          横向  实测航向误差 + 实测转弯率构成的速率式 PD（增益单位 1/秒，与采样率无关）
          油门  表速环（保能量）叠加一个按下降率误差的配平
        且**抬头权限大于压头权限**：原实现只产出 -pitch / 0 / -0.6 三种值，没有任何
        抬头分支，一旦俯冲过陡就再也拉不起来（logs/20260928_152045.log 里 14° 的
        期望飞成了 36° 的实际）。

        日志里那句"俯冲角"打印的其实是**到机场的期望视线角**，从来不是飞机实际的
        下降角 —— 那才是"俯冲过头"的观感来源。所以现在的日志把"航迹/计划"两个角
        分开打，并且带上 dt（采样间隔）供调参。

        不加积分项：采样周期和被控对象都不确定，I 项必然振荡。别加回来。
        """
        self._navigating = True
        self.throttle = 0.8
        # 保持油门，不重置右摇杆（保留上一步的低头姿态）
        if self.gpad:
            self.gpad.left_joystick(0, 0.8)
            self.gpad.update()
        try:
            airfields = get_enemy_airfields()
            if not airfields:
                self.log("8111 未找到敌方机场")
                return

            player = get_player()
            if not player:
                self.log("8111 未获取到玩家位置")
                return

            stale = 0
            map_size = get_map_size()
            if not map_size or map_size <= 0:
                self.log("8111 未获取到地图尺寸，停止导航")
                return
            arrival_m = max(200.0, AIRFIELD_ARRIVAL_RATIO * map_size)

            # 选跑道最长且最远的敌方机场 = 有防空炮的主基地
            # 跑道长度 = (sx,sy) 到 (ex,ey) 的归一化距离 × 地图尺寸
            # 综合评分 = 跑道长度 × 距离（优先跑道长的，同等长度选最远的）
            def _runway_len(a):
                dx = a.get("ex", a["nx"]) - a["nx"]
                dy = a.get("ey", a["ny"]) - a["ny"]
                return dx * dx + dy * dy
            px, py = player["x"], player["y"]
            target = max(airfields, key=lambda a: _runway_len(a) * (
                (a["nx"] - px) ** 2 + (a["ny"] - py) ** 2
            ))
            rw_len = math.sqrt(_runway_len(target)) * map_size
            self.log(f"目标机场: ({target['nx']:.4f}, {target['ny']:.4f}) 跑道 {rw_len:.0f}m")

            # ========== 进场锁定计划下滑线（只算这一次） ==========
            _, dist0_norm = get_target_bearing(px, py, target["nx"], target["ny"])
            dist0 = dist0_norm * map_size
            fs0 = get_flight_state()
            alt0 = self._num(fs0.get("alt")) if fs0 else None
            if alt0 is not None:
                # 上下限 3°~35°：3° 保证总在下降；35° 是防止"又高又近"的进场几何
                # 算出 70° 的自杀下滑线（此时宁可在计划线上方一路压着 PUSH 上限下）
                slope0 = math.atan2(max(0.0, alt0 - NAV_TARGET_ALT), max(dist0 - arrival_m, 100.0))
                slope0 = max(math.radians(3.0), min(math.radians(35.0), slope0))
                self.log(
                    f"下滑线 {math.degrees(slope0):.1f}° "
                    f"({dist0 / 1000:.1f}km/{alt0:.0f}m → {arrival_m / 1000:.1f}km/{NAV_TARGET_ALT:.0f}m) "
                    f"地图 {map_size:.0f}m"
                )
            else:
                slope0 = math.radians(12.0)
                self.log("8111 未取到高度，下滑线退化为固定 12°")
            tan0 = math.tan(slope0)

            # 采样历史：求变化率时拿它与"约 NAV_DIFF_WINDOW 秒前"的样本做差
            hist = collections.deque(maxlen=256)
            law = NavLaw()          # 控制律实例（内部状态：转弯率低通、上一步俯仰）
            rx = 0.0
            ry = 0.0
            throttle = NAV_THROTTLE_NOM
            alt_err = 0.0
            vs_ref = 0.0
            gamma = 0.0
            vs = None
            gs = 0.0
            dt_ref = 0.0
            last_step = None
            last_log = 0.0
            last_arrive_log = 0.0

            while self._navigating and stale < 60:
                if self.game_state not in ("战斗中", "未知"):
                    self.log("离开战斗，停止导航")
                    break
                # 主开关关掉就停手。Gamepad 那一层本来就不输出，但这个循环不该继续
                # 按原样发指令、发请求 —— F12 的语义是"别再动我的飞机"。
                if not self.master_on or not self.gpad or not self.gpad.enabled:
                    self.log("主开关关闭，停止导航")
                    break

                now = time.time()
                dt_step = (now - last_step) if last_step is not None else 0.0
                last_step = now

                player = get_player()
                if not player:
                    stale += 1
                    time.sleep(1)
                    continue
                stale = 0

                bearing, dist_norm = get_target_bearing(
                    player["x"], player["y"], target["nx"], target["ny"]
                )
                error = (bearing - player["heading"] + 540) % 360 - 180
                dist_m = dist_norm * map_size

                fs = get_flight_state()
                altitude = self._num(fs.get("alt")) if fs else None
                ias = self._num(fs.get("ias")) if fs else None

                hist.append((now, player["x"], player["y"], player["heading"], dist_m, altitude))

                # 和"约 NAV_DIFF_WINDOW 秒前"的样本做差，而不是和上一个样本做差：
                # /state 的高度是 1 米量化的，6Hz 下相邻两步的高度差只有几米，噪声
                # 能到 20%；拉开到 1 秒只剩 3%。采样率越高，这一点越重要。
                ref = None
                for s in hist:
                    if now - s[0] >= NAV_DIFF_WINDOW:
                        ref = s
                    else:
                        break
                if ref is None:
                    # 头一秒还没有够老的样本：保持上一步，不瞎发指令
                    if self.gpad:
                        self.gpad.left_joystick(0, throttle)
                        self.gpad.right_joystick(rx, ry)
                        self.gpad.update()
                    time.sleep(0.1)
                    continue
                dt_ref = now - ref[0]
                if not (0.2 <= dt_ref <= 10.0):
                    # 时间戳异常（时钟跳变/线程被挂起）：这一步不发新指令
                    time.sleep(0.1)
                    continue

                # ========== 实测物理量 ==========
                gs = math.hypot(ref[1] - player["x"], ref[2] - player["y"]) * map_size / dt_ref
                omega_raw = ((player["heading"] - ref[3] + 540) % 360 - 180) / dt_ref
                # 实测下降率（正 = 下沉）。这里只负责"采样本 + 求变化率"，采样窗长和
                # 转弯率低通、限速这些**控制器内部状态**都在 NavLaw 里。
                if altitude is not None and ref[5] is not None:
                    vs = (ref[5] - altitude) / dt_ref
                    gamma = math.degrees(math.atan2(vs, max(gs, 1.0)))

                # ---- 控制律（common/nav_law.py：纯数值，可离线闭环仿真） ----
                meas = dict(dist_m=dist_m, arrival_m=arrival_m, altitude=altitude,
                            ias=ias, gs=gs, error=error, omega=omega_raw, vs=vs, tan0=tan0)
                out = law.step(_nav_tunes(), meas, dt_ref, dt_step)
                rx, ry, throttle = out["rx"], out["ry"], out["throttle"]
                alt_err, vs_ref = out["alt_err"], out["vs_ref"]
                if out["arrived"] and now - last_arrive_log >= 5.0:
                    last_arrive_log = now
                    self.log(f"已抵达机场上空 {altitude or 0:.0f}m，平飞掠过")

                # ============ 每秒打印一次（下一局靠它调参） ============
                if now - last_log >= 1.0:
                    last_log = now
                    side = "左" if error < 0 else "右"
                    self.log(
                        f"机场 {dist_m / 1000:.1f}km ({side}偏 {abs(error):.0f}°) 高 {altitude or 0:.0f}m | "
                        f"航迹 {gamma:+.0f}° 计划 {math.degrees(slope0):+.0f}° 高差 {alt_err:+.0f}m "
                        f"vs {vs or 0:+.0f}/{vs_ref:+.0f} | 俯仰 {ry:+.2f} 油门 {throttle:.2f} dt {dt_ref:.2f}s"
                    )

                self.throttle = throttle
                if self.gpad:
                    # 副翼默认不跟随（NAV_AILERON_GAIN=0）：左右摇杆的 X 各有独立的
                    # 符号参数，方向反了会和鼠标瞄准的教官指令打架，也会让
                    # nav_turn_rate_max 的标定失效。确认方向对了再开。
                    self.gpad.left_joystick((rx * NAV_AILERON_GAIN) if NAV_AILERON_GAIN else 0.0, throttle)
                    self.gpad.right_joystick(rx, ry)
                    self.gpad.update()

                time.sleep(0.1)

            # 到达后减速
            self.log("飞往机场完成，减速巡航")
            if self.gpad:
                self.gpad.left_joystick(0, -0.3)
                self.gpad.right_joystick(0, 0)
                self.gpad.update()

        finally:
            self._navigating = False
