"""War Thunder 自动轰炸主逻辑

工作流程:
  1. state_monitor_loop (每10s) → OCR 场景识别 → 自动启停
  2. recog_loop (≤30fps) → YOLO 战区检测
  3. control_loop (≤33Hz) → 摇杆转向对准战区
  4. auto_bomb_sequence → 引导 → 投弹视角 → 对准 → 投弹 → 减速
"""

import re
import time
import threading
import os

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
)
from common.window import find_windows, get_rect, capture
from common.utils import map_val
from common.gamepad import Gamepad, VGAME_OK
from detect.yolo import YOLO
from detect.scene import detect_state, get_scene_button, get_keyword_actions, SceneDetector

try:
    from common.wt8111 import get_player, get_enemy_airfields, get_friendly_airfields, get_target_bearing, get_player_altitude, get_target_bombing_zone, get_map_size, get_bombing_zones, get_teammates, is_teammate_heading_to_zone
    WT8111_OK = True
except ImportError:
    WT8111_OK = False


class App:
    def __init__(self):
        self.yolo = YOLO()
        self.rect = None
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
        self._lobby_since = None   # 进入大厅的时间，用于超时自动加入战斗

        # ---- 辅助锁定模式 ----
        self.lock_side = LOCK_SIDE  # "leftmost" / "rightmost" / "auto"
        self.locked_target = None   # {nx, ny, wx, wy} from 8111
        self.bomb_cycles = BOMB_CYCLES  # 循环次数，0=无限
        self.cycle_count = 0        # 当前已完成轮次
        self.turning_around = False # 是否在掉头阶段
        self.cycle_active = False   # 循环轰炸模式进行中

        # ---- 场景检测器 ----
        self.scene_detector = SceneDetector(log=self.log, stabilize_frames=2)
        self._unknown_streak = 0    # 连续"未知"次数，用于触发全帧OCR

        os.makedirs("logs", exist_ok=True)
        self.log_f = open(f"logs/{time.strftime('%Y%m%d_%H%M%S')}.log", "a", encoding="utf-8")

        self.bomb_count = BOMB_DEFAULT

        self._load_model()
        self._find_window()
        self._init_gamepad()
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
            # 去掉底部 40px（HUD 区域），减少误检
            if frame.shape[0] > 40:
                frame = frame[:-40, :, :]
            dets, _ = self.yolo.run(frame)
            with self.lock:
                self.dets = dets
                self.fshape = (frame.shape[1], frame.shape[0])

    # ==================== 场景监控 (每10s) ====================

    def _state_monitor_loop(self):
        """Scene monitoring loop using SceneDetector"""
        while True:
            if not self.rect:
                time.sleep(1)
                continue

            frame = capture(self.rect)
            if frame is None:
                time.sleep(0.5)
                continue

            force_ocr = self._unknown_streak >= 5
            new_scene, texts = self.scene_detector.scan(frame, force_full_ocr=force_ocr)

            if new_scene != self.game_state:
                self.game_state = new_scene
                self._unknown_streak = 0 if new_scene != "\u672a\u77e5" else self._unknown_streak
                self.log(f"\u573a\u666f: {new_scene}")

            if new_scene == "\u672a\u77e5":
                self._unknown_streak += 1
                # 遇到未识别的场景 → 尝试按 X 关闭弹窗
                # 战斗中只按 X（确认/加入），不按 B/A 以免退出战斗
                if self.gpad:
                    self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X, 0.15)
                    if not (self.guide_mode or self.aim_lock or self.cycle_active):
                        time.sleep(0.3)
                        self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.15)
                        time.sleep(0.3)
                        self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_A, 0.15)
            else:
                self._unknown_streak = 0

            # Scene dispatch
            if new_scene == "\u6218\u6597\u4e2d":
                self._handle_combat(texts)
            elif new_scene == "\u5927\u5385":
                self._handle_lobby(texts)
            elif new_scene == "\u9009\u62e9\u8f7d\u5177":
                self._handle_select_vehicle(texts)
            elif new_scene in ("\u6218\u679c\u6c47\u603b", "\u9000\u51fa\u4e2d"):
                self._handle_post_battle(texts)
            elif new_scene == "\u52a0\u8f7d\u4e2d":
                self._handle_loading()
            elif new_scene == "\u65e0\u91cd\u751f\u57fa\u5730":
                self._handle_no_spawn()
            elif new_scene == "\u89c2\u6218\u6a21\u5f0f":
                self._handle_spectate()
            elif new_scene == "\u673a\u573a\u8865\u7ed9":
                self._handle_airfield_supply(texts)

            # Generic UI buttons (skip during bomb view / cycling)
            if self.gpad and not self.aim_lock and not self.cycle_active:
                btn_map = {
                    "A": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                    "B": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                    "X": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                }
                btn = get_scene_button(new_scene)
                pressed = False
                # 大厅弹窗有"取消"时别按 X，否则会取消操作
                if btn == "X" and new_scene == "大厅" and any("取消" in t for t in texts):
                    pass  # 跳过 X，避免取消
                elif btn and btn in btn_map:
                    self.gpad.tap(btn_map[btn])
                    pressed = (btn == "X")
                # 关键词触发：如果场景键已经按了 X，就不再重复按 X
                for kw, kw_btn in get_keyword_actions(texts):
                    if kw_btn in btn_map and not (pressed and kw_btn == "X"):
                        self.gpad.tap(btn_map[kw_btn])

            sleep_time = 3 if new_scene == "\u672a\u77e5" else 5
            time.sleep(sleep_time)
    # ---- Scene handlers ----

    def _handle_combat(self, texts):
        """In combat: bomb bay + bomb count + auto start"""
        self._lobby_since = None  # 离开大厅
        if texts and self.gpad:
            has_bomb = any("\u70b8\u5f39" in t for t in texts)
            is_open = any("\u5f00\u542f" in t for t in texts)
            if has_bomb and not is_open:
                self.log("\u5f39\u8231\u672a\u5f00\u542f \u2192 \u81ea\u52a8\u6253\u5f00 (LT+RT)")
                self.gpad.pull_bay_lever()

        bomb_info = self.scene_detector.get_bomb_info()
        if bomb_info:
            remaining, total = bomb_info
            self.log(f"\u70b8\u5f39\u5b58\u91cf: {remaining}/{total}")
            if remaining == 0:
                self.log("\u70b8\u5f39\u5df2\u7528\u5b8c \u2192 \u505c\u6b62\u8ffd\u8e2a")
                self.bombs_depleted = True
                if self.running:
                    self.stop()
                return

        if not self.running:
            if self.bombs_depleted:
                if not self._navigating and not self.cycle_active:
                    self.log("\u672c\u5c40\u70b8\u5f39\u5df2\u6295\u5b8c\uff0c\u8df3\u8fc7\u81ea\u52a8\u542f\u52a8")
            else:
                self.log("\u8fdb\u5165\u6218\u6597 \u2192 \u81ea\u52a8\u542f\u52a8\u8ffd\u8e2a")
                self.start()

    def _handle_lobby(self, texts):
        """Lobby: reset bomb state + auto re-join after timeout"""
        if self.bombs_depleted:
            self.bombs_depleted = False
            self.log("\u70b8\u5f39\u72b6\u6001\u5df2\u91cd\u7f6e")
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()
            return

        # Track lobby entry time for auto-rejoin
        if self._lobby_since is None:
            self._lobby_since = time.time()

        elapsed = time.time() - self._lobby_since

        # 超过 30s 还没进战斗 → 尝试重新加入
        if self.gpad and elapsed > 30:
            if "\u53d6\u6d88" in texts:
                # 有取消按钮 → 先按 B 返回，再按 X 打开模式选择
                self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.3)
                time.sleep(0.5)
            # 按 X 打开模式选择 / 加入战斗
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
            if elapsed > 60:
                # 超过 60s: 按 B 返回主菜单再试
                self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.3)
                time.sleep(0.5)
                self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
                self._lobby_since = time.time()  # 重置计时，避免高频重复

    def _handle_select_vehicle(self, texts):
        """Vehicle select: auto join"""
        self._lobby_since = None  # 离开大厅
        if self.bombs_depleted:
            self.bombs_depleted = False
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()
        if self.gpad and not any("\u53d6\u6d88" in t for t in texts):
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)

    def _handle_post_battle(self, texts):
        """Post-battle: skip results"""
        if self.bombs_depleted:
            self.bombs_depleted = False
        if self.running:
            self.log("\u79bb\u5f00\u6218\u6597 \u2192 \u81ea\u52a8\u505c\u6b62")
            self.stop()

    def _handle_loading(self):
        """Loading: no-op"""
        pass

    def _handle_no_spawn(self):
        """No spawn: stop tracking + press X to continue"""
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


    def _target_info_loop(self):
        """通过 8111 API 每秒识别并打印正在飞向的战区及距离"""
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
                        side = "左" if info["bearing_diff"] < 0 else "右"
                        self.log(
                            f"目标战区 {info['dist_m']/1000:.1f}km  "
                            f"({side}偏 {abs(info['bearing_diff']):.0f}°)  "
                            f"夹角 {info['angle']:.0f}°"
                        )
                    else:
                        self.log("8111: 无有效战区（无数据或角度过大）")
                except Exception:
                    pass
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
        ry = self._guide_pitch()  # 引导阶段持续俯冲，非引导阶段 ry=0
        out = {"mode": "neutral", "lx": 0.0, "ly": self.throttle, "rx": 0.0, "ry": ry}
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

    def _guide_pitch(self):
        """引导阶段俯冲量：根据当前高度计算右摇杆 Y 值

        高度越高俯冲越猛，保障开局能快速下降到战区高度。
        - 高度 ≤ 500m: 轻推 -0.08
        - 高度 1000m: 约 -0.14
        - 高度 3000m+: 约 -0.20
        """
        if not self.guide_mode:
            return 0.0
        try:
            alt = get_player_altitude()
            if alt is not None and alt > 100:
                # 高度 100~3000m 线性映射到 -0.08 ~ -0.20
                pitch = max(-0.20, min(-0.08, -0.08 - (alt - 100) / 29000 * 0.12))
                return pitch
        except Exception:
            pass
        # 读不到高度就给个轻俯冲
        return -0.12

    def _control_loop(self):
        while self.running:
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
                                "ry": self._guide_pitch(),
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
                                "ry": self._guide_pitch(),
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

    def _drop_bombs(self, count, keep_running=False):
        """快速点按 LB+X 多次，每颗炸弹触发一次

        keep_running=True: 不设置 running=False，用于循环轰炸模式
        """
        if not self.gpad:
            self.log("虚拟手柄不可用，无法投弹")
            return
        if not keep_running:
            self.running = False
            self.bombs_depleted = True
        # 确保进入投弹视角
        if not self.aim_lock:
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y, 0.15)
            time.sleep(0.5)
        # 退出武器选择模式（还未进投弹视角时），确保 LB+X 是批量投弹
        if not self.aim_lock:
            self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_B, 0.1)
            time.sleep(0.2)
        for i in range(count):
            self.gpad.press_lb_x()
            time.sleep(0.15)
            self.gpad.release_lb_x()
            time.sleep(0.12)
        # 循环轰炸模式：LB+X 全部完成后才设 bombs_depleted
        if keep_running:
            self.bombs_depleted = True

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
                        # 基本正对但还有点距离 → 继续接近
                        if abs(error) < 15 and dist_m < view_distance * 1.5:
                            close_count += 1
                            if close_count >= 5:  # 连续 5 次都在范围内
                                self.log(f"正对且接近锁定目标（{dist_m:.0f}m, 偏{error:.0f}°）→ 进入投弹视角")
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

        while self.cycle_active and self.running:
            # ---- 检查炸弹存量 ----
            if self.bombs_depleted:
                self.log(f"炸弹已用完，停止循环轰炸")
                self.cycle_active = False
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

                while self.running and self.cycle_active:
                    self.aggression = 0.7

                    # 8111 距离检测（即使无锁定目标也可用）
                    if WT8111_OK:
                        try:
                            info = get_target_bombing_zone()
                            if info and info["dist_m"] <= BOMB_VIEW_DISTANCE:
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

                    # 超时保护：引导超过 60 秒仍未满足条件则强制进入投弹视角
                    if time.time() - guide_start > 60:
                        self.log("引导超时 60s，强制进入投弹视角")
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
            self.aim_lock = True
            self._reset_bomb_aim_controller()
            self._bomb_control_enabled = True
            self.log("进入投弹视角")
            if self.gpad:
                self.gpad.tap(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)
            time.sleep(0.5)
            self.throttle = 0.55

            self.log("投弹视角：持续跟踪并修正偏差，稳定对准后投弹")
            dropped = False
            target_stolen = False
            stable_hits = 0
            last_target_log = 0.0
            stale_since = None
            prev_dy = None

            while self.running:
                now = time.time()
                state = self._last_aim_state
                out = self._last_control_state or {}
                if not state or not state.get("bomb_view") or now - state.get("time", 0) > 0.25:
                    if stale_since is None:
                        stale_since = now
                    if now - stale_since > 1.5:
                        self._reset_bomb_aim_controller()
                        self._bomb_control_enabled = True
                    if now - stale_since > STOLEN_TIMEOUT:
                        self.log(f"目标已丢失 {STOLEN_TIMEOUT}s（战区可能被抢）→ 切换目标")
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

            # ---- 战区被抢：切换目标重新引导（不计入已完成轮次） ----
            if target_stolen:
                self.aim_lock = False
                self.guide_mode = False
                self.aggression = 0.0
                self.throttle = 0.5

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

            # 通过 OCR 炸弹存量信息判断是否还有炸弹
            bomb_info = self.scene_detector.get_bomb_info()
            if bomb_info:
                remaining, _ = bomb_info
                if remaining == 0:
                    self.bombs_depleted = True
                    self.log("炸弹已用尽（OCR 确认）")
                else:
                    self.bombs_depleted = False
                    self.log(f"炸弹剩余: {remaining} → 继续下一轮轰炸")

            # 判断是否还有下一轮
            has_next = (
                self.cycle_active
                and self.running
                and self.cycle_count < total_cycles
                and not self.bombs_depleted
            )

            if has_next:
                # 掉头 → 下一轮
                self.log(f"准备第 {self.cycle_count + 1} 轮轰炸...")
                self.throttle = 0.5

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
                # 最后一轮结束
                self.cycle_active = False
                self.log("所有轰炸轮次完成")

                # 恢复 running 状态以供后续正常清理
                if not self.running:
                    self.running = True

                if self.gpad:
                    if FLY_TO_AIRFIELD and WT8111_OK:
                        self.gpad.left_joystick(0, 0.7)
                        self.gpad.right_joystick(0, 0)
                    else:
                        self.gpad.left_joystick(0, -0.5)
                        self.gpad.right_joystick(0, 0)
                    self.gpad.update()

                if FLY_TO_AIRFIELD and WT8111_OK:
                    self.log("所有轮次完成，开始飞往敌方机场...")
                    threading.Thread(target=self._navigate_to_airfield, daemon=True).start()
                else:
                    self.log("所有轮次完成，减速巡航")

                # 清理状态
                self.running = False
                self.bombs_depleted = True

    # ==================== 导航到机场 ====================

    def _heading_to_stick(self, error_deg):
        """航向偏差 (度) → 右摇杆 X 值

        3° 死区，30° 达满杆，使用 MIN_V 和 self.sense 控制曲线
        """
        dz = 3.0
        if abs(error_deg) <= dz:
            return 0.0
        t = min(abs(error_deg) / 30.0, 1.0)
        return math.copysign(MIN_V + t * (self.sense - MIN_V), error_deg)

    def _navigate_to_airfield(self):
        """使用 8111 API 飞往敌方主基地机场（低空自毁，让防空炮打）

        每秒计算距离/俯冲角并打印，接近时按俯冲角比例推杆低头。
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
            last_log = 0.0

            while self._navigating and stale < 60:
                if self.game_state not in ("战斗中", "未知"):
                    self.log("离开战斗，停止导航")
                    break

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

                # ========== 距离 & 俯冲角计算 ==========
                dist_m = dist_norm * map_size
                altitude = get_player_altitude() or 0
                # 当前位置到机场连线的俯冲角（0°=水平, 90°=正下方）
                dive_angle = math.degrees(math.atan2(altitude, dist_m)) if dist_m > 1 else 90

                rx = self._heading_to_stick(error)

                # ========== 提前俯冲决策（计算所需下滑角） ==========
                target_alt = 50  # 目标：到机场上方时约 50m 低空
                alt_to_lose = max(0, altitude - target_alt)
                # 当前位置到机场所需的下滑角（角度越大越陡）
                required_angle = math.degrees(math.atan2(alt_to_lose, max(dist_m, 1)))

                if required_angle > 3:
                    # 3° 以上开始介入，平滑递增，60° 满杆
                    pitch = min(required_angle / 60, 1.0)
                    ry = -pitch
                    throttle = 0.80
                elif dist_norm < AIRFIELD_ARRIVAL_RATIO * 2:
                    # 已到机场附近但高度还高，强行推头
                    ry = -0.6
                    throttle = 0.70
                else:
                    ry = 0.0  # 巡航平飞
                    throttle = 0.80

                # 接近但还没开始俯冲时减速防飞过
                if ry == 0.0 and dist_norm < AIRFIELD_ARRIVAL_RATIO * 3:
                    throttle = 0.35

                # 10km → 5km 平滑降油门，到 5km 时归零滑翔接近
                if dist_m < 10000:
                    factor = max(0.0, (dist_m - 5000) / 5000)
                    throttle *= factor

                # 每秒打印一次
                now = time.time()
                if now - last_log >= 1.0:
                    last_log = now
                    side = "左" if error < 0 else "右"
                    self.log(
                        f"机场 {dist_m/1000:.1f}km  "
                        f"({side}偏 {abs(error):.0f}°)  "
                        f"高 {altitude:.0f}m  "
                        f"俯冲角 {dive_angle:.0f}°  "
                        f"油门 {throttle:.2f}"
                    )

                if self.gpad:
                    self.gpad.left_joystick(0, throttle)
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
