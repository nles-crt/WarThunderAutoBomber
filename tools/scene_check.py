# -*- coding: utf-8 -*-
"""场景关键词表的离线回归：把 logs/ 里真实记录下来的画面重新喂给 detect/scene.py。

不需要游戏。会 import detect.scene，所以连带加载 cv2 / rapidocr（慢几秒）。

    python tools/scene_check.py

**改过 `_SCENES` 或 `KEYWORD_BUTTONS` 之后一定要跑一次。** 这张表是拿真实 OCR
样本实测出来的（见 CLAUDE.md 的场景识别一节），改错一个词的表现是"某个界面认不出来"
或者"某个界面被另一个抢走"，两种都只有真飞一局才看得见 —— 除非跑这个。

夹具不硬编码日志文件名：扫 logs/scenes_*.jsonl，按**标记词**挑帧，然后断言
**当前**分类器给的结论。日志里那些帧当时被判成什么（`未知` / `大厅`）与断言无关 ——
它们多数是在关键词补齐**之前**录的，这正是这份检查存在的意义。

真正会判失败的是：新加的两个场景认不出来、或者它们把别的界面抢了过来、
或者 `研发完毕` 那个超串误匹配又回来了。

logs/ 是可以清理的，所以夹具不在时**跳过并说明**，不算失败。
"""

import glob
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detect.scene import SceneDetector, get_keyword_actions      # noqa: E402

# 标记词（用来从日志里挑帧）-> 期望分类器给的场景
EXPECT_SCENE = [
    ("选择新的研发项",            "选择新的研发项"),
    ("还有改装件未购入",          "购买改装件确认"),
    ("开始任务前必须先点选",      "需选载具"),
    ("等待时间",                  "匹配中"),
]

# 标记词出现在帧里时，**不许**再触发这些关键词按键。
# 这条是那个真 bug 的回归测试：改装件界面上的按钮
# `购买所有研发完毕的改装件（1100条）` 是 `研发完毕` 的超串，
# 会让机器人一边按 X（选择新的研发）一边按 B（研发完毕）。
# 实测 logs/scenes_20260928_131739.jsonl 13:17:53。
BANNED_KEYWORDS_ON = [
    ("购买所有研发完毕的改装件", ["研发完毕", "选择新的研发"]),
]

# 标记词出现在帧里时，**必须**触发这个关键词按键（收窄之后弹窗没被弄丢）
REQUIRED_KEYWORDS_ON = [
    ("均研发完毕", "均研发完毕"),
]

# 这些帧属于别的界面，不许被新场景抢走
MUST_NOT_BE = [
    ("战果汇总",        "选择新的研发项"),
    ("载具研发进度",    "选择新的研发项"),
]
# 同一帧里两个场景的标记词都出现时**跳过不判**：谁赢都有理，判它没有意义。
#
# 实测 logs/scenes_20260928_175652.jsonl 17:57:30 就是这种帧。屏幕上其实是
# 「还有改装件未购入。要立刻购买吗？」这个弹窗（判 购买改装件确认，**对的**），
# 但机器人**自己打出来的日志行**也被 OCR 读回去了：
#   `[17：56：58]场景：选择新的研发项` / `[手柄］点按×（0.1s)` /
#   `[ | [17:56:58]选择新的研发项→按×退`
# 于是这一帧同时含两个标记词。这不是关键词表的问题，是自污染 —— 而且
# `common/results.py` 的 self_window_hits() 认不出它（那个表认的是 GUI/终端
# 的特征词，不认机器人自己打在控制台/HUD 上的日志行）。
AMBIGUOUS = [
    ("选择新的研发项", "还有改装件未购入"),
]


def load_frames():
    """扫所有场景 log，返回 [(日志名, t, texts), ...]。"""
    out = []
    for path in sorted(glob.glob("logs/scenes_*.jsonl")):
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


def has(texts, marker):
    return any(marker in (t or "") for t in texts)


def main():
    env = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                           errors="replace")

    def out(s=""):
        env.write(s + "\n")

    frames = load_frames()
    if not frames:
        out("logs/scenes_*.jsonl 一个都没有（logs/ 被清理过？），什么都没检查。")
        return 0

    out("扫到 %d 帧（%d 份场景 log）" % (len(frames),
                                        len(glob.glob("logs/scenes_*.jsonl"))))
    det = SceneDetector()
    bad = 0
    ran = 0

    def ambiguous(tx):
        return any(has(tx, a) and has(tx, b) for a, b in AMBIGUOUS)

    def check(marker, fn, why):
        """对每一帧含 marker 的画面跑 fn(texts, scene)，返回 (命中数, 失败数)。"""
        all_hits = [(p, t, tx) for p, t, tx in frames if has(tx, marker)]
        hits = [(p, t, tx) for p, t, tx in all_hits if not ambiguous(tx)]
        skipped = len(all_hits) - len(hits)
        fails = 0
        for p, t, tx in hits:
            scene, score, gated, scores = det._scene_from_texts(tx)
            ok, detail = fn(tx, scene, scores)
            if not ok:
                fails += 1
                if fails <= 3:
                    out("    ✗ %s %s  %s" % (t, p, detail))
        tail = "（另有 %d 帧判不了，见 AMBIGUOUS）" % skipped if skipped else ""
        out("  %-22s %3d 帧  %s%s" % (marker, len(hits),
                                     "OK" if not fails else
                                     "FAIL %d/%d" % (fails, len(hits)), tail))
        if hits:
            out("      （%s）" % why)
        return len(hits), fails

    out("\n== 新场景认得出 ==")
    for marker, want in EXPECT_SCENE:
        def f(tx, scene, scores, want=want):
            if scene == want:
                return True, ""
            return False, "判成 %r，期望 %r (scores=%s)" % (scene, want, scores)
        n, k = check(marker, f, "标记词 → 期望场景 %s" % want)
        ran += n
        bad += k

    out("\n== 关键词按键（超串回归）==")
    for marker, banned in BANNED_KEYWORDS_ON:
        def f(tx, scene, scores, banned=banned):
            acted = [kw for kw, _ in get_keyword_actions(tx)]
            hit = [k for k in banned if k in acted]
            if hit:
                return False, "误触发按键关键词 %s（会跟着 handler 一起按键）" % hit
            return True, ""
        n, k = check(marker, f, "这些词是别的按钮的子串，不许命中")
        ran += n
        bad += k

    for marker, want_kw in REQUIRED_KEYWORDS_ON:
        def f(tx, scene, scores, want_kw=want_kw):
            acted = [kw for kw, _ in get_keyword_actions(tx)]
            if want_kw in acted:
                return True, ""
            return False, "没触发 %r（收窄收过头了？实际 %s）" % (want_kw, acted)
        n, k = check(marker, f, "弹窗必须还能被关掉")
        ran += n
        bad += k

    out("\n== 别的界面没被抢走 ==")
    for marker, forbid in MUST_NOT_BE:
        def f(tx, scene, scores, forbid=forbid):
            if scene == forbid:
                return False, "被抢成了 %r (scores=%s)" % (forbid, scores)
            return True, ""
        n, k = check(marker, f, "期望不是 %s" % forbid)
        ran += n
        bad += k

    out("")
    if not ran:
        out("没有一帧命中任何标记词 —— 夹具日志被清理过？什么都没检查。")
        return 0
    out("PASS" if not bad else "FAIL: %d 处不符" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
