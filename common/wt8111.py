"""War Thunder 8111 API 客户端 — 获取玩家位置与机场坐标"""

import math

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

BASE_URL = "http://localhost:8111"
_TIMEOUT = 2.0


def fetch_json(endpoint):
    """获取 8111 端点 JSON 数据，失败返回 None"""
    if not _REQUESTS_OK:
        return None
    try:
        resp = requests.get(f"{BASE_URL}{endpoint}", timeout=_TIMEOUT)
        resp.raise_for_status()
        if resp.text.strip():
            return resp.json()
    except Exception:
        pass
    return None


def _to_world(val, map_min, map_max):
    """归一化坐标 (0~1) → 世界坐标"""
    return map_min + (map_max - map_min) * val


def get_player():
    """提取玩家位置与航向

    返回 dict {x, y, heading, dx, dy} 或 None
    """
    map_obj = fetch_json("/map_obj.json")
    if not isinstance(map_obj, list):
        return None

    player = None
    for obj in map_obj:
        if obj.get("type") == "aircraft" and "Player" in obj.get("icon", ""):
            player = obj
            break
    if player is None:
        return None

    x, y = player.get("x", 0.5), player.get("y", 0.5)
    dx, dy = player.get("dx", 0), player.get("dy", 0)
    heading = math.degrees(math.atan2(dx, -dy)) % 360

    return {"x": x, "y": y, "heading": heading, "dx": dx, "dy": dy}


def get_enemy_airfields(include_carriers=False):
    """获取敌方机场列表

    敌方机场颜色 #fa0C00 (红色)
    include_carriers — 是否包含航母（默认过滤航母，只留陆基机场）
    返回 [{nx, ny, wx, wy, icon}, ...]
    """
    fields = _get_airfields_by_color(("#FA0C00", "#F00C00", "#FA0C0C"))
    if not include_carriers:
        carrier_kw = ["carrier", "aircraft_carrier", "cv", "cvn", "航母"]
        fields = [a for a in fields if not any(
            kw in (a.get("icon", "") or "").lower() for kw in carrier_kw
        )]
    return fields


def get_friendly_airfields():
    """获取友方机场列表（蓝色 #174DFF）"""
    return _get_airfields_by_color(("#174DFF", "#043FFF"))


def _get_airfields_by_color(colors):
    """按颜色筛选机场

    参数: colors — tuple of color strings (大写)
    返回 [{nx, ny, wx, wy}, ...]
    """
    map_obj = fetch_json("/map_obj.json")
    if not isinstance(map_obj, list):
        return []

    map_info = fetch_json("/map_info.json")
    mmax = map_info["map_max"] if map_info and map_info.get("valid") else [-65536, -65536]
    mmin = map_info["map_min"] if map_info and map_info.get("valid") else [65536, 65536]

    airfields = []
    for obj in map_obj:
        if obj.get("type") != "airfield":
            continue
        color = obj.get("color", "")
        if color.upper() not in colors:
            continue

        sx = obj.get("sx", 0.5)
        sy = obj.get("sy", 0.5)
        wx = _to_world(sx, mmin[0], mmax[0])
        wy = _to_world(sy, mmin[1], mmax[1])

        airfields.append({
            "nx": sx, "ny": sy,
            "wx": wx, "wy": wy,
            "ex": obj.get("ex", sx), "ey": obj.get("ey", sy),
            "icon": obj.get("icon", ""),
        })

    return airfields


def get_player_altitude():
    """获取玩家当前高度（米），失败返回 None"""
    state = fetch_json("/state")
    if state and state.get("valid"):
        return state.get("H, m")
    return None


def dump_state():
    """获取 /state 全部字段（调试用），失败返回 None"""
    return fetch_json("/state")


def dump_indicator():
    """获取 /indicator.json 全部字段（调试用），失败返回 None"""
    return fetch_json("/indicator.json")


def get_health():
    """获取机体整体血量 0.0~1.0，失败返回 None"""
    state = fetch_json("/state")
    if state and state.get("valid"):
        return state.get("health")
    return None


def get_damage():
    """获取各部件损伤程度

    返回 dict:
        health:      整体血量 0.0~1.0
        sections:    各部位损伤 {wing_left: 0.0~1.0, engine: 0.0~1.0, ...}
        hit_indicator: 被击中指示（可能含方向信息）
        is_attacking:  是否在攻击状态
    或 None
    """
    state = fetch_json("/state")
    if not state or not state.get("valid"):
        return None
    return {
        "health": state.get("health"),
        "sections": state.get("damage"),
        "hit_indicator": state.get("hit_indicator"),
        "is_attacking": state.get("is_attacking"),
    }


def is_health_critical(threshold=0.3):
    """判断机体是否严重受损（血量低于 threshold，默认 30%）"""
    health = get_health()
    if health is None:
        return False
    return health < threshold


def get_map_objects():
    """获取 map_obj 所有对象的原始数据"""
    return fetch_json("/map_obj.json")


def get_map_size():
    """获取地图尺寸（米），失败返回 130000"""
    info = fetch_json("/map_info.json")
    if info and info.get("valid"):
        try:
            mx = info.get("map_max", [0, 0])
            mn = info.get("map_min", [0, 0])
            s = max(abs(mx[0] - mn[0]), abs(mx[1] - mn[1]))
            if s > 0:
                return s
        except Exception:
            pass
    return 130000.0


def get_bombing_zones():
    """获取所有轰炸战区（bombing_point）

    bombing_point 使用 x, y 归一化坐标 (0~1)
    返回 [{x, y}, ...] 或空列表
    """
    map_obj = fetch_json("/map_obj.json")
    if not isinstance(map_obj, list):
        return []

    zones = []
    for obj in map_obj:
        groups = obj.get("groups") or []
        if "bombing_point" in groups:
            zones.append({
                "x": obj.get("x", 0.5),
                "y": obj.get("y", 0.5),
            })
    return zones


def get_target_bearing(px, py, tx, ty):
    """计算位置 (px,py) 到目标 (tx,ty) 的方位角和距离（归一化坐标）

    返回 (bearing_deg, distance)
    """
    dx = tx - px
    dy = ty - py
    distance = math.sqrt(dx * dx + dy * dy)
    bearing = math.degrees(math.atan2(dx, -dy)) % 360
    return bearing, distance


def get_target_bombing_zone():
    """找出玩家当前正在飞向的战区（轰炸点）

    遍历所有 bombing_point，计算航向与目标方向的夹角，
    返回最前方的战区及其距离/偏航信息。

    ★ 角度计算使用归一化坐标（与 get_player() 和 wt_reader.py 一致），
      世界坐标仅用于距离计算。地图 x/y 比例不均时非等比缩放方向向量
      会导致航向偏差，所以不能转世界坐标算角度。

    返回 dict:
        angle:         航向与目标方向夹角（度），越小越正对
        bearing_diff: 偏航角度（负=偏左, 正=偏右）
        dist_m:       直线距离（米）
        wx, wy:       世界坐标
        nx, ny:       归一化坐标 (0~1)
    或 None（无数据、无有效目标）
    """
    map_obj = fetch_json("/map_obj.json")
    if not isinstance(map_obj, list):
        return None

    map_info = fetch_json("/map_info.json")
    mmax = map_info["map_max"] if map_info and map_info.get("valid") else [-65536, -65536]
    mmin = map_info["map_min"] if map_info and map_info.get("valid") else [65536, 65536]

    # 找玩家飞机
    player = None
    for obj in map_obj:
        if obj.get("type") == "aircraft" and "Player" in obj.get("icon", ""):
            player = obj
            break
    if not player:
        return None

    px, py = player.get("x", 0.5), player.get("y", 0.5)
    dx, dy = player.get("dx", 0), player.get("dy", 0)
    mag = math.sqrt(dx * dx + dy * dy)
    if mag == 0:
        return None

    # ★ 航向用归一化坐标计算（和 get_player()、wt_reader.py 一致）
    heading = math.degrees(math.atan2(dx, -dy)) % 360

    # 世界坐标用于距离转换
    px_w = _to_world(px, mmin[0], mmax[0])
    py_w = _to_world(py, mmin[1], mmax[1])
    map_sx = max(1.0, mmax[0] - mmin[0])
    map_sy = max(1.0, mmax[1] - mmin[1])

    candidates = []
    for obj in map_obj:
        # 同时检查 type 和 groups 字段，兼容不同 API 版本
        obj_type = obj.get("type", "")
        obj_groups = obj.get("groups") or []
        is_bombing = (
            obj_type == "bombing_point"
            or "bombing_point" in obj_groups
        )
        if not is_bombing:
            continue
        tx = obj.get("x", 0.5)
        ty = obj.get("y", 0.5)

        # ★ 夹角/偏航用归一化坐标计算（和 wt_reader.py 一样）
        tdx = tx - px
        tdy = ty - py
        dist_n = math.sqrt(tdx * tdx + tdy * tdy)
        if dist_n < 0.0001:
            continue

        # 航向与目标方向夹角（归一化坐标下计算，避免非等比缩放扭曲角度）
        cos_a = (dx * tdx + dy * tdy) / (mag * dist_n)
        cos_a = max(-1, min(1, cos_a))
        angle = math.degrees(math.acos(cos_a))

        # 偏航（负=偏左, 正=偏右）
        target_bearing = math.degrees(math.atan2(tdx, -tdy)) % 360
        bearing_diff = (target_bearing - heading + 540) % 360 - 180

        # ★ 距离用世界坐标（真实米数）
        tx_w = _to_world(tx, mmin[0], mmax[0])
        ty_w = _to_world(ty, mmin[1], mmax[1])
        dist_w = math.sqrt((tx_w - px_w) ** 2 + (ty_w - py_w) ** 2)

        candidates.append({
            "angle": angle,
            "bearing_diff": bearing_diff,
            "dist_m": dist_w,
            "wx": tx_w,
            "wy": ty_w,
            "nx": tx,
            "ny": ty,
        })

    if not candidates:
        return None

    # 按夹角排序，选最正对前方的战区
    candidates.sort(key=lambda z: z["angle"])
    best = candidates[0]

    # 夹角 > 120° 说明没在朝任何战区飞
    if best["angle"] > 120:
        return None

    return best



def get_teammates():
    """获取所有友方飞机的位置与航向

    蓝天友方颜色 #174DFF / #043FFF，排除自己（icon 含 "Player"）
    返回 [{x, y, dx, dy, heading, icon}, ...]
    """
    map_obj = fetch_json("/map_obj.json")
    if not isinstance(map_obj, list):
        return []

    teammates = []
    for obj in map_obj:
        if obj.get("type") != "aircraft":
            continue
        icon = obj.get("icon", "")
        if "Player" in icon:
            continue  # 排除自己
        color = obj.get("color", "").upper()
        if color not in ("#174DFF", "#043FFF"):
            continue  # 非友方颜色
        x, y = obj.get("x", 0.5), obj.get("y", 0.5)
        dx, dy = obj.get("dx", 0), obj.get("dy", 0)
        heading = math.degrees(math.atan2(dx, -dy)) % 360
        teammates.append({
            "x": x, "y": y,
            "dx": dx, "dy": dy,
            "heading": heading,
            "icon": icon,
        })
    return teammates


def get_throttle():
    """获取当前油门百分比（0~100），多引擎取平均值，失败返回 None"""
    state = fetch_json("/state")
    if not state or not state.get("valid"):
        return None
    # 收集所有引擎的油门值
    throttles = []
    for k, v in state.items():
        if k.startswith("throttle") and isinstance(v, (int, float)):
            throttles.append(v)
    if not throttles:
        return None
    return sum(throttles) / len(throttles)


def is_teammate_heading_to_zone(zone_nx, zone_ny, angle_threshold=45):
    """检查是否有队友在我前方飞向同一战区

    判断标准：
      1. 队友在我（玩家）和战区之间（队友在 P→Z 方向上，夹角 ≤ angle_threshold）
      2. 队友比我更接近战区（队友→战区距离 < 我→战区距离）

    参数:
        zone_nx, zone_ny — 战区的归一化坐标 (0~1)
        angle_threshold — 方向锥角度阈值（度），默认 45°

    返回:
        (count, [teammate_info])  — 我前方飞向该战区的队友数量及列表
    """
    player = get_player()
    if not player:
        return 0, []
    px, py = player["x"], player["y"]

    # 我→战区的方向向量
    pz_x = zone_nx - px
    pz_y = zone_ny - py
    pz_dist = math.sqrt(pz_x * pz_x + pz_y * pz_y)
    if pz_dist < 0.001:
        return 0, []

    teammates = get_teammates()
    heading_toward = []
    for t in teammates:
        # 我→队友的方向向量
        pt_x = t["x"] - px
        pt_y = t["y"] - py
        pt_dist = math.sqrt(pt_x * pt_x + pt_y * pt_y)
        if pt_dist < 0.001:
            continue  # 队友就在我身上，忽略

        # 判断队友是否在我前方（P→Z 与 P→T 的夹角）
        cos_cone = (pz_x * pt_x + pz_y * pt_y) / (pz_dist * pt_dist)
        cos_cone = max(-1, min(1, cos_cone))
        cone_angle = math.degrees(math.acos(cos_cone))
        if cone_angle > angle_threshold:
            continue  # 不在我前方锥形范围内

        # 队友是否比我更接近战区？
        tz_dist = math.sqrt((zone_nx - t["x"]) ** 2 + (zone_ny - t["y"]) ** 2)
        if tz_dist >= pz_dist:
            continue  # 队友离战区更远，不会抢

        heading_toward.append(t)

    return len(heading_toward), heading_toward
