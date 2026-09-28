# -*- coding: utf-8 -*-
"""飞往机场段的离线校验台（不需要游戏、只用标准库）

用法:
    python tools/nav_replay.py selftest            # 合成被控对象做闭环仿真 + 增益扫描
    python tools/nav_replay.py replay logs/xxx.log # 把日志里的真实轨迹喂给同一份控制律

**为什么要有这个工具。** `App._navigate_to_airfield` 只有在游戏里才会跑到，而它上一版
的问题恰恰是"指令与实际航迹完全脱钩"：13° 的期望飞成了 36° 的实际，日志还把期望角
当成俯冲角打出来。这种错在真机上只能靠"摔一次"发现，代价太大。所以控制律被抽到
`common/nav_law.py`（纯数值、无 IO），这里直接调**同一份代码**做两件事。

**这个工具能证明什么、不能证明什么。** 写清楚，别把它当成飞行验证：

- `selftest`：被控对象是**假设**（见 Plane），增益是猜的（真机没测过）。它证明的是
  "在这族可能的被控对象上，闭环不发散、不撞地、能咬住下滑线"，以及"增益量级选得
  不离谱"。它**不能**证明真机上的具体整定值是对的 —— 那只能飞一局看日志。
- `replay`：喂进去的是**旧控制律飞出来的轨迹**，飞机不会因为新指令改变轨迹。所以它
  只能证明"面对这条真实轨迹，新律给出的指令方向/量级是对的"（最要紧的一条：过陡时
  ry 必须变成**正**，而旧代码根本产不出正值）。它不证明闭环收敛。

两条论断合起来才够用：一个是"在被控对象族上稳定"，一个是"在真实数据上方向正确"。
"""

import argparse
import io
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.nav_law import NavLaw                                    # noqa: E402

try:
    from common.config import (NAV_TURN_RATE_MAX, NAV_YAW_KP, NAV_YAW_KD, NAV_YAW_DZ,
                               NAV_OMEGA_TAU, NAV_YAW_MAX, NAV_PITCH_KV, NAV_PITCH_KA,
                               NAV_PITCH_PUSH, NAV_PITCH_PULL, NAV_PITCH_RATE,
                               NAV_THROTTLE_NOM, NAV_THROTTLE_K, NAV_THROTTLE_KV,
                               NAV_THROTTLE_MIN, NAV_IAS_REF, NAV_TARGET_ALT,
                               AIRFIELD_ARRIVAL_RATIO, MIN_V)
    _TUNABLE = True
except Exception as exc:                                             # pragma: no cover
    print(f'警告: 读不到 common/config.py ({exc})，改用内置默认值')
    _TUNABLE = False

TU = {
    'turn_rate_max': NAV_TURN_RATE_MAX if _TUNABLE else 12.0,
    'yaw_kp': NAV_YAW_KP if _TUNABLE else 0.25,
    'yaw_kd': NAV_YAW_KD if _TUNABLE else 0.35,
    'yaw_dz': NAV_YAW_DZ if _TUNABLE else 3.0,
    'omega_tau': NAV_OMEGA_TAU if _TUNABLE else 1.0,
    'yaw_max': NAV_YAW_MAX if _TUNABLE else 0.55,
    'pitch_kv': NAV_PITCH_KV if _TUNABLE else 0.003,
    'pitch_ka': NAV_PITCH_KA if _TUNABLE else 0.0020,
    'pitch_push': NAV_PITCH_PUSH if _TUNABLE else 0.30,
    'pitch_pull': NAV_PITCH_PULL if _TUNABLE else 0.80,
    'pitch_rate': NAV_PITCH_RATE if _TUNABLE else 0.40,
    'throttle_nom': NAV_THROTTLE_NOM if _TUNABLE else 0.45,
    'throttle_k': NAV_THROTTLE_K if _TUNABLE else 0.002,
    'throttle_kv': NAV_THROTTLE_KV if _TUNABLE else 0.008,
    'throttle_min': NAV_THROTTLE_MIN if _TUNABLE else 0.25,
    'ias_ref': NAV_IAS_REF if _TUNABLE else 500,
    'target_alt': NAV_TARGET_ALT if _TUNABLE else 50,
    'min_v': MIN_V if _TUNABLE else 0.26,
}
ARRIVAL_RATIO = AIRFIELD_ARRIVAL_RATIO if _TUNABLE else 0.016
MAP_SIZE = 65536.0          # 旧日志没记地图尺寸；可用 --map-size 覆盖
DT = 1.0 / 6.0              # 采样周期：127.0.0.1 修好后实测约 6Hz
DEAD_REF = 2                # 日志里那次 4 秒一步的老代码，相当于 0.67s 的死区


# ==================== 合成被控对象 ====================

class Plane:
    """极简被控对象 —— 这是**假设**，不是测量。

    纵向: 航迹角 gamma（度，正=下沉）一阶趋近 -k_gamma*ry（ry>0 抬头 ⇒ gamma 变小），
          并被油门影响 k_thr（>0 = 加油门让航迹变平，这是推力的物理方向；扫描时会
          正负都试，用来回答"万一符号反了会怎样"）。
    横向: 转弯率 omega（度/秒）一阶趋近 k_omega*rx。
    指令要经过 dead 步的延迟才生效（真机上有教官/舵面响应）。

    gamma 目标被限在 [-60, +70]：真飞机不可能靠一根杆量无限改变航迹角，不加这个
    饱和，全杆抬头会算出 -112° 的航迹角，仿真就变成数学游戏了。
    """

    def __init__(self, k_gamma, tau_gamma, k_thr, k_omega, tau_omega, dead, gs0=130.0):
        self.k_gamma, self.tau_gamma, self.k_thr = k_gamma, tau_gamma, k_thr
        self.k_omega, self.tau_omega = k_omega, tau_omega
        self.dead = dead
        self.gs = gs0
        self.buf = [(0.0, 0.0, TU['throttle_nom'])] * max(dead, 1)

    def push(self, rx, ry, thr):
        self.buf.append((rx, ry, thr))
        while len(self.buf) > max(self.dead, 1):
            self.buf.pop(0)
        return self.buf[0]


# ==================== 闭环仿真 ====================

def simulate(k_gamma=140.0, tau_gamma=1.0, k_thr=60.0, k_omega=12.0, tau_omega=2.0,
             dead=DEAD_REF, gamma0=0.0, error0=-25.0, dist0=11000.0, alt0=3300.0,
             t_max=240.0, dt=DT, verbose=False, over=None):
    """跑一条进场轨迹：从 (dist0, alt0) 咬着计划下滑线飞到机场上空。

    注意两点，弄错了仿真就没意义：
      1. 飞机的**真实地速由被控对象自己决定**（plane.gs），控制器测到的 gs 只是它的
         观测值。不能反过来用"控制器测到的 gs"去推进飞机 —— 那等于自己观测自己。
      2. 采样窗（1 秒）必须保留：控制器看到的 vs/gs/omega 都是从 1 秒前的样本差出来
         的，和 app.py 里一模一样。所以历史里预置了一个 -1 秒的样本，否则起步时
         gs 会被算成 0。

    over: 临时覆盖整定值（扫描用），不写回 TU。

    返回 dict(steps, crash, plan_alt_err, err_tail_max, gam_err_max, thr_min, n_pull,
             gam_tail_pp: 最后 20 秒航迹角的峰峰值 = 振荡幅度)
    """
    tu = dict(TU)
    if over:
        tu.update(over)
    law = NavLaw()
    plane = Plane(k_gamma, tau_gamma, k_thr, k_omega, tau_omega, dead)
    arrival_m = max(200.0, ARRIVAL_RATIO * MAP_SIZE)

    alt, dist, gamma, omega = alt0, dist0, gamma0, 0.0
    hdg = -error0                      # 相对航向；误差 error = -hdg
    slope0 = math.atan2(max(0.0, alt - tu['target_alt']), max(dist - arrival_m, 100.0))
    slope0 = max(math.radians(3.0), min(math.radians(35.0), slope0))
    tan0 = math.tan(slope0)

    # 预置 -1s 的样本：假设那 1 秒是匀速直线（gs0 / 航迹角 gamma0 / 转弯率 0）
    vs0 = plane.gs * math.tan(math.radians(gamma0))
    hist = [(-1.0, dist0 + plane.gs, alt0 + vs0, hdg)]
    rows = []
    crash = False
    t = 0.0
    while t < t_max:
        t += dt
        hist.append((t, dist, alt, hdg))
        ref = None
        for s in hist:
            if t - s[0] >= 1.0:
                ref = s
            else:
                break
        if ref is None:
            continue
        dtr = t - ref[0]
        gs = max(1.0, (ref[1] - dist) / dtr)              # 观测地速
        vs = (ref[2] - alt) / dtr                         # 观测下降率（正 = 下沉）
        omega_raw = (hdg - ref[3]) / dtr                  # 观测转弯率
        meas = dict(dist_m=dist, arrival_m=arrival_m, altitude=alt,
                    ias=gs * 3.6, gs=gs, error=-hdg, omega=omega_raw, vs=vs, tan0=tan0)
        out = law.step(tu, meas, dtr, dt)
        rx, ry, thr = out['rx'], out['ry'], out['throttle']
        rows.append((t, dist, alt, -hdg, gamma, vs, out['vs_ref'], ry, rx, thr))

        if out['arrived']:
            break

        # ---- 被控对象推进（用真实地速，不用观测地速）----
        a_rx, a_ry, a_thr = plane.push(rx, ry, thr)
        g_t = -k_gamma * a_ry - k_thr * (a_thr - tu['throttle_nom'])
        g_t = max(-60.0, min(70.0, g_t))       # 真飞机不可能靠一根杆量无限改航迹角
        gamma += (g_t - gamma) * min(1.0, dt / max(tau_gamma, 1e-3))
        omega += (k_omega * a_rx - omega) * min(1.0, dt / max(tau_omega, 1e-3))
        hdg += omega * dt
        vs_now = plane.gs * math.tan(math.radians(gamma))
        alt -= vs_now * dt
        dist -= plane.gs * dt
        if alt <= 0.0:
            crash = True
            alt = 0.0
            break
        if verbose and len(rows) % 30 == 0:
            print(f'  t={t:6.1f} d={dist/1000:5.2f}km alt={alt:6.0f}m err={-hdg:+6.1f}° '
                  f'gamma={gamma:+6.1f}° plan={math.degrees(slope0):5.1f}° ry={ry:+.2f} '
                  f'rx={rx:+.2f} thr={thr:.2f}')

    gam_plan = math.degrees(slope0)
    tail = rows[-int(20 / dt):]
    return dict(
        steps=len(rows), crash=crash,
        gam_plan=gam_plan,
        err_tail_max=max(abs(r[3]) for r in tail) if tail else None,
        gam_err_max=max(abs(r[4] - gam_plan) for r in rows) if rows else None,
        thr_min=min(r[9] for r in rows) if rows else None,
        plan_alt_err=(rows[-1][2] - tu['target_alt']) if rows else None,
        n_pull=sum(1 for r in rows if r[7] > 0),
        gam_tail_pp=(max(r[4] for r in tail) - min(r[4] for r in tail)) if tail else None,
    )


def cmd_selftest(args):
    print('== 闭环仿真（被控对象是假设，只有真机才能验证整定值）==')
    rows = []
    fails = []
    for k_gamma in (20, 40, 60, 90, 120, 140):
        for k_thr in (-125, -60, 0, 60, 125):
            k_omega = 12
            r = simulate(k_gamma=k_gamma, k_thr=k_thr, k_omega=k_omega)
            ok = (not r['crash']) and r['thr_min'] >= TU['throttle_min'] - 1e-9
            # 横向：最后 20 秒的残余误差必须收敛（否则是极限环）
            ok = ok and (r['err_tail_max'] < 10.0)
            rows.append((k_gamma, k_thr, k_omega, r, ok))
            if not ok:
                fails.append((k_gamma, k_thr, k_omega, r))
    print('%-8s %-7s %-8s | %-7s %-9s %-9s %-8s' %
          ('k_gamma', 'k_thr', 'k_omega', '撞地', '到位高差', '尾段偏航', '油门最低'))
    for k_gamma, k_thr, k_omega, r, ok in rows:
        print('%-8g %-7g %-8g | %-7s %-9s %-9s %-8.2f %s' % (
            k_gamma, k_thr, k_omega, '是' if r['crash'] else '否',
            f"{r['plan_alt_err']:+.0f}m" if r['plan_alt_err'] is not None else '-',
            f"{r['err_tail_max']:.1f}°" if r['err_tail_max'] is not None else '-',
            r['thr_min'], 'OK' if ok else '<<< 不过'))

    # 横向增益扫描（被控对象转弯能力未知）
    print('\n-- 横向：只扫转弯能力（k_gamma=140, k_thr=60）--')
    lat_fail = []
    for k_omega in (4, 6, 12, 20, 30):
        r = simulate(k_omega=k_omega)
        ok = (not r['crash']) and r['err_tail_max'] < 10.0
        if not ok:
            lat_fail.append((k_omega, r))
        print('  k_omega=%-4g 尾段偏航残余 %-8s 到位高差 %s %s' % (
            k_omega, f"{r['err_tail_max']:.1f}°",
            f"{r['plan_alt_err']:+.0f}m", 'OK' if ok else '<<< 不过'))

    # 过陡拉起：这是用户报的那个 bug 的直接回归测试
    print('\n-- 回归：进场时已经是个 45° 俯冲（旧代码拉不起来的那种状态）--')
    rec = []
    for k_gamma in (20, 60, 140):
        r = simulate(k_gamma=k_gamma, gamma0=45.0, alt0=2000.0, dist0=8000.0, k_thr=60)
        ok = (not r['crash'])
        rec.append((k_gamma, r, ok))
        print('  k_gamma=%-4g 撞地=%-4s 到位高差 %-8s 最大航迹偏差 %s %s' % (
            k_gamma, '是' if r['crash'] else '否',
            f"{r['plan_alt_err']:+.0f}m" if r['plan_alt_err'] is not None else '-',
            f"{r['gam_err_max']:.0f}°", 'OK' if ok else '<<< 不过'))

    bad = fails or lat_fail or [x for x in rec if not x[2]]
    print()
    if bad:
        print('FAIL: %d 组不过' % len(bad))
        return 1
    print('PASS: %d 组纵向 + 5 组横向 + 3 组拉起，全部不发散、不撞地、横向收敛' % len(rows))
    print('注意：这只证明"在被控对象族上稳定"，真机整定值仍需飞一局看日志。')
    return 0


# ==================== 真实日志回放 ====================

RE_OLD = re.compile(r'\[(\d+):(\d+):(\d+)\]\s*机场\s*([\d.]+)km\s*\((左|右)偏\s*(\d+)°\)'
                    r'\s*高\s*(\d+)m\s*俯冲角\s*(\d+)°\s*油门\s*([\d.]+)')
RE_NEW = re.compile(r'\[(\d+):(\d+):(\d+)\]\s*机场\s*([\d.]+)km\s*\((左|右)偏\s*(\d+)°\)'
                    r'\s*高\s*(\d+)m\s*\|\s*航迹\s*([+-]?\d+)°\s*计划\s*([+-]?\d+)°'
                    r'\s*高差\s*([+-]?\d+)m')
RE_SLOPE = re.compile(r'下滑线\s*([\d.]+)°')


def parse_log(path):
    """抽出导航段的 (t秒, dist_m, error°, alt_m[, gamma°]) 序列"""
    samples = []
    for line in io.open(path, encoding='utf-8', errors='replace'):
        m = RE_NEW.search(line)
        if m:
            h, mi, s, km, side, err, alt, gam = m.groups()
            samples.append(dict(t=int(h) * 3600 + int(mi) * 60 + int(s),
                                dist=float(km) * 1000, err=float(err) * (-1 if side == '左' else 1),
                                alt=float(alt), gam_new=float(gam)))
            continue
        m = RE_OLD.search(line)
        if m:
            h, mi, s, km, side, err, alt = m.groups()[:7]
            samples.append(dict(t=int(h) * 3600 + int(mi) * 60 + int(s),
                                dist=float(km) * 1000, err=float(err) * (-1 if side == '左' else 1),
                                alt=float(alt), gam_new=None))
    return samples


def cmd_replay(args):
    rc = 0
    for path in args.logs:
        samples = parse_log(path)
        print('\n== %s：导航段 %d 个样本 ==' % (path, len(samples)))
        if len(samples) < 3:
            print('  样本太少（旧日志里只有 11-21 行），跳过')
            continue
        arrival_m = max(200.0, ARRIVAL_RATIO * (args.map_size or MAP_SIZE))
        slope0 = None
        for line in io.open(path, encoding='utf-8', errors='replace'):
            m = RE_SLOPE.search(line)
            if m:
                slope0 = math.radians(float(m.group(1)))
                break
        if slope0 is None:
            alt0, dist0 = samples[0]['alt'], samples[0]['dist']
            slope0 = math.atan2(max(0.0, alt0 - TU['target_alt']),
                                max(dist0 - arrival_m, 100.0))
            slope0 = max(math.radians(3.0), min(math.radians(35.0), slope0))
        tan0 = math.tan(slope0)
        print('  计划下滑线 %.1f°（%s）' % (math.degrees(slope0),
                                          '日志里的下滑线行' if RE_SLOPE.search(
                                              ''.join(io.open(path, encoding='utf-8',
                                                              errors='replace')))
                                          else '按进场点复算（旧日志没有这行）'))

        law = NavLaw()
        worst = None
        n_upset = 0
        for prev, cur in zip(samples, samples[1:]):
            dt = cur['t'] - prev['t']
            if dt <= 0:
                continue
            gs = max(1.0, (prev['dist'] - cur['dist']) / dt)
            vs = (prev['alt'] - cur['alt']) / dt                     # 正 = 下沉
            closure = max(0.0, gs * math.cos(math.radians(cur['err'])))
            meas = dict(dist_m=cur['dist'], arrival_m=arrival_m, altitude=cur['alt'],
                        ias=gs * 3.6, gs=gs, error=cur['err'], omega=0.0, vs=vs, tan0=tan0)
            out = law.step(TU, meas, dt, dt)
            # 不变式 1：指令不得与"该给的修正方向"相反（限速不改变符号）
            want = TU['pitch_kv'] * (vs - out['vs_ref']) - TU['pitch_ka'] * out['alt_err']
            if out['ry'] != 0 and want != 0 and (out['ry'] > 0) != (want > 0):
                print('  !! 符号不一致 t=%s d=%.1fkm: ry=%+.2f want=%+.2f'
                      % (cur['t'], cur['dist'] / 1000, out['ry'], want))
                rc = 1
            if out['ry'] > 0:
                n_upset += 1
            if abs(out['rx']) > TU['yaw_max'] + 1e-9 or abs(out['ry']) > TU['pitch_pull'] + 1e-9:
                print('  !! 输出越界 rx=%+.3f ry=%+.3f' % (out['rx'], out['ry']))
                rc = 1
            if out['throttle'] < TU['throttle_min'] - 1e-9:
                print('  !! 油门低于地板 %.3f' % out['throttle'])
                rc = 1
            mark = ''
            if vs > out['vs_ref'] + 5 and out['ry'] > 0:
                mark = '   <- 沉的比计划快，指令抬头（旧代码这里只会给负值）'
            print('  %5.1fkm 高%5.0fm 偏%+4.0f° | 航迹 %+4.0f° 计划 %+4.0f° vs %+5.1f/%+5.1f '
                  '| 俯仰 %+.2f 油门 %.2f%s'
                  % (cur['dist'] / 1000, cur['alt'], cur['err'],
                     math.degrees(math.atan2(vs, max(gs, 1.0))), math.degrees(slope0),
                     vs, out['vs_ref'], out['ry'], out['throttle'], mark))
            if worst is None or out['alt_err'] < worst:
                worst = out['alt_err']
        print('  共 %d 步指令抬头（旧实现恒为 0）' % n_upset)
    return rc


def main():
    ap = argparse.ArgumentParser(description='飞往机场段离线校验台')
    sub = ap.add_subparsers(dest='cmd')
    st = sub.add_parser('selftest', help='合成被控对象闭环仿真 + 增益扫描')
    st.set_defaults(fn=cmd_selftest)
    rp = sub.add_parser('replay', help='把日志里的真实轨迹喂给同一份控制律')
    rp.add_argument('logs', nargs='+')
    rp.add_argument('--map-size', type=float, default=None)
    rp.set_defaults(fn=cmd_replay)
    args = ap.parse_args()
    if not getattr(args, 'fn', None):
        ap.print_help()
        return 2
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
