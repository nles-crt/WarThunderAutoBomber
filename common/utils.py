"""控制算法工具函数"""


def map_val(e, half, sense=0.6, min_v=0.26, scale=0.15):
    """将像素偏差映射为摇杆输出值

    参数:
        e:     偏差值（像素），正负表示方向
        half:  屏幕半宽/半高（像素）
        sense: 灵敏度 (0~1)，越大满杆输出越高
        min_v: 最小输出值，死区外最低推杆力度
        scale: 映射比例，越小转向越激进

    返回:
        float: -1 ~ 1 的摇杆值，0.0 = 死区内不输出
    """
    dz = max(3, half * 0.015)
    if abs(e) <= dz:
        return 0.0
    t = (abs(e) - dz) / (half * scale - dz)
    mag = min_v + min(t, 1.0) * (sense - min_v)
    return mag if e > 0 else -mag
