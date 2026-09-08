import torch
import numpy as np
import cv2


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _refine_box_center(frame, x1, y1, x2, y2):
    """修正细长检测框的目标点。

    有些战区会被 YOLO 框成“细线 + 方块”的长方形，bbox 中心会落在线上。
    这里在长框内寻找最像方块的局部高密度区域，并返回它的中心。
    """
    fh, fw = frame.shape[:2]
    x1 = int(_clamp(round(x1), 0, fw - 1))
    y1 = int(_clamp(round(y1), 0, fh - 1))
    x2 = int(_clamp(round(x2), x1 + 1, fw))
    y2 = int(_clamp(round(y2), y1 + 1, fh))

    w = x2 - x1
    h = y2 - y1
    raw_cx = int((x1 + x2) / 2)
    raw_cy = int((y1 + y2) / 2)
    short = min(w, h)
    long = max(w, h)

    if short < 8 or long / max(short, 1) < 1.8:
        return raw_cx, raw_cy

    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return raw_cx, raw_cy

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 35, 110)

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    color_mask = ((sat > 35) & (val > 45)).astype(np.uint8) * 255

    # 边缘 + 颜色密度，方块区域通常比细线有更高局部质量。
    mask = cv2.bitwise_or(edges, color_mask)
    if int(mask.sum()) == 0:
        return raw_cx, raw_cy

    if w >= h:
        win = int(_clamp(round(short * 1.25), short, w))
        scores = np.convolve(mask.sum(axis=0), np.ones(win), mode="valid")
        start = int(np.argmax(scores))
        roi = mask[:, start:start + win]
        ys, xs = np.nonzero(roi)
        if len(xs) == 0:
            return raw_cx, raw_cy
        cx = x1 + start + int(np.mean(xs))
        cy = y1 + int(np.mean(ys))
    else:
        win = int(_clamp(round(short * 1.25), short, h))
        scores = np.convolve(mask.sum(axis=1), np.ones(win), mode="valid")
        start = int(np.argmax(scores))
        roi = mask[start:start + win, :]
        ys, xs = np.nonzero(roi)
        if len(xs) == 0:
            return raw_cx, raw_cy
        cx = x1 + int(np.mean(xs))
        cy = y1 + start + int(np.mean(ys))

    return int(_clamp(cx, x1, x2 - 1)), int(_clamp(cy, y1, y2 - 1))


class YOLO:
    def __init__(self):
        self.model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.conf = 0.5
        self.iou = 0.45

    def load(self, path):
        from ultralytics import YOLO as Y

        self.model = Y(path)
        dummy = np.zeros((640, 640, 3), dtype=np.uint8)
        self.model(dummy, device=self.device, verbose=False)

    def run(self, frame):
        if self.model is None:
            return [], frame
        r = self.model(frame, device=self.device, conf=self.conf, iou=self.iou, imgsz=640, verbose=False)
        if not r:
            return [], frame
        r = r[0]
        dets = []
        for b in r.boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            box_cx = int((x1 + x2) / 2)
            box_cy = int((y1 + y2) / 2)
            cx, cy = _refine_box_center(frame, x1, y1, x2, y2)
            dets.append(
                {
                    "cls": r.names.get(int(b.cls[0]), "?"),
                    "conf": float(b.conf[0]),
                    "cx": cx,
                    "cy": cy,
                    "box_cx": box_cx,
                    "box_cy": box_cy,
                    "w": int(x2 - x1),
                    "h": int(y2 - y1),
                }
            )
        return dets, frame
