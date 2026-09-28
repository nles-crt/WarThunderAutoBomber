"""场景识别模块 — 全屏 OCR

检测逻辑:
  1. 全屏 OCR
  2. 关键词匹配 → 场景分类
  3. 如果 OCR 没结果 + HUD 颜色明显 → 判为战斗中
  4. 场景消抖（连续 3 帧相同才切换）
"""

import time
import cv2
import numpy as np

try:
    from rapidocr_onnxruntime import RapidOCR
    _ocr = RapidOCR()
    OCR_OK = True
except Exception:
    _ocr = None
    OCR_OK = False


# ============================================================
# 场景定义
# ============================================================

# 战斗 HUD 的锚点词。`战斗中` 的松词（km/h / m/s / 表速 / 高度 / 速度）在机库的
# 载具数据卡上满地都是 —— 实测一帧机库 OCR 里有 "最大速度：417 km/h"、
# "指示空速上限 480 km/h"、"最大带起落架速度(表速)：300 km/h"，松词能拿满 4 分。
# 只靠松词判定，机器人在机库里就会开始投弹（拉弹舱、进投弹视角）。
# 而下面这些词只在真正飞行时的 HUD 上出现（见 logs/20260928_09*.log 的真实采样）：
# 节流阀 / 燃油量 / 油温1 / 水温1 / 海拔高度 / 雷达高度 / 8/8[弹舱]。
# 所以给 战斗中 加一道门：必须命中锚点词里的至少一个才算数。松词保留，
# 仍然参与和其它场景的比分仲裁。
_COMBAT_ANCHORS = ["节流阀", "燃油量", "海拔高度", "雷达高度", "油温", "水温", "弹舱"]

# 场景表：(名称, 关键词, min_match[, 必需命中集])
#
# 第 4 项是**门控**：命中少于 min_match 分词，或者门控词一个都没命中，该场景本次作废
# （分数仍记进 scores 供分析）。省略则没有门控，行为与旧版一致。
#
# 顺序有意义：_scene_from_texts 里同分先到先得，所以 `机组锁定` 必须排在 `选择载具`
# 前面 —— 机组锁定时这两个界面长得像，会同时命中。
_SCENES = [
    ("加载中",      ["加载中", "正在加载", "载入中", "正在读取", "无法连接", "重新连接"], 1),
    ("战斗中",      ["km/h", "m/s", "表速", "高度", "速度"] + _COMBAT_ANCHORS,
     1, _COMBAT_ANCHORS),
    # 飞机拉断坠毁后机组被锁，界面停在「选择可用的飞机」。刻意**不用** "没有可用的"
    # 作关键词：它是 `无重生基地` 那条 "没有可用的重生基地" 的子串，会串味。
    ("机组锁定",    ["选择可用的飞机", "可用的飞机", "可用的坦克", "机组锁定", "乘员锁定"], 1),
    # min_match=2：机库的 "空战·历史性能（3.0权重）" 会让它白拿 1 分（关键词里有
    # "历史性能"），而 1 分就成立的话机库里也会按 X。要两个词才算数。
    ("选择载具",    ["历史性能", "标准涂装", "加入游戏", "加入战斗", "选择载具"], 2),
    # 「开始任务前必须先点选一台可供出击的载具。」——在**不可重生**地图上阵亡后，
    # 按结果浮层的「加入战斗！」就会弹这个框，框里只有一个「确定」。
    # 原文见 logs/scenes_20260928_140707.jsonl（37 轮扫描都是这两行文字）。
    # 这两个词真·选择载具界面上一个都没有，不会串味；按键由 app.py 的
    # `_handle_needs_vehicle` 控频，不进 SCENE_BUTTONS。
    ("需选载具",    ["开始任务前必须先点选", "可供出击的载具"],        1),
    # 排队等匹配。**实测出来的真关键词是 `等待时间`**：2450 帧日志里它出现 235 次，
    # 每一次都是排队面板打开时的画面（浮层盖在机库上，所以 raw 仍常判成 大厅）——
    # 原文见 logs/scenes_20260928_175652.jsonl 18:05:27 / 18:06:24：
    #   「等待时间 | 0:00→0:29 | 当前排队状况：40→28 | 取消」，
    # 其中 `取消` 就是面板上的**取消匹配按钮**。
    #
    # 下面那四个词是**猜的，至今一次都没真正命中过**（`等待游戏开始`/`正在搜索战斗`/
    # `排队中` 0 帧，`正在等待游戏` 只命中过 1 帧 —— 还是机器人自己的窗口造成的假阳性）。
    # 留着是版式变化时的兜底，不是判据。之前没人发现，是因为排队时浮层被读成 大厅、
    # 而 `_handle_lobby` 里那个「有取消就按 B」的分支把手指按在了面板的取消键上。
    #
    # 这个词是**命中即胜**（匹配中 在 _DECISIVE 里），因为按分数它永远赢不了：
    # 机库词随便就是 3~4 分，而这里只有 1 分。
    ("匹配中",      ["等待时间", "正在等待游戏", "等待游戏开始",
                     "正在搜索战斗", "排队中"],                       1),
    # 阵亡后的结果浮层（任务进行中 / 初步战果 / 完整收益将在战斗结束后自动结算 /
    # 前往基地 / 加入战斗！）同样命中 `选择载具` 的两个词（`历史性能` + `加入战斗`），
    # 2 分压过这里原来的 1 分，于是浮层被判成"选择载具"，而
    # `_handle_select_vehicle` 按的 X 正好落在「加入战斗！」上 —— 弹出「需选载具」，
    # 整条链见 logs/scenes_20260928_140707.jsonl idx78-80。
    # 补的这三个词都是浮层独有、真·选择载具界面上没有的（对照同一份 log 里
    # idx78 的浮层全文和 idx 里的真界面全文）。加了之后浮层得 4 分，判对。
    ("战果汇总",    ["战果汇总", "载具研发", "个人得分",
                     "任务进行中", "初步战果", "完整收益"],          1),
    # 结算之后弹出来的改装件界面（「选择新的研发项」）。原文见
    # logs/scenes_20260928_131739.jsonl 13:17:53 / logs/scenes_20260928_114629.jsonl 11:46:37。
    #
    # 只挑该界面**独有**的串。刻意不用 `飞行性能` / `生存能力` / `武器配置`
    # （它们是改装件分类页签，机库的改装页也会有），更不用 `研发完毕` ——
    # 那个词是本界面按钮 `购买所有研发完毕的改装件（1100条）` 的子串，
    # 正是下面 KEYWORD_BUTTONS 里那个误按 B 的根源。
    #
    # 串味检查：结算界面（114629.jsonl 11:46:31）上也有 `载具研发进度`、
    # `改装件研发进度`、`研发载具`、`研发改装件` —— 但都没有"整体"二字，
    # 所以 `改装件整体研发进度` 不会把结算界面抢过来。
    ("选择新的研发项", ["选择新的研发项", "改装件整体研发进度",
                        "购买所有研发完毕的改装件", "分配研发点"],     1),
    # 「Ju88A-1还有改装件未购入。要立刻购买吗？这将花费1520象？」的是/否弹窗
    # （logs/scenes_20260928_163436.jsonl 16:34:47）。按键由 app.py 的
    # `_handle_buy_prompt` 走节流阀按 B（取消）—— 机器人不替玩家花银狮。
    # 这个界面**不能**改按 X：X 在这个弹窗上是「确认购买」，会真的花掉钱。
    # 不拿 `是` / `否` 当关键词：样本里 `否` 就被 OCR 读成了 `香`。
    ("购买改装件确认", ["还有改装件未购入", "要立刻购买吗"],           1),
    ("大厅",        ["战斗", "社区", "商店", "科技树", "战役"], 2),
    ("无重生基地",  ["没有可用的重生基地"],                          1),
    ("观战模式",    ["观战模式"],                                   1),
    ("退出中",      ["返回基地", "确认退出"],                       1),
    ("机场补给",    ["已验收作战物资", "正在装填"],                  1),
]

# 场景名 -> min_match，供 _scene_from_texts 做优先级仲裁
_SCENE_MIN_MATCH = {e[0]: e[2] for e in _SCENES}

# 命中即胜、不看分数的场景。
#
# 排队浮层是**盖在机库上面**的：同一帧里 社区/商店/科技树 照样读得到，大厅能拿 3 分
# 而排队只有 1 分。按分数比大小永远是 大厅 赢 —— 而机器人一旦以为在大厅，
# _handle_lobby 等 30 秒就会按 B（取消）把正在进行的匹配掐掉。
# 所以这两个场景只看"有没有",不看"得几分"。它们的词都足够专有，
# 不会在别的界面上出现，直接判胜是安全的。
_DECISIVE = {"匹配中", "机组锁定"}

# 场景 -> 每轮扫描都要按的键。**只有"按多少次都安全"的键才配进这张表。**
#
# `匹配中` / `机组锁定` / `大厅` / `战果汇总` / `需选载具` **故意都不在表里**，
# 它们的按键全部由 app.py 的 handler 走 `_press_x_throttled()`（30 秒最多一次）：
#   - 排队时任何按键都会把正在进行的匹配取消掉；
#   - 机组锁定时反复按 B 是按键死循环 —— B 正是那个界面要的键，场景永远不变，
#     按一次就够了（handler 用 `_crew_lock_cancelled` 守）；
#   - 大厅里每 3 秒按一次 X：实测 logs/scenes_20260928_131739.jsonl
#     从 13:34:59 一路按到 13:48:02，260 多次 X，一次战斗都没进；
#   - 结算/结果界面上的 X **不保证是"前进"**。在阵亡后的结果浮层上它正好是
#     「加入战斗！」——按下去就弹出「需选载具」提示，每 3 秒重开一次
#     （logs/scenes_20260928_140707.jsonl idx79→80 起，37 轮）。
SCENE_BUTTONS = {
    "无重生基地":   "X",
    "观战模式":     "X",
    "机场补给":     "A",
}

KEYWORD_BUTTONS = {
    "返回基地":          "X",
    # `选择新的研发` 原本在这里 -> X。删掉了：那个界面现在有自己的场景和
    # handler（按 B 返回机库），留着会变成 X 和 B 同时按 —— 实测
    # logs/scenes_20260928_131739.jsonl 13:17:53 那一帧的 actions 就是
    # ['tap:X', 'tap:B', 'tap:A', 'tap:X(选择新的研发)', 'tap:B(研发完毕)']，
    # 两个方向相反的键一起按。机器人当时能退出这个界面纯属误触发。
    "请选择符合要求":    "B",
    "已验收作战物资":    "A",
    "正在装填":          "A",
    "新贴花可用":        "A",
    "加入战斗":          "X",
    # 关掉「该等级的所有改装件均研发完毕！」弹窗。**必须带"均"字**：
    # 改装件界面上的按钮 `购买所有研发完毕的改装件（1100条）` 是 `研发完毕`
    # 的超串，只写 `研发完毕` 就会在那个界面上每轮扫描误按一次 B。
    # 两份弹窗样本（114629.jsonl 11:46:34 / 11:46:43）都含 `均研发完毕`。
    "均研发完毕":        "B",
}


# ============================================================
# 工具函数
# ============================================================

def get_scene_button(scene):
    return SCENE_BUTTONS.get(scene)


def get_keyword_actions(texts):
    if not texts:
        return []
    result = []
    for kw, btn in KEYWORD_BUTTONS.items():
        if any(kw in t for t in texts):
            result.append((kw, btn))
    return result


# ============================================================
# 颜色辅助
# ============================================================

_HUD_WHITE_LOWER = np.array([0, 0, 180], dtype=np.uint8)
_HUD_WHITE_UPPER = np.array([180, 30, 255], dtype=np.uint8)


def _hud_color_score(hsv):
    """检测画面中是否有 HUD 风格的颜色（白色/浅色文字）
    返回 0~1 分数，越高越像游戏 HUD 界面
    """
    if hsv is None or hsv.size == 0:
        return 0.0
    h, w = hsv.shape[:2]
    top_region = hsv[:int(h * 0.15), :, :]
    bot_region = hsv[int(h * 0.85):, :, :]
    score = 0.0
    for region in (top_region, bot_region):
        if region.size == 0:
            continue
        white = cv2.inRange(region, _HUD_WHITE_LOWER, _HUD_WHITE_UPPER)
        white_ratio = cv2.countNonZero(white) / max(region.size // 3, 1)
        if 0.005 < white_ratio < 0.25:
            score += white_ratio * 2.0
    return min(score, 1.0)


# ============================================================
# 区域 OCR
# ============================================================

def _ocr_region(frame, y_start, y_end, x_start=0, x_end=1.0):
    """对帧的指定区域做 OCR

    y_start, y_end: 垂直比例 (0~1)
    x_start, x_end: 水平比例 (0~1)
    返回识别到的文字列表
    """
    if not OCR_OK or frame is None:
        return []
    h, w = frame.shape[:2]
    y1 = int(h * y_start)
    y2 = int(h * y_end)
    x1 = int(w * x_start)
    x2 = int(w * x_end)
    if y2 <= y1 or x2 <= x1:
        return []
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return []
    texts = []
    try:
        result, _ = _ocr(roi)
        if result:
            for _, text, score in result:
                try:
                    if score and float(score) > 0.4:
                        texts.append(text)
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    return texts


def _has_multiplayer_hud(hsv):
    """检测多人模式 HUD（顶部有白色文字块）"""
    if hsv is None or hsv.size == 0:
        return False
    h, w = hsv.shape[:2]
    top = hsv[:int(h * 0.10), :, :]
    if top.size == 0:
        return False
    white = cv2.inRange(top, _HUD_WHITE_LOWER, _HUD_WHITE_UPPER)
    white_pixels = cv2.countNonZero(white)
    total_pixels = top.shape[0] * top.shape[1]
    ratio = white_pixels / max(total_pixels, 1)
    return ratio > 0.003


# ============================================================
# 场景检测器（有状态）
# ============================================================

class SceneDetector:
    """场景检测器

    特点:
      - 全屏 OCR + 关键词匹配
      - HUD 颜色兜底
      - 场景消抖（连续稳定帧数）
    """

    def __init__(self, log=None, stabilize_frames=3):
        self.log = log or (lambda s: None)
        self.stabilize = stabilize_frames
        self.current_scene = "未知"
        self.last_texts = []
        # 判定细节，供 app 写进 JSONL 场景 log 和排查用
        self.last_scores = {}      # 本次各场景得分
        self.last_raw = "未知"     # 消抖**之前**的判定
        self.last_gated = []       # 本来会赢、但被门控拦下的场景
        self.last_ocr_ms = 0       # 本帧整屏 OCR 耗时
        self._scene_hits = {}
        self._total_frames = 0

    def _scene_from_texts(self, texts):
        """OCR 文字 -> (最佳场景, 得分, 被门控拦下的场景, 全部得分)"""
        if not texts:
            return "未知", 0, [], {}
        best_scene = "未知"
        best_score = 0
        scores = {}
        gated_out = []
        for entry in _SCENES:
            scene, keywords, min_match = entry[0], entry[1], entry[2]
            gate = entry[3] if len(entry) > 3 else None

            score = 0
            for kw in keywords:
                if any(kw in t for t in texts):
                    score += 1
            scores[scene] = score

            if score < min_match:
                continue
            # 门控：锚点词一个都没命中 → 本次作废。分数留在 scores 里，
            # 并由 gated_out 记下"本来会赢但被拦住"，场景 log 里直接可查。
            if gate and not any(any(kw in t for t in texts) for kw in gate):
                gated_out.append(scene)
                continue
            if score > best_score:
                best_score = score
                best_scene = scene

        # 命中即胜的场景优先，不看分数（理由见 _DECISIVE 的说明）。
        for scene in _DECISIVE:
            if scene in gated_out:
                continue
            if scores.get(scene, 0) >= _SCENE_MIN_MATCH.get(scene, 1):
                return scene, scores[scene], gated_out, scores

        # 机库的载具详情页会同时命中两边：
        #   战斗中 ← "最大速度：417 km/h"、"最大带起落架速度(表速)：300 km/h"
        #   大厅   ← "社区"/"商店"/"科技树"
        # 门控已经把上面那种机库帧挡在 战斗中 门外了，这条兜底留着防止
        # 门控词没覆盖到的新 HUD 变体（比如某架飞机不显示 节流阀）。
        # "社区/商店/科技树" 在战斗 HUD 里不会出现，反过来 km/h、速度 在机库里
        # 到处都是 —— 所以 战斗中 胜出而 大厅 也够分时，一律按大厅算。
        if best_scene == "战斗中" and scores.get("大厅", 0) >= _SCENE_MIN_MATCH["大厅"]:
            return "大厅", scores["大厅"], gated_out, scores

        return best_scene, best_score, gated_out, scores

    def scan(self, frame):
        """扫描一帧，返回 (场景名称, OCR 文字列表)"""
        if frame is None:
            return self.current_scene, self.last_texts

        self._total_frames += 1

        # ========== 全屏 OCR ==========
        # 计时：整屏 OCR 是扫描周期的主要成本，app 要用它把周期补齐到设定值，
        # 场景 log 里也记一份，方便判断 3 秒周期到底达不达得到。
        _t0 = time.monotonic()
        texts = _ocr_region(frame, 0, 1.0, 0, 1.0)
        self.last_ocr_ms = int((time.monotonic() - _t0) * 1000)

        # ========== 关键词匹配 ==========
        scene, score, gated, scores = self._scene_from_texts(texts)
        self.last_raw = scene
        self.last_gated = gated
        self.last_scores = scores

        # ========== 兜底: HUD 颜色检测 ==========
        if scene == "未知":
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            if _has_multiplayer_hud(hsv):
                hud_score = _hud_color_score(hsv)
                if hud_score > 0.10:
                    scene = "战斗中"
                    self.last_raw = "战斗中"
                    self.last_gated = []

        # ========== 场景消抖 ==========
        scene = self._stabilize_scene(scene)

        self.last_texts = texts
        return scene, texts

    def _stabilize_scene(self, new_scene):
        """场景消抖：连续 N 帧相同才切换"""
        if new_scene == self.current_scene:
            for s in list(self._scene_hits.keys()):
                if s != new_scene:
                    self._scene_hits.pop(s, None)
            self._scene_hits[new_scene] = self._scene_hits.get(new_scene, 0) + 1
            return self.current_scene
        self._scene_hits[new_scene] = self._scene_hits.get(new_scene, 0) + 1
        if self._scene_hits[new_scene] >= self.stabilize:
            self._scene_hits.clear()
            self.current_scene = new_scene
            self._log_decision(new_scene)
            return new_scene
        return self.current_scene

    def _log_decision(self, new_scene):
        """切换场景时打一行判定依据。

        只有分数排名看不出"为什么是它"，把被门控拦下的场景也标出来，
        才能一眼分清是"识别错了"还是"识别对了但在消抖"。
        """
        detail = " ".join(
            f"{s}={n}" + ("-门控" if s in self.last_gated else "")
            for s, n in sorted(self.last_scores.items(), key=lambda kv: -kv[1])
            if n > 0
        )
        self.log(f"场景: {new_scene}  [{detail or '无命中词'}]"
                 f" raw={self.last_raw} {self.last_ocr_ms}ms")


# ============================================================
# 兼容旧接口
# ============================================================

_detector = None


def _get_detector(log=None):
    global _detector
    if _detector is None:
        _detector = SceneDetector(log=log)
    return _detector


def detect_state(frame, log=None):
    """兼容旧的调用方式: scene, texts = detect_state(frame, log=log)

    注意这条路径用的是模块级单例（`stabilize_frames` 默认 3），app.py 走的是
    自己 new 的 SceneDetector，两者状态互不相干。
    """
    det = _get_detector(log)
    return det.scan(frame)
