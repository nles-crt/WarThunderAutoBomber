import os, json

MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "best.pt")

_DEFAULTS = {
    "sense": 0.6,
    "bomb_count": 1,
    "min_v": 0.26,
    "auto_bomb": True,
    "pixel_deviation": 7,
    "guide_box_ratio": 0.02,
    "bomb_view_sense": 0.6,
    "model_conf": 0.5,
    "model_iou": 0.45,
    "bomb_view_distance": 6500,
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
    "bomb_cycles_comment": "循环轰炸次数: 1=单次, 2=炸两次, 0=一直循环直到炸弹用完",
    "turn_speed": 0.20,
    "turn_speed_comment": "掉头转向速度 (0~1)，越小转弯越平滑",
    "stolen_timeout": 5,
    "stolen_timeout_comment": "战区被队友抢后等待几秒切换目标（投弹视角中目标丢失超时）",
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
STOLEN_TIMEOUT = _cfg.get("stolen_timeout", 5)

