#!/usr/bin/env python
"""OCR 加速离线对比台 — PP-OCRv6 vs 现役 ch_PP-OCRv4

为什么有这个文件
----------------
目标是把场景扫描周期从 3.0s 压到 1.5s，而整屏 OCR（真帧中位 734ms）是唯一的成本大头，
其中 det 218ms / cls 16ms / rec 251ms / 其余 93ms。这里离线对比 det 和 rec 的替换方案，
**既量速度也量关键串准确率**。

为什么必须逐串量准确率，而不是看平均分：v6 的归一化和现役不同，喂错了的时候
`节流阀` / `燃油量` / `匹配中` 这些都还是对的，只有 `8/8[弹舱]` 变成 `8/8[B单舱】`
—— 静默的部分损坏，和之前修掉的 千→干 是同一类 bug。平均分完全看不出来。

这个脚本**不改任何产品代码**，也不新增依赖（用 stdlib 的 urllib 下载）。

用法
----
    python tools/ocr_bench.py selftest           # 自检，不需要游戏
    python tools/ocr_bench.py capture            # 抓帧（需要游戏在前台、停在战斗中）
    python tools/ocr_bench.py bench              # 对已抓的帧跑对比（出数字和判定）
    python tools/ocr_bench.py dump               # 逐帧打印每个变体读到的**原文**
    python tools/ocr_bench.py all                # capture + bench
    python tools/ocr_bench.py bench --quick      # 跳过 small rec 和降采样档，用于迭代

产物
----
    tools/models/            下载的 onnx + 从 yml 生成的词表
    frames/<时间戳>/         抓到的帧（可离线重放，精度对比可重复）
    frames/<时间戳>/bench.json  对比结果

`tools/models/` 和 `frames/` 都已在 .gitignore 里。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request

import cv2
import numpy as np
import yaml

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS_DIR)
MODELS_DIR = os.path.join(TOOLS_DIR, "models")
FRAMES_DIR = os.path.join(ROOT, "frames")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import rapidocr_onnxruntime as _rapidocr  # noqa: E402
from rapidocr_onnxruntime import RapidOCR  # noqa: E402
from rapidocr_onnxruntime.ch_ppocr_det.text_detect import TextDetector  # noqa: E402
from rapidocr_onnxruntime.ch_ppocr_rec.text_recognize import TextRecognizer  # noqa: E402

from detect.scene import _COMBAT_ANCHORS  # noqa: E402
from common.window import capture, find_windows, get_rect  # noqa: E402

# 现役 rapidocr 的配置文件。拿它当所有变体的基线，而不是在这里硬抄一份参数 ——
# 用户改了 rapidocr 的 config.yaml 时，bench 要跟着变，否则基线就不是生产了。
RAPIDOCR_CFG = os.path.join(os.path.dirname(_rapidocr.__file__), "config.yaml")
RAPIDOCR_MODELS = os.path.join(os.path.dirname(_rapidocr.__file__), "models")

# ---------------------------------------------------------------- 关键串

# 这些串丢了就判定该变体不合格。分三类，理由不同：
#   anchor: `战斗中` 的门控词，漏一个就可能整局判不出在战斗（detect/scene.py:36）
#   scene : 场景分类的判别词，认错会按错键（大厅按 X、机组锁定按 B）
#   bomb  : 战斗 HUD 的弹舱/武器串 —— 弹舱 是 战斗中 的锚点词，炸弹 决定要不要拉弹舱杆
CRITICAL = {
    "anchor": list(_COMBAT_ANCHORS),
    "scene": ["大厅", "匹配中", "机组锁定", "加载中", "战果汇总", "观战模式", "机场补给"],
    "bomb": ["弹舱", "炸弹", "已选武器"],
}

# 期望出现在真帧上的具体串。真帧上没读到是正常的（HUD 不显示就没有），
# 所以这里只用来在报告里提示"基线有、候选没有"，不单独当判据。
WATCH_STRINGS = ["弹舱", "节流阀", "燃油量", "海拔高度", "雷达高度",
                 "油温", "水温", "已选武器", "km/h"]

# ---------------------------------------------------------------- 模型仓库
#
# 注意：用户最初点名的 `PaddlePaddle/PP-OCRv6_tiny_rec` **不是 ONNX**，里面只有
# inference.json + inference.pdiparams，用它就得装 paddlepaddle。官方有 ONNX 版
# 兄弟仓库，下面用的都是 _onnx 那几个。

HF = "https://huggingface.co/{repo}/resolve/main/{name}"
MODELS = {
    "v6_tiny_rec":  ("PaddlePaddle/PP-OCRv6_tiny_rec_onnx",  "inference.onnx", 4462639),
    "v6_tiny_rec_y": ("PaddlePaddle/PP-OCRv6_tiny_rec_onnx", "inference.yml", 55571),
    "v6_small_rec": ("PaddlePaddle/PP-OCRv6_small_rec_onnx", "inference.onnx", 21159378),
    "v6_small_rec_y": ("PaddlePaddle/PP-OCRv6_small_rec_onnx", "inference.yml", 150579),
    "v6_tiny_det":  ("PaddlePaddle/PP-OCRv6_tiny_det_onnx",  "inference.onnx", 1780590),
    "v6_tiny_det_y": ("PaddlePaddle/PP-OCRv6_tiny_det_onnx", "inference.yml", 883),
}

# 后缀，用于把 v6_tiny_rec / v6_small_rec 映射到各自的 yml 和词表
YML_SUFFIX = {"v6_tiny_rec": "v6_tiny_rec_y", "v6_small_rec": "v6_small_rec_y",
              "v6_tiny_det": "v6_tiny_det_y"}


def log(msg=""):
    print(msg, flush=True)


def die(msg):
    log("错误: " + msg)
    raise SystemExit(1)


# ---------------------------------------------------------------- 下载


def hf_download(key, force=False):
    """下到 tools/models/。用 urllib，本机证书链正常（curl 反而会因为
    CRYPT_E_REVOCATION_OFFLINE 失败，要用 --ssl-no-revoke 才行）。"""
    repo, name, want = MODELS[key]
    dest = os.path.join(MODELS_DIR, key + os.path.splitext(name)[1])
    if os.path.exists(dest) and not force:
        got = os.path.getsize(dest)
        if got == want:
            return dest
        log("  尺寸不符，重下: %s (%d != %d)" % (key, got, want))
    os.makedirs(MODELS_DIR, exist_ok=True)
    url = HF.format(repo=repo, name=name)
    log("  下载 %-16s %s" % (key, url))
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, dest)
    got = os.path.getsize(dest)
    if got != want:
        # 不硬失败：参考尺寸只是用来发现"下到一半/下错文件"，真正的守卫是
        # 后面 len(dict)+2 == 输出维度 那条断言。
        log("  警告: %s 尺寸 %d，与记录值 %d 不符" % (key, got, want))
    return dest


def ensure_models(which, force=False):
    need = set()
    for k in which:
        need.add(k)
        if k in YML_SUFFIX:
            need.add(YML_SUFFIX[k])
    log("准备模型 (tools/models/)")
    return {k: hf_download(k, force) for k in sorted(need)}


# ---------------------------------------------------------------- 词表


def read_yml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def rec_dict_from_yml(yml, path):
    """从 inference.yml 取 rec 词表。

    **必须用 YAML 解析，不能用正则。** 我用正则抓过一版，少了一项（6903 vs 真值
    6904），少的那项正好落在 ASCII→CJK 交界处，于是所有中文整体错位一格、
    而 `8/8[` 依然正确 —— 症状极具迷惑性。
    """
    d = (yml.get("PostProcess") or {}).get("character_dict")
    if not d:
        die("%s 里没有 PostProcess.character_dict" % path)
    return list(d)


def write_keys(chars, dest):
    """写成 rapidocr 的 CTCLabelDecode 想要的"一行一个字符"格式，UTF-8 无 BOM。

    rapidocr 会自己在这份表前后插 blank(0) 和空格(末尾)，所以这里不能自己插，
    插了就会多两位、和模型输出维度对不上。
    """
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        for c in chars:
            f.write(str(c) + "\n")
    return dest


# ---------------------------------------------------------------- 组件构造


def _abs_model(cfg_val):
    if os.path.isabs(cfg_val):
        return cfg_val
    return os.path.join(os.path.dirname(_rapidocr.__file__), cfg_val)


def base_config():
    with open(RAPIDOCR_CFG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class PlainNormRecognizer(TextRecognizer):
    """把归一化从 `(x/255-0.5)/0.5` 换成纯 `x/255`。

    PP-OCRv6 的 rec 导出需要纯 /255（见 README / 计划）。而 rapidocr 的
    `resize_norm_img` 把 0.5/0.5 写死在方法体里，没有可覆盖的钩子，只能整个
    覆盖。下面是 upstream 那个方法的逐行拷贝，只改最后两行 —— 保留零填充，
    因为居中空间里的 0 在纯 /255 空间里应该是 0（黑）而不是 0.5。
    """

    def __init__(self, config, mode="plain"):
        super().__init__(config)
        self.norm_mode = mode

    def resize_norm_img(self, img, max_wh_ratio):
        ch, ih, iw = self.rec_image_shape
        assert ch == img.shape[2]
        iw = int(ih * max_wh_ratio)
        h, w = img.shape[:2]
        ratio = w / float(h)
        resized_w = iw if math.ceil(ih * ratio) > iw else int(math.ceil(ih * ratio))
        resized = cv2.resize(img, (resized_w, ih)).astype("float32")
        resized = resized.transpose((2, 0, 1)) / 255
        if self.norm_mode == "centered":
            resized -= 0.5
            resized /= 0.5
        pad = np.zeros((ch, ih, iw), dtype=np.float32)
        pad[:, :, 0:resized_w] = resized
        return pad


def make_det(base, model_path, limit_type=None, limit_side_len=None,
             mean=None, std=None, thresh=None, box_thresh=None,
             unclip_ratio=None, max_candidates=None):
    cfg = dict(base["Det"])
    cfg["model_path"] = model_path
    if limit_type is not None:
        cfg["limit_type"] = limit_type
    if limit_side_len is not None:
        cfg["limit_side_len"] = limit_side_len
    if mean is not None:
        cfg["mean"] = mean
    if std is not None:
        cfg["std"] = std
    if thresh is not None:
        cfg["thresh"] = thresh
    if box_thresh is not None:
        cfg["box_thresh"] = box_thresh
    if unclip_ratio is not None:
        cfg["unclip_ratio"] = unclip_ratio
    if max_candidates is not None:
        cfg["max_candidates"] = max_candidates
    return TextDetector(cfg)


def make_rec(base, model_path, keys_path=None, norm="centered", img_w=None):
    """img_w 覆盖 `rec_img_shape` 的宽度，也就是**每批填充宽度的下限**。

    rapidocr 的 `TextRecognizer.__call__` 里 `max_wh_ratio = imgW / imgH` 是每批
    宽高比的**起点**，实际取 `max(起点, 该批最宽那张)`。所以只要真实 crop 比这个
    起点窄，整批就按起点宽度算 —— 生产是 320/48 = **6.67**，而真帧 HUD 上 crop 的
    宽高比中位 2.19、最大 3.81，等于每张都白算了一倍多的零填充。

    调小它是安全的：`max()` 保证真实宽高比更宽时仍以真实值为准，所以下限只影响
    "没用到的那部分填充"，不会压扁任何一张比下限宽的图。**但不能低于真实最大宽高比**
    —— 低于时 `resize_norm_img` 会按 `img_width` 截断（丢宽高比地压扁）。
    192/48 = 4.0 覆盖实测的 3.81。
    """
    cfg = dict(base["Rec"])
    cfg["model_path"] = model_path
    if keys_path:
        cfg["rec_keys_path"] = keys_path
    if img_w is not None:
        cfg["rec_img_shape"] = [3, 48, img_w]
    cls = TextRecognizer if norm == "centered" else PlainNormRecognizer
    return cls(cfg) if norm == "centered" else cls(cfg, mode="plain")


# ---------------------------------------------------------------- 流水线
#
# 逐步复用 RapidOCR 自己的方法（load_img / preprocess / maybe_add_letterbox /
# auto_text_det / get_crop_img_list / text_cls / text_rec），只把 det/rec 组件换成
# 变体。这样基线就是生产保真的，而且能分出每段的耗时。
#
# 唯一的"自己写"的部分是这 8 行顺序。`selftest` 会拿它和 RapidOCR.__call__ 对拍，
# 保证没有偏离 upstream。


# 战斗中 HUD 的文字块（左上）。**真帧实测**：20/20 帧的关键串（节流阀/燃油量/
# 海拔高度/雷达高度/弹舱/炸弹/油温/水温/速度）全部落在这个矩形内，一个不丢。
# 注意这只覆盖"战斗中"—— 机库/匹配中/加载中的关键词不在这里，它们要各自的区域。
COMBAT_HUD_REGION = (0, 10, 290, 235)  # x0, y0, x1, y1


class _TimedInfer:
    """包住 OrtInferSession，只累计**纯 onnx 推理**的耗时。

    det 的耗时里混着 resize/归一化/DB 后处理/框还原，rec 的混着归一化/批补齐/CTC
    解码。不拆开就不知道 432ms 到底花在哪一段 —— 实测 det 在整屏下 736 和 512 两个
    输入尺寸都是 117ms（面积差 2 倍），说明 ONNX 面积根本不是主导项，只有拆开才看得见。

    `__getattr__` 转发是兜底：构造期会调 `have_key()`/`get_character_list()`，
    而包装发生在构造之后，所以正常用不到。
    """

    def __init__(self, inner):
        self._inner = inner
        self.ms = 0.0
        self.n = 0

    def reset(self):
        self.ms = 0.0
        self.n = 0

    def __call__(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return self._inner(*a, **kw)
        finally:
            self.ms += (time.perf_counter() - t0) * 1000
            self.n += 1

    def __getattr__(self, k):
        return getattr(self._inner, k)


def _wrap_infer(comp, attr):
    shim = _TimedInfer(getattr(comp, attr))
    setattr(comp, attr, shim)
    return shim


_HELPER = None


def get_helper():
    """共用一个 RapidOCR 只为了借它的 helper 方法（load_img/preprocess/
    get_crop_img_list/...）。每个变体都 new 一个的话，会把三个生产模型重新加载
    十几遍，白白多花十几秒和一大截内存。"""
    global _HELPER
    if _HELPER is None:
        _HELPER = RapidOCR()
    return _HELPER


class Pipeline:
    """一个 (det, rec) 组合。**自己持有组件**，在 run() 时才绑定到共用 helper 上。

    不能把组件直接存进 helper：helper 是单例，构造两个 Pipeline 时后一个会把前一个
    的 det/rec 覆盖掉，于是先构造的那个 Pipeline 会静默地用错模型。
    """

    def __init__(self, det, rec, cls=None, profile=False):
        self.det = det
        self.rec = rec
        self.cls = cls
        self.profile = profile
        self.det_infer = _wrap_infer(det, "infer") if profile else None
        self.rec_infer = _wrap_infer(rec, "session") if profile else None

    def run(self, frame, use_cls=True, region=None):
        o = get_helper()
        o.text_det = self.det
        o.text_rec = self.rec
        if self.cls is not None:
            o.text_cls = self.cls

        # 区域化：只把矩形内的像素喂给 det/rec。放在 load_img 之前，因为后面全是
        # 按图算的，进来之后再裁就白跑了。裁剪本身是视图拷贝，0.2ms 量级。
        if region is not None:
            x0, y0, x1, y1 = region
            frame = np.ascontiguousarray(frame[y0:y1, x0:x1])

        img = o.load_img(frame)
        img, _rh, _rw = o.preprocess(img)
        img, _op = o.maybe_add_letterbox(img, {})

        t0 = time.perf_counter()
        boxes, det_el = o.auto_text_det(img)
        det_ms = (time.perf_counter() - t0) * 1000
        if boxes is None:
            out = {"det": det_ms, "cls": 0.0, "rec": 0.0,
                   "total": det_ms, "n_crops": 0}
            return [], self._add_profile(out)

        crops = o.get_crop_img_list(img, boxes)

        cls_ms = 0.0
        if use_cls and getattr(o, "use_cls", False):
            t0 = time.perf_counter()
            crops, _cres, _cel = o.text_cls(crops)
            cls_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        rec_res, _rel = o.text_rec(crops)
        rec_ms = (time.perf_counter() - t0) * 1000

        texts = []
        for item in (rec_res or []):
            t = item[0] if isinstance(item, (list, tuple)) else item
            if t:
                texts.append(t)
        out = {"det": det_ms, "cls": cls_ms, "rec": rec_ms,
               "total": det_ms + cls_ms + rec_ms, "n_crops": len(crops)}
        return texts, self._add_profile(out)

    def _add_profile(self, out):
        """把 det/rec 各自拆成 onnx / 前后处理两段。只在 profile=True 时有值。"""
        if not self.profile:
            return out
        for tag, shim in (("det", self.det_infer), ("rec", self.rec_infer)):
            out[tag + "_onnx"] = round(shim.ms, 2)
            out[tag + "_n"] = shim.n
            shim.reset()
        return out


# ---------------------------------------------------------------- 抓帧


def cmd_capture(args):
    import win32gui

    ws = find_windows("War Thunder") or find_windows("Thunder")
    if not ws:
        die("找不到游戏窗口（找的是标题含 'War Thunder' 或 'Thunder' 的可见窗口）")
    hwnd, title = ws[0]

    # 必须校验前台窗口就是游戏窗口。mss 抓的是屏幕像素，游戏不在前台时抓到的是
    # 别的程序 —— 上一轮就误抓成了浏览器画面。挡在保存之前，不要先存再检查。
    fg = win32gui.GetForegroundWindow()
    if fg != hwnd:
        fg_title = win32gui.GetWindowText(fg)
        log("拒绝抓帧：游戏窗口不在前台。")
        log("  游戏窗口 : hwnd=%s  %r" % (hwnd, title))
        log("  前台窗口 : hwnd=%s  %r" % (fg, fg_title))
        log("请把 War Thunder 切到前台、停在**战斗中**，再重新运行。")
        raise SystemExit(2)

    rect = get_rect(hwnd)
    if rect["width"] < 100 or rect["height"] < 100:
        die("游戏客户区尺寸异常: %r（窗口最小化了？）" % (rect,))

    outdir = os.path.join(FRAMES_DIR, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(outdir, exist_ok=True)
    log("窗口 %r  客户区 %dx%d" % (title, rect["width"], rect["height"]))
    log("输出 %s" % outdir)
    for s in range(args.countdown, 0, -1):
        log("  %d..." % s)
        time.sleep(1)

    n = args.count
    for i in range(n):
        frame = capture(rect)
        if frame is None or frame.size == 0:
            log("  第 %d 帧抓取失败，跳过" % i)
            continue
        path = os.path.join(outdir, "f%02d.png" % i)
        cv2.imwrite(path, frame)
        log("  [%2d/%d] %s  %dx%d" % (i + 1, n, os.path.basename(path),
                                      frame.shape[1], frame.shape[0]))
        time.sleep(args.gap)
    log("抓帧完成: %s" % outdir)
    return outdir


def latest_frames_dir():
    if not os.path.isdir(FRAMES_DIR):
        die("还没有任何帧。先跑 `python tools/ocr_bench.py capture`")
    subs = [os.path.join(FRAMES_DIR, d) for d in os.listdir(FRAMES_DIR)
            if os.path.isdir(os.path.join(FRAMES_DIR, d))]
    if not subs:
        die("%s 下没有帧目录" % FRAMES_DIR)
    return max(subs, key=os.path.getmtime)


def load_frames(d, limit=None):
    names = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
    if limit:
        names = names[:limit]
    frames = []
    for f in names:
        img = cv2.imread(os.path.join(d, f), cv2.IMREAD_COLOR)
        if img is not None:
            frames.append((f, img))
    if not frames:
        die("%s 里没有可读的 png" % d)
    return frames


# ---------------------------------------------------------------- 变体定义
#
# 基线名字**必须精确匹配**着跳过，不能用 startswith("v4-prod") —— 因为
# `v4-prod(narrow192)` 是同一个模型、只改填充下限的候选，它也以 v4-prod 开头，
# 用前缀判断会把它一起跳掉，于是"不改模型的纯收益"永远不会被测到。


REC_BASELINE = "v4-prod(320)"
DET_BASELINE = "v4-prod(min736)"


def rec_variants(base, files, quick=False):
    """[(名字, Pipeline 工厂)]，det 固定为生产配置。"""
    prod_rec = _abs_model(base["Rec"]["model_path"])
    # **同一个模型**、只把每批填充宽度下限从 320 调到 192。这是唯一一个"不动模型、
    # 纯赚"的候选，所以必须和换模型的候选放在一张表里比 —— 否则会得出"换模型才快"
    # 的错误结论。
    out = [(REC_BASELINE, lambda: make_rec(base, prod_rec)),
           ("v4-prod(narrow192)", lambda: make_rec(base, prod_rec, img_w=192))]

    plans = [("v6-tiny", "v6_tiny_rec"), ("v6-small", "v6_small_rec")]
    if quick:
        plans = plans[:1]
    for label, key in plans:
        yml = read_yml(files[YML_SUFFIX[key]])
        chars = rec_dict_from_yml(yml, files[YML_SUFFIX[key]])
        keys = write_keys(chars, os.path.join(MODELS_DIR, key + "_keys.txt"))
        for norm in ("centered", "plain"):
            nm = "%s-%s" % (label, norm)
            out.append((nm, (lambda m=files[key], k=keys, n=norm:
                             make_rec(base, m, keys_path=k, norm=n))))
    return out


def v6_det_kwargs(files):
    """v6 tiny det 的 DBPostProcess 阈值，直接取自它的 yml，不在这里硬抄。

    阶段 5 要自己建 det（quick 模式下 det_variants 里没有 max736 那一档），
    所以抽成函数两边共用，免得阈值在两处各写一份然后漂移。
    """
    pp = (read_yml(files["v6_tiny_det_y"]).get("PostProcess") or {})
    return dict(thresh=pp.get("thresh", 0.2), box_thresh=pp.get("box_thresh", 0.4),
                unclip_ratio=pp.get("unclip_ratio", 1.4),
                max_candidates=pp.get("max_candidates", 3000))


def det_variants(base, files, quick=False):
    prod_det = _abs_model(base["Det"]["model_path"])
    out = [(DET_BASELINE, lambda: make_det(base, prod_det))]

    v6 = v6_det_kwargs(files)
    m = files["v6_tiny_det"]

    # 归一化是**待定的**：v6 det 的 yml 写的是 ImageNet，但实测 ImageNet 出全零图，
    # 而现役生产用的是 0.5/0.5。合成图上分不出 0.5/0.5 和 /255 谁对（实心色块不是
    # 文字，v4 自己在合成图上都只det到 1 个框），所以两个都跑、用真帧定。
    norms = {"05": ([0.5] * 3, [0.5] * 3), "plain": ([0.0] * 3, [1.0] * 3)}
    for nlab, (mean, std) in norms.items():
        out.append(("v6-%s(min736)" % nlab,
                    lambda mn=mean, sd=std: make_det(base, m, mean=mn, std=sd, **v6)))
    if not quick:
        # 降采样必须改 limit_type：现役 min/736 下 720p 永远不会被缩小（720<736，
        # 反而放大约 2%）。改 max 才真的缩。
        for lim in (960, 736):
            for nlab, (mean, std) in norms.items():
                out.append(("v6-%s(max%d)" % (nlab, lim),
                            lambda mn=mean, sd=std, l=lim:
                            make_det(base, m, limit_type="max", limit_side_len=l,
                                     mean=mn, std=sd, **v6)))
    return out


def parse_region(s):
    """把 `x0,y0,x1,y1` 解析成元组；不传就用 COMBAT_HUD_REGION。"""
    if not s:
        return COMBAT_HUD_REGION
    try:
        v = [int(x) for x in s.replace(" ", "").split(",")]
        if len(v) != 4:
            raise ValueError(len(v))
    except ValueError:
        die("--region 要写成 x0,y0,x1,y1 四个整数（例如 0,10,290,235），收到 %r" % s)
    return tuple(v)


# ---------------------------------------------------------------- 评分


def tokens_present(texts, tokens):
    joined = "\n".join(texts)
    return {t for t in tokens if t in joined}


def all_critical():
    toks = []
    for v in CRITICAL.values():
        toks.extend(v)
    return toks


def score_run(texts, baseline_texts):
    """返回这次运行的准确率指标。baseline_texts 为 None 时表示它自己就是基线。"""
    got = tokens_present(texts, all_critical())
    lost = []
    if baseline_texts is not None:
        lost = sorted(tokens_present(baseline_texts, all_critical()) - got)
    return {
        "n_texts": len(texts),
        "tokens": sorted(got),
        "lost": lost,
    }


def diff_strings(base_texts, texts):
    """基线有、候选没有的整串（去掉纯噪声）。用于报告里展示具体错读。"""
    bs = set(base_texts)
    return sorted(t for t in bs if t not in set(texts))[:6]


def pick_best(results, stage):
    """某阶段里最快且不丢串的变体，返回 (名字, 该阶段的耗时 ms)。

    按**该阶段自己的耗时**排（rec 阶段比 rec，det 阶段比 det），不按整屏 —— 整屏
    里混着另一段固定不变的成本，会把排序带偏。基线不参与排序，由调用方拿它去比。
    """
    key = "rec" if stage == "rec" else "det"
    cands = [(r["ms"][key]["median"], name) for name, r in results.items()
             if r.get("stage") == stage and not r.get("lost_any")]
    return min(cands)[::-1] if cands else (None, None)


def summarize_ms(samples):
    if not samples:
        return {}
    return {
        "median": round(statistics.median(samples), 1),
        "p90": round(sorted(samples)[int(len(samples) * 0.9) - 1], 1)
        if len(samples) > 1 else round(samples[0], 1),
        "max": round(max(samples), 1),
    }


# ---------------------------------------------------------------- dump


def cmd_dump(args):
    """逐帧打印每个变体**实际读到的文字**。

    这是最直观的"结果对比"：`bench` 给的是数字和判定，`dump` 给的是原文 ——
    一眼就能看出 v6 是把 `弹舱` 读成了 `单舱`，还是整行都读飞了。
    """
    fdir = args.frames or latest_frames_dir()
    frames = load_frames(fdir, limit=args.limit)
    which = ["v6_tiny_rec", "v6_tiny_det"]
    if not args.quick:
        which.append("v6_small_rec")
    files = ensure_models(which)
    base = base_config()
    det_prod = _abs_model(base["Det"]["model_path"])
    rec_prod = _abs_model(base["Rec"]["model_path"])
    log("帧目录 %s   共 %d 帧" % (fdir, len(frames)))

    # 只跑基线 + 用户点名要看的那几档，避免把 11 个变体全跑一遍
    variants = [("v4-prod        (基线)", make_det(base, det_prod),
                 make_rec(base, rec_prod))]
    for name, factory in rec_variants(base, files, quick=args.quick):
        if name.startswith("v4-prod"):
            continue
        variants.append((name + " (只换rec)", make_det(base, det_prod), factory()))
    for name, factory in det_variants(base, files, quick=args.quick):
        if name.startswith("v4-prod"):
            continue
        variants.append((name + " (只换det)", factory(), make_rec(base, rec_prod)))

    # 每个变体预先造成 Pipeline（它自己持有组件），每帧只跑一次
    pipes = [(label, Pipeline(det, rec)) for label, det, rec in variants]

    for fname, img in frames[:args.frames_n]:
        log("\n" + "=" * 78)
        log("帧 %s   %dx%d" % (fname, img.shape[1], img.shape[0]))
        log("-" * 78)
        got = [(label, pipe.run(img)[0]) for label, pipe in pipes]
        for label, texts in got:
            log("  %-24s %s" % (label, " | ".join(texts) if texts else "(无文字)"))


# ---------------------------------------------------------------- bench


def cmd_bench(args):
    fdir = args.frames or latest_frames_dir()
    frames = load_frames(fdir, limit=args.limit)
    log("帧目录 %s   共 %d 帧" % (fdir, len(frames)))

    which = ["v6_tiny_rec", "v6_tiny_det"]
    if not args.quick:
        which.append("v6_small_rec")
    files = ensure_models(which, force=args.redownload)
    base = base_config()

    results = {}

    # ---- 阶段 1：基线。所有后面的对比都对着它 ----
    log("\n[1/5] 基线 (v4 det + v4 rec, 生产配置)")
    pipe = Pipeline(make_det(base, _abs_model(base["Det"]["model_path"])),
                    make_rec(base, _abs_model(base["Rec"]["model_path"])))
    base_texts, base_ms = run_pipeline(pipe, frames)
    results["v4-prod"] = {"stage": "baseline", "name": "v4-prod",
                          "ms": base_ms, "score": score_run_flatten(base_texts),
                          "texts": base_texts, "lost_any": [], "diffs": []}
    log("  整屏中位 %.0fms (det %.0f / cls %.0f / rec %.0f)"
        % (base_ms["total"]["median"], base_ms["det"]["median"],
           base_ms["cls"]["median"], base_ms["rec"]["median"]))

    # ---- 阶段 2：只换 rec（det 固定为基线，crop 完全相同 → 严格可比）----
    log("\n[2/5] 只换 rec（det 固定 = 基线）")
    det_prod = _abs_model(base["Det"]["model_path"])
    rec_defs = dict(rec_variants(base, files, quick=args.quick))
    det_defs = dict(det_variants(base, files, quick=args.quick))
    for name, factory in rec_defs.items():
        if name == REC_BASELINE:
            continue
        pipe = Pipeline(make_det(base, det_prod), factory())
        texts, ms = run_pipeline(pipe, frames)
        rec = compare_to_base(name, texts, base_texts, frames, "rec")
        rec["ms"] = ms
        results[name] = rec
        log_variant(rec, ms)

    # ---- 阶段 3：只换 det（rec 固定为基线）----
    log("\n[3/5] 只换 det（rec 固定 = 基线）")
    rec_prod = _abs_model(base["Rec"]["model_path"])
    for name, factory in det_defs.items():
        if name == DET_BASELINE:
            continue
        pipe = Pipeline(factory(), make_rec(base, rec_prod))
        texts, ms = run_pipeline(pipe, frames)
        d = compare_to_base(name, texts, base_texts, frames, "det")
        d["ms"] = ms
        results[name] = d
        log_variant(d, ms)

    # ---- 阶段 4：把两个阶段各自值得换的组合起来 ----
    # 关键：**只有比现役自己那一段更快才值得换**。候选之间排队是不够的 —— 例如
    # v6-small rec 通过了丢串检查，但比现役 v4 rec 还慢，换它纯亏。
    # 也必须真的跑一次：det 和 rec 的耗时不是简单相加（crop 数、batch 补齐都会变），
    # 而且"单独都不丢串"不保证"组合起来也不丢串"。
    log("\n[4/5] 组合最优")
    base_det = base_ms["det"]["median"]
    base_rec = base_ms["rec"]["median"]
    rname, rcost = pick_best(results, "rec")
    dname, dcost = pick_best(results, "det")

    use_rec = rname if (rname and rcost < base_rec) else None
    use_det = dname if (dname and dcost < base_det) else None
    log("  现役 det %.0fms / rec %.0fms" % (base_det, base_rec))
    if rname and not use_rec:
        log("  rec 不换: 最快的合格候选 %s (%.0fms) 并不比现役快" % (rname, rcost))
    if dname and not use_det:
        log("  det 不换: 最快的合格候选 %s (%.0fms) 并不比现役快" % (dname, dcost))
    if not rname:
        log("  rec 一律否决: 没有合格候选（丢串）")
    if not dname:
        log("  det 一律否决: 没有合格候选（丢串）")

    if use_rec is None and use_det is None:
        log("  结论: 两条都不换，保持现役（现役已是最快且不丢串的组合）")
    else:
        det_f = det_defs[use_det] if use_det else (lambda: make_det(base, det_prod))
        rec_f = rec_defs[use_rec] if use_rec else (lambda: make_rec(base, rec_prod))
        combo = "%s + %s" % (use_det or "v4-det", use_rec or "v4-rec")
        pipe = Pipeline(det_f(), rec_f())
        texts, ms = run_pipeline(pipe, frames)
        c = compare_to_base(combo, texts, base_texts, frames, "combo")
        c["ms"] = ms
        c["parts"] = {"det": use_det, "rec": use_rec}
        results[combo] = c
        log_variant(c, ms)

    # ---- 阶段 5：区域化 + 拆开看耗时 ----
    #
    # 前四个阶段都在解"换成哪个模型"，这一段解的是"要不要让模型看整屏"。
    # 真帧上的关键串只落在左上 290x235（占屏 7%），所以这一段的对照是：
    #   整屏 + 基线配置   vs   区域 + 基线配置   vs   区域 + 换 det + 窄填充
    # 三个都开着 profile，把 det/rec 各自拆成 onnx 和前后处理 —— 只有拆开才知道
    # 下一步该优化哪一段。
    if not args.no_region:
        region = parse_region(args.region)
        log("\n[5/5] 区域化 (只 OCR 战斗中 HUD 块 %s) + 耗时拆分" % (region,))
        v6_kw = v6_det_kwargs(files)
        # v6 det 用生产同款 0.5/0.5 归一化：真帧上 0.5/0.5 和纯 /255 都合格且同速，
        # 所以这一段少变一个量。
        v6_05 = [0.5] * 3

        def v6max(lim=736):
            return make_det(base, files["v6_tiny_det"], limit_type="max",
                            limit_side_len=lim, mean=v6_05, std=v6_05, **v6_kw)

        combos = [
            ("整屏 + 现役 det/rec", None,
             lambda: make_det(base, det_prod), lambda: make_rec(base, rec_prod)),
            ("区域 + 现役 det/rec", region,
             lambda: make_det(base, det_prod), lambda: make_rec(base, rec_prod)),
            ("区域 + v6-max736 + 现役 rec", region,
             v6max, lambda: make_rec(base, rec_prod)),
            ("区域 + v6-max736 + 窄填充 rec", region,
             v6max, lambda: make_rec(base, rec_prod, img_w=192)),
        ]
        for label, reg, det_f, rec_f in combos:
            pipe = Pipeline(det_f(), rec_f(), profile=True)
            texts, ms = run_pipeline(pipe, frames, region=reg)
            c = compare_to_base(label, texts, base_texts, frames, "region")
            c["ms"] = ms
            results[label] = c
            log("  %-30s 总中位 %6.0fms  det %5.0f(onnx %5.0f)  rec %5.0f(onnx %5.0f)  crop %2.0f  %s"
                % (label, ms["total"]["median"], ms["det"]["median"],
                   ms.get("det_onnx", {}).get("median", -1), ms["rec"]["median"],
                   ms.get("rec_onnx", {}).get("median", -1),
                   ms.get("n_crops", {}).get("median", 0),
                   "合格" if not c["lost_any"] else "丢串(%d帧)" % len(c["lost_any"])))
            if c["lost_any"]:
                fn, lost = c["lost_any"][0]
                log("      丢串 @%s: %s" % (fn, ", ".join(lost)))

    # ---- 结论 ----
    log("\n" + "=" * 78)
    log(summarize(results, base_ms))

    out = os.path.join(fdir, "bench.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"frames": fdir, "n": len(frames), "results": results},
                  f, ensure_ascii=False, indent=2)
    log("\n明细已写入 %s" % out)


def run_pipeline(pipe, frames, use_cls=True, region=None):
    """逐帧跑，返回 (每帧的串, 每段的耗时统计)。

    耗时键不是写死的 —— 开了 profile 会多出 det_onnx/rec_onnx/n_crops 等，
    多出来的键自动一起统计，免得每加一个探针都要回来改这里。
    """
    texts_per_frame = []
    ms_acc = {}
    for _name, img in frames:
        texts, ms = pipe.run(img, use_cls=use_cls, region=region)
        texts_per_frame.append(texts)
        for k, v in ms.items():
            ms_acc.setdefault(k, []).append(v)
    return texts_per_frame, {k: summarize_ms(v) for k, v in ms_acc.items()}


def score_run_flatten(texts_per_frame):
    return [score_run(t, None) for t in texts_per_frame]


def compare_to_base(name, texts_per_frame, base_per_frame, frames, stage):
    per_frame = []
    lost_any = []
    diffs = []
    for i, texts in enumerate(texts_per_frame):
        s = score_run(texts, base_per_frame[i])
        per_frame.append(s)
        if s["lost"]:
            lost_any.append((frames[i][0], s["lost"]))
        d = diff_strings(base_per_frame[i], texts)
        if d:
            diffs.append((frames[i][0], d))
    return {"stage": stage, "name": name, "frames": per_frame,
            "lost_any": lost_any, "diffs": diffs}


def log_variant(r, ms):
    nlost = len(r["lost_any"])
    verdict = "合格" if nlost == 0 else "不合格(丢串)"
    log("  %-24s 整屏中位 %6.0fms (det %5.0f / rec %5.0f)  %s"
        % (r["name"], ms["total"]["median"], ms["det"]["median"],
           ms["rec"]["median"], verdict))
    if r["lost_any"]:
        fn, lost = r["lost_any"][0]
        log("      丢串 @%s: %s" % (fn, ", ".join(lost)))
    if r["diffs"]:
        fn, d = r["diffs"][0]
        log("      读不同 @%s: %s" % (fn, " | ".join(d[:3])))


def summarize(results, base_ms):
    lines = []
    lines.append("基线整屏中位 %.0fms -> 1.5s 周期下 OCR 占 %.0f%%"
                 % (base_ms["total"]["median"],
                    100 * base_ms["total"]["median"] / 1500))
    lines.append("")
    lines.append("%-34s %10s %10s %10s %8s  %s"
                 % ("变体", "整屏中位", "det中位", "rec中位", "丢串帧", "判定"))
    lines.append("-" * 86)
    combo = None
    for name, r in sorted(results.items(), key=lambda kv: kv[1]["ms"]["total"]["median"]):
        if r.get("stage") == "region":
            continue  # 区域化单独一节报告，见下
        ms = r["ms"]
        nlost = len(r.get("lost_any", []))
        verdict = "合格" if nlost == 0 else "不合格"
        if r.get("stage") == "combo" and nlost == 0:
            verdict = "推荐"
            combo = name
        lines.append("%-34s %9.0fms %9.0fms %9.0fms %8d  %s"
                     % (name, ms["total"]["median"], ms["det"]["median"],
                        ms["rec"]["median"], nlost, verdict))
    lines.append("")
    # 推荐只看组合变体：只有它代表真正会落地的那个配置。没有组合变体 =
    # 阶段 4 判定两条都不值得换（或候选全被丢串否决），那就保持现役。
    if combo:
        b = results[combo]["ms"]["total"]["median"]
        parts = results[combo].get("parts") or {}
        lines.append("推荐: %s   整屏中位 %.0fms（基线 %.0fms，%.1fx）"
                     % (combo, b, base_ms["total"]["median"],
                        base_ms["total"]["median"] / max(b, 1)))
        if not parts.get("rec"):
            lines.append("  注意 rec **没有**换（现役 v4 rec 反而比候选快），只换 det。")
        if not parts.get("det"):
            lines.append("  注意 det **没有**换，只换 rec。")
        lines.append("  1.5s 周期下 OCR 占 %.0f%%，余量%s"
                     % (100 * b / 1500, "充裕" if b < 750 else "偏紧（考虑 2.0s）"))
        lines.append("  这只是单次扫描耗时，不含 CPU 竞争；周期降到 1.5s 还要处理"
                     "每轮按键频率翻倍的问题（见计划第 6 节）。")
    else:
        lines.append("结论: **两条都不换** —— 没有候选能在「不丢串」的前提下比现役快。")
        lines.append("  （真帧上若结果不同，说明合成图/帧数不足，请用 capture 抓真帧重跑）")

    # 区域化单独说，**不并进上面的排名**：它比整屏快得多，但它只 OCR 了一个矩形，
    # 而 `lost_any` 检查不到这个矩形里**本来就没有**的串 —— 这批帧全是战斗中，
    # 所以「机库/匹配中的关键词全丢」这种失败在这里是隐形的。排名会把一个只在
    # 战斗中成立的方案排到第一，那是误导。
    reg = [(n, r) for n, r in results.items() if r.get("stage") == "region"]
    if reg:
        lines.append("")
        lines.append("区域化（阶段 5，**只对 %s 有效**）:" % (COMBAT_HUD_REGION,))
        for n, r in sorted(reg, key=lambda kv: kv[1]["ms"]["total"]["median"]):
            ms = r["ms"]
            lines.append("  %-32s %6.0fms  (det %4.0f onnx %4.0f / rec %4.0f onnx %4.0f, "
                         "crop %.0f)  %s"
                         % (n, ms["total"]["median"], ms["det"]["median"],
                            ms.get("det_onnx", {}).get("median", -1),
                            ms["rec"]["median"],
                            ms.get("rec_onnx", {}).get("median", -1),
                            ms.get("n_crops", {}).get("median", 0),
                            "合格" if not r["lost_any"] else "丢串"))
        lines.append("  ⚠ 这个矩形只覆盖战斗中 HUD。机库/匹配中/加载中的关键词不在里面，")
        lines.append("    所以「区域化合格」只证明这条帧集没丢串，**不证明产品上可用** ——")
        lines.append("    要么每个场景各留一个区域，要么先用一次廉价的全屏 det 决定裁哪块。")
    return "\n".join(lines)


# ---------------------------------------------------------------- selftest


def cmd_selftest(args):
    """不改任何东西的自检。三条：词表守卫、流水线等价、v6 输出维度。"""
    log("selftest")
    files = ensure_models(["v6_tiny_rec", "v6_tiny_det"])
    base = base_config()
    ok = True

    # 1. 词表守卫：故意少一项，必须抓出来
    log("\n[1] 词表错位守卫")
    yml_path = files["v6_tiny_rec_y"]
    chars = rec_dict_from_yml(read_yml(yml_path), yml_path)
    log("  yml 词表 %d 项" % len(chars))
    import onnxruntime as ort
    sess = ort.InferenceSession(files["v6_tiny_rec"], providers=["CPUExecutionProvider"])
    dim = sess.get_outputs()[0].shape[2]
    log("  onnx 输出维度 %d   (期望 len(dict)+2)" % dim)
    if len(chars) + 2 != dim:
        log("  ✗ 对不上 —— 真跑起来会整体错位")
        ok = False
    else:
        log("  ✓ 对得上")
    short = chars[:-1]
    if len(short) + 2 == dim:
        log("  ✗ 守卫失效：少一项居然没被发现")
        ok = False
    else:
        log("  ✓ 少一项会被 len+2 != dim 抓到（这就是正则那版的坑）")

    # 2. 流水线等价：自己的 run() 必须和 RapidOCR.__call__ 出一样的文字
    log("\n[2] 流水线等价（Pipeline.run vs RapidOCR.__call__）")
    frames = [(f, cv2.imread(os.path.join(args.frames, f), cv2.IMREAD_COLOR))
              for f in sorted(os.listdir(args.frames))[:2]
              if f.lower().endswith(".png")] if args.frames else []
    if not frames:
        log("  (无帧可测，跳过 —— 跑一次 capture 后再来)")
    else:
        for fn, img in frames:
            ref, _ = RapidOCR()(img)
            ref_texts = [r[1] for r in (ref or [])]
            pipe = Pipeline(make_det(base, _abs_model(base["Det"]["model_path"])),
                            make_rec(base, _abs_model(base["Rec"]["model_path"])))
            got, _ = pipe.run(img)
            if ref_texts == got:
                log("  ✓ %s 完全一致 (%d 条)" % (fn, len(got)))
            else:
                only_ref = [t for t in ref_texts if t not in got][:3]
                only_got = [t for t in got if t not in ref_texts][:3]
                log("  ✗ %s 不一致" % fn)
                log("      仅 upstream: %s" % only_ref)
                log("      仅 bench   : %s" % only_got)
                ok = False

    # 3. v6 rec 在合成图上能出字（证明驱动得起来）
    log("\n[3] v6 rec 驱动检查（合成图）")
    try:
        from PIL import Image, ImageDraw, ImageFont
        chars6 = chars
        keys = write_keys(chars6, os.path.join(MODELS_DIR, "v6_tiny_rec_keys.txt"))
        r = make_rec(base, files["v6_tiny_rec"], keys_path=keys, norm="plain")
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)
        im = Image.new("RGB", (400, 56), (0, 0, 0))
        ImageDraw.Draw(im).text((8, 6), "节流阀", font=font, fill=(255, 255, 255))
        crop = np.array(im)[:, :, ::-1].copy()
        res, _el = r([crop])
        text = res[0][0]
        log("  合成图 '节流阀' -> %r" % text)
        if "节流阀" in text:
            log("  ✓ 能正确识读")
        else:
            log("  ✗ 读不出正确文字（归一化/词表有问题）")
            ok = False
    except Exception as e:
        log("  ✗ 驱动失败: %s: %s" % (type(e).__name__, e))
        ok = False

    log("\nselftest %s" % ("通过" if ok else "**未通过**"))
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI


def main():
    ap = argparse.ArgumentParser(
        description="PP-OCRv6 vs ch_PP-OCRv4 离线对比（不改产品代码，不新增依赖）")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("capture", help="抓帧（需游戏在前台）")
    c.add_argument("--count", type=int, default=20)
    c.add_argument("--gap", type=float, default=0.4, help="帧间隔秒")
    c.add_argument("--countdown", type=int, default=5)
    c.set_defaults(func=cmd_capture)

    b = sub.add_parser("bench", help="对已抓的帧跑对比")
    b.add_argument("--frames", default=None, help="帧目录（默认用最新的）")
    b.add_argument("--limit", type=int, default=None, help="只用前 N 帧")
    b.add_argument("--quick", action="store_true", help="跳过 small rec 和降采样档")
    b.add_argument("--redownload", action="store_true")
    b.add_argument("--region", default=None,
                   help="区域化的矩形 x0,y0,x1,y1（默认战斗中 HUD 块 %s）"
                        % (",".join(str(v) for v in COMBAT_HUD_REGION),))
    b.add_argument("--no-region", action="store_true", help="跳过阶段 5（区域化）")
    b.set_defaults(func=cmd_bench)

    d = sub.add_parser("dump", help="逐帧打印每个变体实际读到的文字（最直观的对比）")
    d.add_argument("--frames", default=None)
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--frames-n", type=int, default=3, help="打印前几帧，默认 3")
    d.add_argument("--quick", action="store_true")
    d.set_defaults(func=cmd_dump)

    s = sub.add_parser("selftest", help="自检（不需要游戏）")
    s.add_argument("--frames", default=None)
    s.set_defaults(func=cmd_selftest)

    a = sub.add_parser("all", help="capture + bench")
    a.add_argument("--count", type=int, default=20)
    a.add_argument("--gap", type=float, default=0.4)
    a.add_argument("--countdown", type=int, default=5)
    a.add_argument("--quick", action="store_true")
    a.add_argument("--redownload", action="store_true")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 1
    if args.cmd == "all":
        d = cmd_capture(argparse.Namespace(count=args.count, gap=args.gap,
                                           countdown=args.countdown))
        args.frames, args.limit, args.quick = d, None, args.quick
        args.redownload = args.redownload
        args.region, args.no_region = None, False
        return cmd_bench(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
