# -*- coding: utf-8 -*-
"""飞往敌方机场段的控制律（纯数值：无 IO、无第三方依赖）

**为什么要单独一个模块**：`tools/nav_replay.py` 要拿"真正在飞的那份代码"做闭环
仿真。如果回放工具自己再写一遍控制律，它证明的就只是"两句相似的公式都收敛"，
而不是 app.py 里那段在跑的东西。抽出来之后，app.py 只负责取数、打印、发手柄。

这是**下滑道耦合器**，不是"按距离推杆"的开环定时器：进场时锁定一条从当前位置连到
"机场上方 target_alt"的直线（tan0 就是它的斜率），整段导航咬住它。计划线锁定的
意义是它**固定** —— 飞机偏出去就有一个持续存在的高度差把它拉回来；而"每步重算到
机场的视线角"等于永远在追"我现在在哪"那条线，只有速率项没有位置项。

三个通道：

  横向  rx = clamp((kp*误差 - kd*实测转弯率) / 满杆转弯率, ±yaw_max)
        增益的单位是 1/秒 ⇒ 与采样率无关；dt 变快变慢只是每步修正量变，整定值不变。
        死区只吃比例项：飞机正在快速转头、误差刚好穿过死区时，微分项必须照常反打。
  纵向  ry = clamp(kv*(实测下降率 - 计划下降率) - ka*(高度 - 计划线高度), -push, +pull)
        正 ry = 抬头。push < pull，俯冲过陡时一定有权限拉起来（原实现只产出负值，
        完全没有抬头分支，过陡就不可逆 —— logs/20260928_152045.log 里 14° 的期望
        飞成了 36° 的实际）。
  油门  表速环（保住能量）+ 按下降率误差的有限配平（±0.16 封顶，只做配平不救命）。

不加积分项：采样周期与被控对象都不确定，I 项必然振荡。别加回来。

符号约定（本模块唯一的约定，全篇照此，也和 app.py 里对得上）：
  vs    > 0 = **下沉**（与 app._update_flight_state 注释里"正=上升"那句相反 ——
        那句注释和它自己的算式不一致，这里以算式为准）
  ry    > 0 = 抬头   （app._tracking_stick: ry = map_val(-dy, ...)，机头朝上为正）
  rx    > 0 = 右转
  error > 0 = 目标在右侧
"""

import math


class NavLaw:
    """有状态的控制器：内部只存三个量（转弯率低通、上一步俯仰、横向滞回标志）。

    一局导航建一个实例；重新导航就 reset()。其余输入每步传入。
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.omega_f = None       # 实测转弯率的低通值（度/秒，右转为正）
        self.ry = 0.0             # 上一步俯仰指令（限速从一个真实值开始）
        self.rx_active = False    # 横向最小输出地板的滞回状态

    def step(self, tunes, meas, dt_ref, dt_step):
        """走一步。

        tunes   整定值 dict（每步重新传入 ⇒ GUI 热改立刻生效），键：
                  turn_rate_max, yaw_kp, yaw_kd, yaw_dz, omega_tau, yaw_max,
                  pitch_kv, pitch_ka, pitch_push, pitch_pull, pitch_rate,
                  throttle_nom, throttle_k, throttle_kv, throttle_min,
                  ias_ref, target_alt, min_v
        meas    实测值 dict：dist_m, arrival_m, altitude(None=拿不到), ias(None),
                  gs(地速 m/s), error(度), omega(原始转弯率 度/秒), vs(None), tan0
        dt_ref  求变化率用的采样间隔（秒）
        dt_step 距上一步的实际间隔（秒），用于限速积分

        返回 dict(rx, ry, throttle, alt_plan, alt_err, vs_ref, arrived)
        """
        dist_m = meas["dist_m"]
        arrival_m = meas["arrival_m"]
        altitude = meas["altitude"]
        tan0 = meas["tan0"]
        vs = meas["vs"]
        gs = meas["gs"]

        # ---- 实测转弯率低通：航向差反推出来的转速本来就很毛 ----
        if self.omega_f is None:
            self.omega_f = meas["omega"]
        else:
            self.omega_f += (meas["omega"] - self.omega_f) * min(
                1.0, (dt_step or dt_ref) / max(tunes["omega_tau"], 1e-3))

        # ---- 计划下滑线在本处的期望值；alt_err 正 = 高于计划线 ----
        alt_plan = tunes["target_alt"] + (dist_m - arrival_m) * tan0
        alt_err = (altitude - alt_plan) if altitude is not None else 0.0
        closure = max(0.0, gs * math.cos(math.radians(meas["error"])))  # 朝机场的接近率
        vs_ref = closure * tan0                                          # 正 = 应当下沉

        arrived = dist_m <= arrival_m
        if arrived:
            # 已到机场上空：平飞掠过，不追视线角（dist→0 时那个 atan2 会炸）。
            # 设计意图是"低空让防空炮打"，所以是平飞，不是俯冲撞地。
            ry_t = 0.0
            rx = 0.0
            self.rx_active = False
        else:
            # ---- 纵向：下降率误差（主）+ 偏离计划线（次） ----
            # 沉得比计划快 ⇒ vs-vs_ref>0 ⇒ ry 为正 ⇒ 抬头。
            # 两项的时间尺度差一档（速率环几秒、位置环约 15 秒，见 config.json 里
            # nav_pitch_ka 的说明），所以不会互相打架。
            if vs is not None:
                ry_t = tunes["pitch_kv"] * (vs - vs_ref) - tunes["pitch_ka"] * alt_err
            else:
                ry_t = self.ry        # 拿不到高度：保持上一步，绝不悄悄改成平飞
            ry_t = max(-tunes["pitch_push"], min(tunes["pitch_pull"], ry_t))

            # ---- 横向：速率指令式 PD ----
            err = meas["error"]
            dz = tunes["yaw_dz"]
            e_p = math.copysign(abs(err) - dz, err) if abs(err) > dz else 0.0
            omega_cmd = tunes["yaw_kp"] * e_p - tunes["yaw_kd"] * self.omega_f
            rx_raw = max(-tunes["yaw_max"], min(tunes["yaw_max"],
                                                omega_cmd / max(tunes["turn_rate_max"], 1e-3)))
            # 最小输出地板（沿用 app.py guide 阶段的 MIN_V*0.35）带滞回：指令太小就
            # 不再输出，免得采样率一高就反复 ±0.26 抖舵；但一旦开始修正就保持到指令
            # 跌破地板的一半，而不是一步一抖。
            floor = tunes["min_v"] * 0.35
            mag = abs(rx_raw)
            if mag == 0.0:
                rx, self.rx_active = 0.0, False
            elif mag >= floor:
                rx, self.rx_active = rx_raw, True
            elif self.rx_active:
                rx = math.copysign(floor, rx_raw)
            else:
                rx = 0.0

        # ---- 俯仰指令限速（按实际步长积分，不是按 dt_ref） ----
        step = tunes["pitch_rate"] * (dt_step or dt_ref)
        self.ry += max(-step, min(step, ry_t - self.ry))
        ry = self.ry

        # ---- 油门：表速环 + 下降率配平 ----
        # 推力在这套"鼠标瞄准=姿态指令"的模型里是航迹角的主控量之一：沉得比计划快
        # 就加油门把航迹拉平。误差按 ±20 m/s 截断（最多 ±0.16 油门）—— 只想让它做
        # 配平，不想让它代替升降舵去救命；觉得跟纵向环打架就把 nav_throttle_kv 设 0。
        thr = tunes["throttle_nom"]
        if meas["ias"] is not None:
            thr += tunes["throttle_k"] * (tunes["ias_ref"] - meas["ias"])
        if vs is not None:
            thr += tunes["throttle_kv"] * max(-20.0, min(20.0, vs - vs_ref))
        throttle = max(tunes["throttle_min"], min(1.0, thr))

        return {
            "rx": rx,
            "ry": ry,
            "throttle": throttle,
            "alt_plan": alt_plan,
            "alt_err": alt_err,
            "vs_ref": vs_ref,
            "arrived": arrived,
        }
