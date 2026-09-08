"""场景识别模块 — 全屏 OCR

检测逻辑:
  1. 全屏 OCR
  2. 关键词匹配 → 场景分类
  3. 如果 OCR 没结果 + HUD 颜色明显 → 判为战斗中
  4. 场景消抖（连续 3 帧相同才切换）
"""

import time
import re
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

_SCENES = [
    ("加载中",      ["加载中", "正在加载", "载入中", "正在读取", "无法连接", "重新连接"], 1),
    ("战斗中",      ["km/h", "m/s", "表速", "高度", "速度"],     1),
    ("选择载具",    ["历史性能", "标准涂装", "加入游戏", "加入战斗", "选择载具"], 1),
    ("等待中",      ["等待游戏开始"],                               1),
    ("战果汇总",    ["战果汇总", "载具研发", "个人得分"],             1),
    ("大厅",        ["战斗", "社区", "商店", "科技树", "战役"], 2),
    ("无重生基地",  ["没有可用的重生基地"],                          1),
    ("观战模式",    ["观战模式"],                                   1),
    ("退出中",      ["返回基地", "确认退出"],                       1),
    ("机场补给",    ["已验收作战物资", "正在装填"],                  1),
]

SCENE_BUTTONS = {
    "大厅":         "X",
    "战果汇总":     "X",
    "无重生基地":   "X",
    "观战模式":     "X",
    "机场补给":     "A",
}

KEYWORD_BUTTONS = {
    "返回基地":          "X",
    "选择新的研发":      "X",
    "请选择符合要求":    "B",
    "已验收作战物资":    "A",
    "正在装填":          "A",
    "新贴花可用":        "A",
    "加入战斗":          "X",
    "研发完毕":          "B",
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


def _extract_bomb_count(texts):
    """从 OCR 文本中提取炸弹存量

    输入: ["炸弹  32/36", "火箭弹 0/36"]
    返回: (剩余, 总量) 或 None
    """
    if not texts:
        return None
    for t in texts:
        if "炸弹" in t:
            nums = re.findall(r"\d+", t)
            if len(nums) >= 2:
                return int(nums[0]), int(nums[1])
            elif len(nums) == 1:
                return int(nums[0]), None
    return None



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
      - 炸弹存量跟踪
    """

    def __init__(self, log=None, stabilize_frames=3):
        self.log = log or (lambda s: None)
        self.stabilize = stabilize_frames
        self.current_scene = "未知"
        self.last_texts = []
        self._scene_hits = {}
        self._bomb_info = None
        self._total_frames = 0
        self._ocr_frames = 0

    def _scene_from_texts(self, texts):
        """OCR 文字 -> 最佳匹配场景"""
        if not texts:
            return "未知", 0
        best_scene = "未知"
        best_score = 0
        for scene, keywords, min_match in _SCENES:
            score = 0
            for kw in keywords:
                if any(kw in t for t in texts):
                    score += 1
            if score >= min_match and score > best_score:
                best_score = score
                best_scene = scene
        return best_scene, best_score

    def scan(self, frame, force_full_ocr=False):
        """扫描一帧，返回 (场景名称, OCR 文字列表)"""
        if frame is None:
            return self.current_scene, self.last_texts

        self._total_frames += 1

        # ========== 全屏 OCR ==========
        texts = _ocr_region(frame, 0, 1.0, 0, 1.0)

        # ========== 关键词匹配 ==========
        scene, score = self._scene_from_texts(texts)

        # ========== 兜底: HUD 颜色检测 ==========
        if scene == "未知":
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            if _has_multiplayer_hud(hsv):
                hud_score = _hud_color_score(hsv)
                if hud_score > 0.10:
                    scene = "战斗中"

        # ========== 炸弹信息 ==========
        if scene == "战斗中":
            bomb_info = _extract_bomb_count(texts)
            if bomb_info:
                self._bomb_info = bomb_info

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
            self._ocr_frames += 1
            return new_scene
        return self.current_scene

    def get_bomb_info(self):
        return self._bomb_info

    def has_bombs(self):
        if self._bomb_info is None:
            return True
        return self._bomb_info[0] > 0

    def get_bombs_remaining(self):
        if self._bomb_info is None:
            return None
        return self._bomb_info[0]


# ============================================================
# 兼容旧接口
# ============================================================

_detector = None


def _get_detector(log=None):
    global _detector
    if _detector is None:
        _detector = SceneDetector(log=log)
    return _detector


def detect_state(frame, log=None, force_full_ocr=False):
    """兼容旧的调用方式: scene, texts = detect_state(frame, log=log)"""
    det = _get_detector(log)
    return det.scan(frame, force_full_ocr=force_full_ocr)
