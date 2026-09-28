# -*- coding: utf-8 -*-
"""战果解析的离线回归：把 logs/ 里真实记录下来的结算画面喂给 common/results.py。

不需要游戏，只用 stdlib。**改过 LABELS 或 SELF_WINDOW_MARKERS 之后一定要跑一次** ——
这张标签表只能拿真实 OCR 验证，跑不了这个脚本就等于在盲改。

    python tools/results_check.py

夹具是 logs/ 里两份已经记录好的日志：
  * scenes_20260928_162128.jsonl —— 真画面（8 帧 战果汇总，texts 的 md5 全同）
  * scenes_20260928_114345.jsonl —— 脏画面（3 帧，其实是机器人自己的 GUI + 终端）

logs/ 是可以清理的，所以夹具不在时**跳过并说明**，不算失败。真正会判失败的是：
真画面的解析结果和当时记下来的值对不上，或者脏画面没被标出来。
"""

import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.results import parse, self_window_hits        # noqa: E402

# 2026-09-28 16:21 那局突尼斯战役的真值，直接从 OCR 文本里读出来的
FIXTURE_CLEAN = "logs/scenes_20260928_162128.jsonl"
EXPECT_CLEAN = {
    "空中目标击坠数": "0",
    "轰炸基地TNT吨": "0.136",
    "摧毁基地": "3801",
    "活跃时长": "0:24",
    "活跃度": "64%",
    "总计": "169",
    "游戏时长": "4:17",
    "任务时长": "6:55",
    "自由研发点": "169",
}

FIXTURE_DIRTY = "logs/scenes_20260928_114345.jsonl"
EXPECT_DIRTY_HITS = ["投弹视角距离", "python main.py"]


def load_frames(path):
    """取出这份日志里所有 scene == 战果汇总 的 (t, texts)。"""
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("scene") == "战果汇总":
                out.append((o.get("t"), o.get("texts") or []))
    return out


def main():
    bad = 0
    ran = 0
    env = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    def out(s=""):
        env.write(s + "\n")

    # ---- 真画面：进入场景那一帧必须 9 个字段全中 ----
    #
    # 注意这份夹具里有 9 帧而不是 8 帧：最后一帧（16:22:06）的 raw 已经是「大厅」，
    # 但 scene 还停在「战果汇总」—— `_stabilize_scene` 要连续 2 次命中才换锁，
    # 所以**一次停留的尾巴上那几帧带的是下一个画面的文字**（那一帧 81 个 token，
    # 明显是机库）。这正是"必须按场景进入记一次、不能按帧记"的直接证据，
    # 所以这里只对第一帧（evt=change）做断言，尾巴那几帧只报告不判错。
    if not os.path.exists(FIXTURE_CLEAN):
        out("跳过真画面夹具 %s（logs/ 被清理过？）" % FIXTURE_CLEAN)
    else:
        ran += 1
        frames = load_frames(FIXTURE_CLEAN)
        out("== %s：%d 帧 ==" % (FIXTURE_CLEAN, len(frames)))
        if not frames:
            out("  这份日志里没有 战果汇总 帧")
            bad += 1
        else:
            map_name, fields = parse(frames[0][1])
            hits = self_window_hits(frames[0][1])
            if map_name != "历史性能，【军事行动】突尼斯战役（不可重生）":
                out("  进入帧的地图串不对: %r" % map_name)
                bad += 1
            if fields != EXPECT_CLEAN:
                out("  进入帧字段不匹配")
                for k in sorted(set(EXPECT_CLEAN) | set(fields)):
                    if EXPECT_CLEAN.get(k) != fields.get(k):
                        out("      %-18s 期望 %r 实得 %r"
                            % (k, EXPECT_CLEAN.get(k), fields.get(k)))
                bad += 1
            if hits:
                out("  进入帧被误判成脏画面: %s" % hits)
                bad += 1
            # 静止的那一段必须解析成完全一样的结果（对应"md5 全同"那条证据）
            key = json.dumps([map_name, sorted(fields.items())], ensure_ascii=False)
            same = 0
            for _, texts in frames:
                m2, f2 = parse(texts)
                if json.dumps([m2, sorted(f2.items())], ensure_ascii=False) == key:
                    same += 1
            out("  地图: %s" % map_name)
            for k, v in fields.items():
                out("    %-18s %s" % (k, v))
            out("  %d/%d 帧解析结果相同（其余是换画面后锁还没换的尾巴）" % (same, len(frames)))
            if same < 8:
                out("  静止段本该有 8 帧相同，只有 %d —— 画面变动或解析不稳" % same)
                bad += 1
            out("  进入帧: 9 字段齐全、值正确、无污染标记"
                if not bad else "  有 %d 处不符" % bad)

    # ---- 脏画面：必须被标出来 ----
    if not os.path.exists(FIXTURE_DIRTY):
        out("\n跳过脏画面夹具 %s" % FIXTURE_DIRTY)
    else:
        ran += 1
        frames = load_frames(FIXTURE_DIRTY)
        out("\n== %s：%d 帧（来自机器人自己的窗口，必须被判脏）=="
            % (FIXTURE_DIRTY, len(frames)))
        if not frames:
            out("  这份日志里没有 战果汇总 帧")
            bad += 1
        for i, (t, texts) in enumerate(frames):
            hits = self_window_hits(texts)
            if not hits:
                out("  帧%d 没被判为脏 —— 特征词表漏了" % i)
                bad += 1
                continue
            missing = [h for h in EXPECT_DIRTY_HITS if h not in hits]
            if missing:
                out("  帧%d 命中了 %s，但缺少 %s" % (i, hits, missing))
                bad += 1
            else:
                out("  帧%d 命中 %s  OK" % (i, hits))

    out("")
    if not ran:
        out("没有可用的夹具，什么都没检查。")
        return 0
    out("PASS" if not bad else "FAIL: %d 处不符" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
