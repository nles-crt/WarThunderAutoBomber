# -*- coding: utf-8 -*-
"""结算画面（场景 `战果汇总`）的 OCR 解析（纯函数：无 IO、无第三方依赖）

**为什么要单独一个模块**：这份解析只能拿**真实记录下来的 OCR** 验证。抽成纯函数之后
`tools/results_check.py` 才能把 logs/ 里那一帧真画面喂进来做离线回归 —— 否则改错一个
标签名只有等下一局打完、看 JSON 少了一项才发现。

**标签表是实测出来的，不是猜的。** 来源：`logs/scenes_20260928_162128.jsonl`
（2026-09-28 16:21，一局突尼斯战役的结算画面，37 个 token、无任何自窗口污染）。
那一帧里 9 个标签**全部**满足同一条规律：

    标签后面紧跟着的那个 token 就是它的值

    10 空中目标击坠数       11 0
    12 轰炸基地（TNT当量，吨） 13 0.136
    15 摧毁基地            16 3801
    17 活跃时长            18 0:24
    20 活跃度              21 64%
    22 总计                23 169
    25 游戏时长            26 4:17
    27 任务时长            28 6:55
    29 自由研发点           30 169

同一帧里还有一排**没有标签的裸数字**：855 / 169 / 2676 / `1688条` / 721 / 5871 / 16881。
它们的标签在 OCR 顺序里不知去向（可能落在面板的另一侧）。**这些故意不解析** ——
凭空给它们安名字就是把噪声当数据。它们在 `texts` 里原样保留，谁需要谁自己去认。

**换地图/换模式时这张表要补**。表现是 `fields` 里少几项而不会报错，而且 `texts` 永远是
完整的，所以没有丢数据的风险。补之前请先在 logs/ 里找到对应的真实样本 —— 这个仓库里
所有关键词表都是先有样本才写的（见 CLAUDE.md 的场景识别一节）。
"""

# 标签片段 -> json 里的字段名。
# 用「包含」匹配而不是全等：OCR 会把全角括号和逗号读歪（样本里
# `轰炸基地（TNT当量，吨）` 就是一个整体 token，但换个分辨率可能被切成两段）。
LABELS = [
    ("空中目标击坠数", "空中目标击坠数"),
    ("轰炸基地",       "轰炸基地TNT吨"),   # 全称「轰炸基地（TNT当量，吨）」
    ("摧毁基地",       "摧毁基地"),
    ("活跃时长",       "活跃时长"),
    ("活跃度",         "活跃度"),
    ("总计",           "总计"),
    ("游戏时长",       "游戏时长"),
    ("任务时长",       "任务时长"),
    ("自由研发点",     "自由研发点"),
]

# 机器人**自己的窗口**出现在游戏画面上时会读到的词。来源：
# `logs/scenes_20260928_114345.jsonl` 里 11:43 那三帧 —— 它们同样被判成了 `战果汇总`，
# 但 texts 是「投弹视角距离 (m)」「单次投弹数量」「python main.py >」「Bash(...」
# 「All 40 assertions pass」「ctrl+o」，也就是本程序自己的 GUI 和终端窗口（连 Claude Code
# 的界面都在里面）。那三帧的 md5 互不相同，而真实结算画面那 8 帧完全相同 ——
# 终端一直在刷新，静止的结算画面不会，这是区分二者的旁证。
#
# 前 7 个是 gui.py 里 FIELDS 的界面标签，后几个是终端里会出现的字（提示符、运行命令、
# 测试输出）。名单必然会随 GUI 改版而过时，所以它是**打标记用的启发式，不是过滤器**：
# 漏标只是少一个提醒，误标也只是多一个提醒，两种都不会丢数据。
SELF_WINDOW_MARKERS = [
    "自动轰炸",        # GUI 窗口标题「War Thunder 自动轰炸」
    "控制台",
    "投弹视角距离",
    "单次投弹数量",
    "循环轰炸轮次",
    "信号翻转",
    "自动开始投弹",
    "python main.py",
    "Bash(",
    "assertions pass",
    "ctrl+",
    "fix-bomb",
]


def parse(texts):
    """把一帧结算画面的 OCR token 列表拆成 (地图串, 字段 dict)。

    值取「标签后面第一个非空 token」，并且**这个值不再参与标签匹配** ——
    否则某个字段的值恰好包含另一个标签名时会被重复计一次。

    拿不到就是拿不到：标签没出现、或它是最后一个 token，这个字段就不在 dict 里。
    调用方始终能用原始 texts 兜底，所以这里不做任何猜测性填充。
    """
    map_name = None
    for t in texts or []:
        s = (t or "").strip()
        if map_name is None and "【" in s:
            map_name = s            # 样本里就是「历史性能，【军事行动】突尼斯战役（不可重生）」
            break

    fields = {}
    ts = list(texts or [])
    n = len(ts)
    i = 0
    while i < n:
        s = (ts[i] or "").strip()
        hit = None
        for label, name in LABELS:
            if label in s:
                hit = name
                break
        if hit is not None:
            j = i + 1
            while j < n and not (ts[j] or "").strip():
                j += 1
            if j < n:
                fields[hit] = (ts[j] or "").strip()
                i = j + 1           # 跳过刚被当作值的那一个 token
                continue
        i += 1
    return map_name, fields


def self_window_hits(texts):
    """本帧里出现的「机器人自己窗口」特征词列表。空列表 = 没看出污染。

    返回的是命中的词而不是 True/False：写进 JSON 后能直接看出**是哪几个词**让它可疑，
    否则下次还得靠人肉比对。
    """
    hits = []
    for t in texts or []:
        s = (t or "")
        for m in SELF_WINDOW_MARKERS:
            if m in s and m not in hits:
                hits.append(m)
    return hits
