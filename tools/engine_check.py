# -*- coding: utf-8 -*-
"""智能油门的离线回归：真帧验解析、假想被控对象验控制器。

    python tools/engine_check.py

不需要游戏，也不需要 cv2 —— `common/engine.py` 只 import re。

两部分：

1. **解析器**：把 logs/scenes_*.jsonl 里每一帧真 OCR 喂给 `parse_engine()`，
   断言解析率、值域、以及「屏上有告警就一定 hot」。
2. **控制器**：拿三种假想被控对象（台阶轴 / 线性轴 / 读不到）驱动 `SmartThrottle`，
   断言收敛后**不再动**（无极限环）、不越下限、读不到时一步不动。

**第 2 部分证明的只是「给一个台阶轴，控制器会收敛并停住」** —— 它不证明游戏里那根轴
就是那个台阶。台阶（阈值约 0.4）是从 logs/ 的 `节流阀` 读数和 `.log` 里 `手柄 L:(x,y)`
对齐反推出来的**假设**，不是实测。所以第一次飞行是标定飞行：看日志里那行
「智能油门: ...，摇杆 x→y」是不是真的在把读数拉下来。

logs/ 是可以清理的，夹具不在时**跳过解析那部分并说明**，控制器那部分照跑（它不依赖日志）。
"""

import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.engine import (                                      # noqa: E402
    parse_engine, SmartThrottle,
    STEP_DEFAULT, DEADBAND_DEFAULT, MIN_CMD_DEFAULT,
    HOT_PCT_DEFAULT, COOL_PCT_DEFAULT, COOL_S_DEFAULT,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 出厂整定值。控制器那部分**必须**用这一份 —— 收敛性对任何步长都成立，
# 但「几步收敛、停在哪个杆量」会变，所以断言写的是性质而不是具体数字。
_TUNES = dict(hot_pct=HOT_PCT_DEFAULT, cool_pct=COOL_PCT_DEFAULT,
              cool_s=COOL_S_DEFAULT, step=STEP_DEFAULT,
              deadband=DEADBAND_DEFAULT, lo=MIN_CMD_DEFAULT)

# 实测（5194 帧含 `节流阀`）解析率 99.52%，读不到的 25 帧里有 12 帧是标签后面直接跟
# 下一个仪表的名字。留一点余量，但低于 99% 就说明解析器被改坏了。
MIN_PARSE_RATE = 0.99


def load_frames():
    """扫所有场景 log，返回 [(日志名, t, texts), ...]。"""
    out = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "logs", "scenes_*.jsonl"))):
        for ln in io.open(path, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            out.append((os.path.basename(path), o.get("t"), o.get("texts") or []))
    return out


def run_loop(plant, hot_at, cmd=1.0, n=60):
    """跑一圈闭环。plant(cmd) -> pct 或 None；hot_at(k) -> 该轮是否过热。"""
    ctl = SmartThrottle(cmd)
    cmds, reads = [], []
    for k in range(n):
        reads.append(plant(ctl.cmd))
        cmds.append(ctl.cmd)
        ctl.cmd, _target = ctl.observe(float(k), hot_at(k), reads[-1], **_TUNES)
    return cmds, reads


def step_plant(thr=0.4, top=110, bot=100):
    """实测反推出来的台阶轴：杆量越过阈值就进加力，否则 100%。"""
    return lambda cmd: top if cmd >= thr else bot


def linear_plant(cmd):
    """假想的线性轴（用来确认控制器**不会**在上面自激）。"""
    return int(round(100 * max(0.0, min(1.0, cmd))))


def blind_plant(cmd):
    """永远读不到读数。"""
    return None


def main():
    env = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    def out(s=""):
        env.write(s + "\n")

    out("整定值: step=%.2f deadband=%.0f lo=%.2f 目标 %d%%(过热) / %d%%(正常) 冷却 %.0fs"
        % (STEP_DEFAULT, DEADBAND_DEFAULT, MIN_CMD_DEFAULT,
           HOT_PCT_DEFAULT, COOL_PCT_DEFAULT, COOL_S_DEFAULT))

    bad = 0

    # ==================== 1. 解析器（真帧） ====================
    out("\n== 解析器（logs/scenes_*.jsonl 真帧）==")
    frames = load_frames()
    if not frames:
        out("  logs/scenes_*.jsonl 一个都没有（logs/ 被清理过？），跳过这一部分。")
    else:
        n_lab = n_ok = 0
        hist = {}
        range_bad = []
        hot_miss = []
        for name, t, tx in frames:
            if not any("节流阀" in (x or "") for x in tx):
                continue
            n_lab += 1
            r = parse_engine(tx)
            if r["pct"] is None:
                continue
            n_ok += 1
            hist[r["pct"]] = hist.get(r["pct"], 0) + 1
            if not (0 <= r["pct"] <= 120):
                range_bad.append((name, t, r["pct"]))
        rate = n_ok / float(n_lab) if n_lab else 0.0
        out("  含 `节流阀` 的帧 %d，解析出值 %d（%.2f%%），读不到 %d"
            % (n_lab, n_ok, rate * 100, n_lab - n_ok))
        out("  值分布: %s"
            % ", ".join("%d×%d" % kv for kv in
                        sorted(hist.items(), key=lambda kv: -kv[1])[:8]))
        if rate < MIN_PARSE_RATE:
            bad += 1
            out("  ✗ 解析率 %.2f%% < %.0f%%" % (rate * 100, MIN_PARSE_RATE * 100))
        if range_bad:
            bad += 1
            out("  ✗ 有 %d 帧解析出不可能的值: %s" % (len(range_bad), range_bad[:3]))

        # 「屏上有告警就一定是 hot」。这一条是控制器唯一的触发信号，不能漏。
        n_hot = 0
        for name, t, tx in frames:
            if not any("油温过" in (x or "") or "水温过" in (x or "")
                       for x in tx):
                continue
            n_hot += 1
            r = parse_engine(tx)
            if not r["hot"]:
                hot_miss.append((name, t))
        out("  含 `油温过`/`水温过` 的帧 %d，漏判 %d" % (n_hot, len(hot_miss)))
        if hot_miss:
            bad += 1
            out("  ✗ 有告警却没判成 hot: %s" % hot_miss[:3])

        # 反过来：没有告警的帧不该被判 hot（否则飞机会莫名其妙一直不开加力）
        false_hot = [(name, t) for name, t, tx in frames
                     if parse_engine(tx)["hot"]
                     and not any("油温过" in (x or "") or "水温过" in (x or "")
                                 for x in tx)]
        if false_hot:
            bad += 1
            out("  ✗ 没告警却判成 hot: %s" % false_hot[:3])

    # ==================== 2. 控制器（假想被控对象） ====================
    out("\n== 控制器（离线仿真）==")

    def check(name, plant, hot_at, expect_read=None, hold_at=None, n=60):
        """期望：读数收敛到目标、后 20 轮杆量不再变、全程不越下限。"""
        nonlocal bad
        cmds, reads = run_loop(plant, hot_at, n=n)
        tail = cmds[-20:]
        errs = []
        if len(set(tail)) != 1:
            errs.append("后 20 轮杆量还在变 %s（极限环？）"
                        % sorted(set(round(c, 3) for c in tail)))
        if any(c < MIN_CMD_DEFAULT - 1e-9 or c > 1.0 + 1e-9 for c in cmds):
            errs.append("杆量越界 [%.2f, %.2f]"
                        % (min(cmds), max(cmds)))
        if expect_read is not None and reads[-1] != expect_read:
            errs.append("最终读数 %s，期望 %s" % (reads[-1], expect_read))
        if hold_at is not None and abs(tail[-1] - hold_at) > 1e-9:
            errs.append("停在杆量 %.3f，期望 %.3f" % (tail[-1], hold_at))
        if errs:
            bad += 1
            out("  ✗ %-22s %s" % (name, "；".join(errs)))
        else:
            out("  ✓ %-22s 收敛于杆量 %.2f / 读数 %s（后 20 轮不再动）"
                % (name, tail[-1], reads[-1]))
        return cmds, reads

    # 台阶轴：先凉后热 —— 应当降下来并停在「读数=100%」那一档。
    # **不写死停在哪个杆量**：那是 STEP 的函数（0.25 步长停在 0.25，0.10 步长停在 0.30），
    # 写死等于把这份检查绑在出厂步长上，一改 STEP 就要跟着改一个假数字。
    check("台阶轴 先凉后热", step_plant(), lambda k: k >= 3,
          expect_read=HOT_PCT_DEFAULT)
    # 台阶轴：先热后凉 —— 应当升上去并停回加力那一档
    check("台阶轴 先热后凉", step_plant(), lambda k: k < 3,
          expect_read=COOL_PCT_DEFAULT)
    # 线性轴：天花板就是 100%，没有加力可给 —— 顶在 1.0 不动是对的
    check("线性轴 一直热", linear_plant, lambda k: True, hold_at=1.0)
    check("线性轴 一直凉", linear_plant, lambda k: False, hold_at=1.0)
    # 线性轴 + 冷热抖动：迟滞生效后也不该自激
    check("台阶轴 冷热抖动", step_plant(), lambda k: (k // 2) % 2 == 0)

    # 读不到就一步不动 —— 这条是「宁可维持现状也不要瞎挪油门」的那条保证
    cmds, _ = run_loop(blind_plant, lambda k: True)
    if len(set(cmds)) != 1:
        bad += 1
        out("  ✗ 读不到读数时杆量动了: %s" % sorted(set(cmds)))
    else:
        out("  ✓ %-22s 60 轮读数全 None，杆量稳在 %.2f" % ("读不到读数", cmds[0]))

    # 读数被读成荒唐的大值：只能朝 100% 走（往下），而且不越下限
    cmds, _ = run_loop(lambda cmd: 999, lambda k: True)
    if cmds[-1] < MIN_CMD_DEFAULT - 1e-9:
        bad += 1
        out("  ✗ 荒唐读数把杆量拉到了下限以下: %.3f" % cmds[-1])
    else:
        out("  ✓ %-22s 读数一直 999，杆量收到下限 %.2f 就停住"
            % ("荒唐读数 999", cmds[-1]))

    # 100 秒以上的迟滞：告警消失但还没到 cool_s，就不该回加力
    ctl = SmartThrottle(1.0)
    ctl.observe(0.0, True, 100, **_TUNES)
    cmd_a, _ = ctl.observe(1.0, False, 100, **_TUNES)     # 告警刚消失
    cmd_b, _ = ctl.observe(COOL_S_DEFAULT - 1.0, False, 100, **_TUNES)
    if not ctl.hot:
        bad += 1
        out("  ✗ %.1f 秒就没有告警了，却已经认为凉了（迟滞没生效）"
            % (COOL_S_DEFAULT - 1.0))
    else:
        out("  ✓ %-22s 告警消失后 %.0f 秒内仍算过热"
            % ("告警迟滞", COOL_S_DEFAULT))

    out("")
    out("PASS" if not bad else "FAIL: %d 处不符" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
