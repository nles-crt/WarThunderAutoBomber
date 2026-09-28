import os, json

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "best.pt")
# config.json 的绝对路径。GUI 需要自己读写原始 JSON（common/config.py 只在
# import 时读一次、且不暴露写回接口），所以把路径导出出去。
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))

_DEFAULTS = {
    "sense": 0.6,
    "bomb_count": 1,
    "min_v": 0.26,
    "auto_bomb": True,
    "pixel_deviation": 7,
    "pixel_deviation_y": 9,
    "guide_box_ratio": 0.02,
    "bomb_view_sense": 0.6,
    "model_conf": 0.5,
    "model_iou": 0.45,
    "bomb_view_distance": 6500,
    "fly_to_airfield": False,
    "nav_turn_rate_max": 12.0,
    "nav_yaw_kp": 0.25,
    "nav_yaw_kd": 0.35,
    "nav_yaw_dz": 3.0,
    "nav_omega_tau": 1.0,
    "nav_yaw_max": 0.55,
    "nav_aileron_gain": 0.0,
    "nav_pitch_kv": 0.003,
    "nav_pitch_ka": 0.0020,
    "nav_pitch_push_max": 0.30,
    "nav_pitch_pull_max": 0.80,
    "nav_pitch_rate": 0.40,
    "nav_throttle_nom": 0.45,
    "nav_throttle_k": 0.002,
    "nav_throttle_kv": 0.008,
    "nav_throttle_min": 0.25,
    "nav_ias_ref": 500,
    "nav_diff_window": 1.0,
    "nav_target_alt": 50,
    "airfield_arrival_ratio": 0.04,
    "aim_offset_x": 0,
    "aim_offset_y": 0,
    "bomb_aim_offset_x": 0,
    "bomb_aim_offset_y": 0,
    "bomb_view_left_x_sign": 1,
    "bomb_view_right_x_sign": 1,
    "bomb_view_right_y_sign": 1,
    "track_right_x_sign": 1,
    "track_right_y_sign": 1,
    "lock_side": "leftmost",
    "lock_comment": "锁定目标方向: leftmost(最左)/rightmost(最右)/auto(自动)",
    "bomb_cycles": 1,
    "bomb_cycles_comment": "循环轰炸次数: 1=单次, 2=炸两次, 0=不自动收尾（需手动 F12 停止）",
    "turn_speed": 0.20,
    "turn_speed_comment": "掉头转向速度 (0~1)，越小转弯越平滑",
    "stolen_timeout": 20,
    "stolen_timeout_comment": "投弹视角里目标连续看不到多少秒才放弃换目标（期间留在镜里持续重捕）",
    # ---- HUD 告警阈值（只变色，不干预飞行）----
    "dive_angle_warn": 30.0,
    "dive_angle_warn_comment": "HUD 告警：实际俯冲角超过此角度时该行变红。仅提示，不影响飞行。",
    "max_ias_warn": 700,
    "max_ias_warn_comment": "HUD 告警：表速超过此值时变红（老式战机约 700-750 解体）。仅提示，不影响飞行。",
    # ---- HUD 外观 ----
    "hud_enabled": True,
    "hud_enabled_comment": "是否显示游戏窗口上的半透明 HUD。注意：HUD 盖住的这块画面会从机器人自己的视野里涂黑（否则 OCR 会读到 HUD 上的「表速/高度/km/h」而把机库误判成战斗中），所以 HUD 越大、机器人能看到的画面越小。720p 下默认大小约占 18%%，嫌挡可用 show_log_lines=0、hud_scale 调小或换 hud_corner。",
    "hud_corner": "topright",
    "hud_corner_comment": "HUD 停靠角: topright/topleft/bottomright/bottomleft。",
    "hud_opacity": 0.65,
    "hud_opacity_comment": "HUD 底板不透明度 (0.15~1.0)。",
    "hud_scale": 1.0,
    "hud_scale_comment": "HUD 字号缩放 (0.6~2.0)。",
    "hud_offset_x": 0,
    "hud_offset_x_comment": "HUD 水平微调(像素)。若显示器缩放不是 100%，位置可能偏，用这个挪。",
    "hud_offset_y": 0,
    "hud_offset_y_comment": "HUD 垂直微调(像素)。",
    "follow_window": True,
    "follow_window_comment": "是否持续跟踪游戏窗口位置/大小（窗口移动后识别不再失效）。关掉则沿用启动时的一次性快照。",
    # ---- 场景识别 ----
    "scene_scan_interval": 3.0,
    "scene_log": True,
    "scene_log_texts": True,
    "battle_log": True,
    "unknown_log": True,
    "match_wait_warn": 300,
    # ---- 其它 ----
    "hotkey_master": "f12",
    "hotkey_master_comment": "总开关热键名。支持 f1~f12 及 pynput 键名；改完需重启程序生效。",
    "show_log_lines": 6,
    "show_log_lines_comment": "HUD 上显示最近几行日志（0=不显示）。日志行会让 HUD 变高，见 _hud_enabled 的说明。",
}

_cfg = dict(_DEFAULTS)
_cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
if os.path.exists(_cfg_path):
    with open(_cfg_path, "r", encoding="utf-8") as f:
        _cfg.update(json.load(f))

SENSE_DEFAULT   = _cfg["sense"]
BOMB_DEFAULT    = _cfg["bomb_count"]
MIN_V           = _cfg["min_v"]
AUTO_BOMB       = _cfg["auto_bomb"]
PIXEL_DEV       = _cfg["pixel_deviation"]
PIXEL_DEV_Y     = _cfg.get("pixel_deviation_y", 9)
GUIDE_BOX_RATIO = _cfg["guide_box_ratio"]
BOMB_VIEW_SENSE = _cfg["bomb_view_sense"]
CONF_DEFAULT           = _cfg["model_conf"]
IOU_DEFAULT            = _cfg["model_iou"]
FLY_TO_AIRFIELD        = _cfg.get("fly_to_airfield", False)
# ---- 飞往敌方机场（投弹后的自毁航段，见 app._navigate_to_airfield）----
# 这个航段原来实测跑在 0.25Hz 上（日志行间隔恒定 4.0 秒），根因不是游戏慢，是
# BASE_URL 写成了 localhost：Windows 先试 IPv6 ::1，而游戏只听 IPv4，::1 既不回
# RST 也不回 SYN-ACK，于是每个请求都要耗满一次超时（实测 localhost 2034ms /
# 127.0.0.1 33ms）。改成 127.0.0.1 后循环约 6Hz。下面这些量全部按**物理单位**
# 定义（度/秒、杆量每 m/s），所以采样率再变也只是每步步长变，含义不变。
# 纵向用「下降率」而不是「俯仰角」做反馈：/state 里根本没有俯仰角字段（见
# CLAUDE.md），实测下降率是唯一能测到的纵向状态量。
NAV_TURN_RATE_MAX = _cfg.get("nav_turn_rate_max", 12.0)   # 满杆转弯率 (度/秒)，实测反推
NAV_YAW_KP        = _cfg.get("nav_yaw_kp", 0.25)          # 横向 P 增益 (1/秒)
NAV_YAW_KD        = _cfg.get("nav_yaw_kd", 0.35)          # 横向 D 增益（乘实测转弯率）
NAV_YAW_DZ        = _cfg.get("nav_yaw_dz", 3.0)           # 航向死区 (度)
NAV_OMEGA_TAU     = _cfg.get("nav_omega_tau", 1.0)        # 实测转弯率低通时间常数 (秒)
NAV_YAW_MAX       = _cfg.get("nav_yaw_max", 0.55)         # 横向杆量上限（对齐 track 模式）
NAV_AILERON_GAIN  = _cfg.get("nav_aileron_gain", 0.0)     # 左摇杆副翼同步增益（默认关）
NAV_PITCH_KV      = _cfg.get("nav_pitch_kv", 0.003)       # 杆量 / (m/s) 下降率误差（仿真标定）
NAV_PITCH_KA      = _cfg.get("nav_pitch_ka", 0.0020)      # 杆量 / m 高差
NAV_PITCH_PUSH    = _cfg.get("nav_pitch_push_max", 0.30)  # 压头杆量上限
NAV_PITCH_PULL    = _cfg.get("nav_pitch_pull_max", 0.80)  # 抬头杆量上限（必须 > PUSH）
NAV_PITCH_RATE    = _cfg.get("nav_pitch_rate", 0.40)      # 俯仰指令限速 (每秒)
NAV_THROTTLE_NOM  = _cfg.get("nav_throttle_nom", 0.45)    # 表速环基准油门
NAV_THROTTLE_K    = _cfg.get("nav_throttle_k", 0.002)     # 表速环增益（每 km/h 差）
NAV_THROTTLE_KV   = _cfg.get("nav_throttle_kv", 0.008)    # 油门对下降率误差的配平（每 m/s）
NAV_THROTTLE_MIN  = _cfg.get("nav_throttle_min", 0.25)    # 油门地板（别在高空空油门）
NAV_IAS_REF       = _cfg.get("nav_ias_ref", 500)          # 目标表速 (km/h)
NAV_DIFF_WINDOW   = _cfg.get("nav_diff_window", 1.0)      # 求变化率的采样窗长 (秒)
NAV_TARGET_ALT    = _cfg.get("nav_target_alt", 50)        # 机场上方的目标高度 (m)
AIRFIELD_ARRIVAL_RATIO = _cfg.get("airfield_arrival_ratio", 0.04)
BOMB_VIEW_DISTANCE     = _cfg.get("bomb_view_distance", 6500)
AIM_OFFSET_X           = _cfg.get("aim_offset_x", 0)
AIM_OFFSET_Y           = _cfg.get("aim_offset_y", 0)
BOMB_AIM_OFFSET_X      = _cfg.get("bomb_aim_offset_x", 0)
BOMB_AIM_OFFSET_Y      = _cfg.get("bomb_aim_offset_y", 0)
BOMB_VIEW_LEFT_X_SIGN  = _cfg.get("bomb_view_left_x_sign", 1)
BOMB_VIEW_RIGHT_X_SIGN = _cfg.get("bomb_view_right_x_sign", 1)
BOMB_VIEW_RIGHT_Y_SIGN = _cfg.get("bomb_view_right_y_sign", 1)
TRACK_RIGHT_X_SIGN     = _cfg.get("track_right_x_sign", 1)
TRACK_RIGHT_Y_SIGN     = _cfg.get("track_right_y_sign", 1)
LOCK_SIDE = _cfg.get("lock_side", "leftmost")
BOMB_CYCLES = _cfg.get("bomb_cycles", 1)
TURN_SPEED = _cfg.get("turn_speed", 0.20)
STOLEN_TIMEOUT = _cfg.get("stolen_timeout", 20)

# HUD 告警阈值（app.py 不用，GUI/HUD 读；放在这里是为了 GUI 能统一枚举）
# 注意这两个**只驱动 HUD 变色，不进任何控制量**。实际俯冲角由 app 按高度
# 变化率反推（8111 没有俯仰角字段），引导阶段的俯冲本身靠 TRACK_RIGHT_Y_SIGN
# 那条视觉伺服 + _tracking_stick 里的防正反馈衰减来管。
DIVE_ANGLE_WARN = _cfg.get("dive_angle_warn", 30.0)
MAX_IAS_WARN    = _cfg.get("max_ias_warn", 700)
# 总开关热键（pynput 键名，如 f12）
HOTKEY_MASTER = _cfg.get("hotkey_master", "f12")

# ---- 场景识别 ----
# 扫描目标周期（秒）。整屏 OCR 的耗时从这个周期里扣掉，所以它是"每轮多久"
# 而不是"每轮额外睡多久"。太快没有意义：OCR 本身就占掉大半。
SCENE_SCAN_INTERVAL = _cfg.get("scene_scan_interval", 3.0)
# 是否写 JSONL 场景记录（logs/scenes_*.jsonl），排查场景误判用。
SCENE_LOG        = _cfg.get("scene_log", True)
SCENE_LOG_TEXTS  = _cfg.get("scene_log_texts", True)
# 是否记录战果（logs/results_*.jsonl，一局结算画面一行）。**和 scene_log 不同，
# 这一项热生效**：句柄是懒开的、开关是每次写入时重读的（见 App._battle_log_write）。
BATTLE_LOG       = _cfg.get("battle_log", True)
# 是否记录"认不出来"的画面（logs/unknown_*.jsonl）。和 battle_log 一样热生效。
# 场景关键词表是实测出来的，这份文件就是它唯一的补词来源：认不出来的画面
# 按内容去重各记一行，下次照着样本加关键词即可。
UNKNOWN_LOG      = _cfg.get("unknown_log", True)
# 排队等匹配超过这么多秒打一条警告日志。只记录，不按键（按键会取消匹配）。
MATCH_WAIT_WARN  = _cfg.get("match_wait_warn", 300)

