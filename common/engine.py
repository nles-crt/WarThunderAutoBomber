# -*- coding: utf-8 -*-
"""发动机温度 → 油门目标（纯函数：无 IO、无第三方依赖）

**为什么要单独一个模块**：和 `common/nav_law.py` / `common/results.py` 同一个理由 ——
这套解析只能拿**真实录下来的 OCR** 验证，控制器只能拿**离线仿真**验证。抽成纯函数之后
`tools/engine_check.py` 才能拿 logs/ 里的真帧和假想被控对象把**出厂代码**跑一遍；
否则调错一个常数，只有等实机把发动机烧了才知道。

**为什么是闭环，而不是「加力给 1.0、降温给 0.3」**：油门在实机上**不是线性轴**。
把 `logs/scenes_*.jsonl` 的 `节流阀` 读数和同名 `.log` 里 `手柄 L:(x,y)` 的 y 值按时间戳
对齐之后（22 份录音、2900 余帧）：

    命令的 y = 0.0~0.36                ->  HUD 节流阀 `100%`
    命令的 y = 0.5 / 0.55 / 0.8 / 1.0  ->  HUD 节流阀 `110%加力`

是**台阶式**的，阈值大致落在 0.4 附近（顺带说明：投弹视角那个 0.55 和返航那个 0.8
其实也一直压在加力上）。台阶具体在哪、换飞机换键位会不会挪，都是先验未知的 ——
所以这里**不写死一个「加力值」**，而是拿 HUD 自己的读数做闭环，让它自己找。

**为什么规则只有一条**：`|目标 - 读数| <= 死区` 就原地不动，否则朝目标挪一步。
先写过一版「记住切换点 + 回退边距」的，离线仿真证明它在线性轴上会**极限环**
（0.92↔1.00 反复横跳、读数 100↔92）。规则越简单越稳，别把那套加回来。

**为什么这个循环天生安全**：两个目标（100 / 110）都在读数的上半段，所以
**只有读数高于目标时命令才会往下走**。读数被 OCR 读成 0、读成 133、或者整帧读丢，
都只会把油门**往上**推，不会把发动机关小。摇杆下限也落在「100%」那一档里
（实测 0.0~0.36 都是 100%），所以哪怕一路压到底，也还是巡航油门，不是空中停车。
"""

import re

# 「节流阀」标签后面紧跟着的那个 token 就是读数框（实测 5182/5194 帧如此，
# 剩下 12 帧后面跟的是下一个仪表的名字 `速度`）。OCR 把读数写成
# `110%加力` / `100 %` / `110 % 加力` / `O%`（0 被读成字母 O）等等，
# 所以匹配的是「开头几个数字 + 一个 %」。
_RE_PCT = re.compile(r"^\s*([0-9Oo]{1,3})\s*%")
# 少数帧（6/5194）OCR 把 % 读到了数字前面：`%0`。
_RE_PCT_REV = re.compile(r"^\s*%\s*([0-9Oo]{1,3})\s*$")

# 标签往后看这么多 token。实测值框的下一个 token 是 `速度`（下一个仪表的名字），
# 所以 3 足够；再大就越过仪表区、可能读到别处的数字。
LOOKAHEAD = 3

# 过热告警。实测 token 形态：`油温过高`×87 / `水温过高`×77 / `油温过`×11 /
# `水温过`×8 / `油温过膏`×1 / `水温过高：`×1 —— 用「过」截断匹配，两种写法一起收。
_HOT_WORDS = ("油温过", "水温过")

# 出厂值。改了这里就要重跑 tools/engine_check.py：收敛性对任何步长都成立，
# 但「几步收敛」会变，那份检查是按出厂值断言的。
STEP_DEFAULT     = 0.25    # 每轮挪多少杆量（扫描周期 3s ⇒ 约 9s 从加力降到 100%）
DEADBAND_DEFAULT = 3.0     # 读数和目标差多少个百分点以内就原地不动
MIN_CMD_DEFAULT  = 0.0     # 摇杆下限（实测这一档就是 100%）
COOL_PCT_DEFAULT = 110.0   # 不过热时的目标（加力）
HOT_PCT_DEFAULT  = 100.0   # 过热时的目标
COOL_S_DEFAULT   = 10.0    # 告警消失多少秒才算凉下来


def _to_int(s):
    """OCR 会把 0 读成字母（实测有 `O%`）。"""
    return int(s.replace("O", "0").replace("o", "0"))


def parse_engine(texts):
    """把一帧的 OCR token 列表拆成发动机读数。

    返回 dict:
        pct    节流阀读数（整数百分比）；**None = 这一帧没读到**
        wep    读数框里带「加力」= 游戏自己承认在开加力
        hot    屏上有油温/水温过热告警
        words  命中的告警词（写日志用，一眼看出是油温还是水温）

    读不到就是读不到，**不做任何猜测性填充**：控制器对 None 的处理是原地不动，
    而任何「大概是这个值吧」的猜测都可能让它朝错误方向挪油门。
    """
    pct = None
    wep = False
    hot_words = []
    ts = list(texts or [])
    n = len(ts)

    for i, t in enumerate(ts):
        s = t or ""
        if pct is None and "节流阀" in s:
            for j in range(i + 1, min(i + 1 + LOOKAHEAD, n)):
                nxt = ts[j] or ""
                m = _RE_PCT.match(nxt) or _RE_PCT_REV.match(nxt)
                if m:
                    pct = _to_int(m.group(1))
                    # 加力标记可能落在值框里（`110%加力`），也可能被 OCR 拆到
                    # 下一个 token（`110% 加力`），所以往后扫一小段。
                    wep = any("加力" in (ts[k] or "")
                              for k in range(j, min(j + LOOKAHEAD, n)))
                    break
        for w in _HOT_WORDS:
            if w in s and w not in hot_words:
                hot_words.append(w)

    return {"pct": pct, "wep": wep, "hot": bool(hot_words), "words": hot_words}


class SmartThrottle:
    """「过热降到 100%、否则加力」做成的自校准油门环。

    只持有两个状态：当前摇杆值 `cmd`、过热标志 `hot`（带迟滞）。
    **参数一个都不存** —— 每次 `observe` 由调用方传进来，这样 app.py 里那几个
    config 全局量改完立刻生效，和仓库里其它参数走的是同一套热生效机制。

    和 App._retry_next 同一个约定：**没有任何地方重置它**。场景切换、F12 暂停都不清状态，
    否则「清一次计时器就多按一次」那类 bug 会在这里重演。
    """

    def __init__(self, cmd=1.0):
        self.cmd = float(cmd)
        self.hot = False
        self._cool_since = None

    def observe(self, now, hot, pct, *, hot_pct, cool_pct, cool_s,
                step, deadband, lo=0.0, hi=1.0):
        """喂一帧的解析结果，返回 (新的摇杆值, 目标百分比)。"""

        # ---- 告警迟滞：过热立刻置位，要连续 cool_s 秒没有告警才降回 ----
        # 发动机温度到过限就掉不下来那么快，读数/OCR 又只有 3 秒一拍，
        # 不迟滞的话会在 100/110 之间来回切。
        if hot:
            self.hot = True
            self._cool_since = None
        elif self.hot:
            if self._cool_since is None:
                self._cool_since = now
            elif now - self._cool_since >= cool_s:
                self.hot = False
                self._cool_since = None

        target = hot_pct if self.hot else cool_pct

        # 这一帧没读到 -> 一步不动。宁可维持现状，也不拿猜的数字去挪油门。
        if pct is None:
            return self.cmd, target

        err = target - pct
        if abs(err) <= deadband:
            # 已经在目标附近：原地不动。**这一条才是它不震荡的原因** ——
            # 少了它，读数在目标上下抖一个百分点就会一直改杆量。
            return self.cmd, target

        self.cmd = min(hi, max(lo, self.cmd + step * (1.0 if err > 0 else -1.0)))
        return self.cmd, target
