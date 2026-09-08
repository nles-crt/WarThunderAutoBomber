# WarThunderAutoBomber

战争雷霆（War Thunder）空中目标自动轰炸脚本。常驻后台运行：通过 **YOLO** 在屏幕上识别地面轰炸战区，用 **OCR** 判断当前游戏场景（大厅 / 加载 / 战斗等）以自动启停，最后通过**虚拟 Xbox 360 手柄**模拟按键自动飞向目标并投弹。

> ⚠️ 仅供个人学习 / 自动化研究使用。任何第三方自动化工具都可能违反游戏用户协议并导致封号，使用风险请自行承担。

## 功能

- **全自动流程**：检测到战斗场景自动进入 → 追踪目标 → 切投弹视角 → 微调对准 → 投弹 →（可选）飞往敌方机场
- **YOLO 战区识别**：对屏幕画面实时检测轰炸区，内置细长框目标点修正
- **OCR 场景判断**：无需读内存，通过屏幕文字 + HUD 颜色判断当前场景并自动启停
- **多目标策略**：锁定最近 / 最左 / 最右战区，被队友抢时可自动切换目标
- **可循环轰炸**：支持单次 / 双次 / 无限循环直到炸弹耗尽
- **精细控制**：两级投弹微调（PI + 限速），摇杆死区与灵敏度自适应衰减
- **可选 8111 数据增强**：接入本地 `:8111` HTTP API 获取精确距离 / 机场导航（不开启则退回纯视觉方案）

## 环境要求

- Windows（依赖 vgamepad 虚拟手柄与 Win32 截图）
- 已安装并运行 War Thunder
- Python 3.10+

## 安装

```bash
pip install -r requirements.txt
```

`models/best.pt`（YOLO 权重，约 6 MB）已随仓库提供，无需额外下载。

## 快速开始

```bash
# 自动轰炸主程序（常驻后台，自动检测游戏窗口）
python main.py

# （可选）键盘 → 虚拟手柄工具：把键盘按键映射到手柄，供手动游玩
python x360_keyboard.py
```

程序启动后：

1. 打开 War Thunder 进入任意空战/陆战地图；
2. 脚本在 OCR 检测到「战斗中」场景后自动开始追踪并投弹；
3. `Ctrl+C` 或关闭进程会自动清理虚拟手柄。

所有参数（灵敏度、投弹死区、瞄准偏移、转向符号、循环次数等）都放在根目录 `config.json`，直接改文件即可、无需改代码。

## 目录结构

```
WarThunderAutoBomber/
├── main.py               # 入口：信号清理 + 启动 App
├── app.py                # 核心编排：4 条线程、控制算法、自动投弹流程
├── x360_keyboard.py      # 独立小工具：键盘 → 虚拟手柄（不属于自动轰炸流程）
├── config.json           # 全部可调参数
├── common/
│   ├── config.py         # 读取 config.json → 模块级常量
│   ├── gamepad.py        # vgamepad 封装：按钮 / 摇杆 / 扳机
│   ├── window.py         # Win32 找窗口 + MSS 截屏
│   ├── utils.py          # map_val：像素偏差 → 摇杆值（死区/灵敏度）
│   └── wt8111.py         # 本地 :8111 HTTP API（位置/距离/机场导航）
├── detect/
│   ├── yolo.py           # YOLO 检测 + 细长框目标点修正
│   └── scene.py          # OCR 场景分类 + UI 按钮触发
├── models/best.pt        # YOLO 模型权重
├── wt_controls/          # War Thunder 手柄键位配置文件（供手动参考/导入）
└── requirements.txt
```

## 工作流程

```
main.py → App → 4 条后台线程
  ├─ state_monitor_loop: OCR 场景识别（每 10s）→ 自动启停 / 开关舱门 / 按 UI 按钮
  ├─ target_info_loop:   8111 API 数据（每 1s，可选）
  ├─ recog_loop:         YOLO 检测（≤30 fps）
  └─ control_loop:       误差 → 摇杆值（≤33 Hz）
       └─ auto_bomb_sequence: 引导 → 投弹视角 → 对准 → 投弹 → 减速/返航
```

### 投弹流程

1. **引导（guide）**：满油门，追踪目标，直到与战区距离 ≤ `bomb_view_distance`（8111）或目标框占屏幕比例 ≥ `guide_box_ratio`（视觉兜底）；
2. **投弹视角（bomb view）**：按 `Y` 切到投弹瞄准镜，用阻尼 PI 控制器精细对准；
3. **投弹（drop）**：十字线与目标偏差进入阈值（`pixel_deviation` / `pixel_deviation_y`）后按 `LB + X` 批量投弹；
4. **投弹后**：退出投弹视角，油门降至 -50%，可选飞往最近的红方机场。

## 键位说明

默认按键映射来自 `wt_controls/` 中的手柄键位配置：

| 功能             | 按键     |
| ---------------- | -------- |
| 批量投弹         | LB + X   |
| 切到投弹视角     | Y        |
| 切换视角         | RB       |
| 开关炸弹舱门     | LT + RT  |
| 继续 / 确认      | A        |
| 弹射跳伞         | 十字键上 |

若与你的游戏键位不同，可把 `wt_controls/战争雷霆自动炸战区按键.blk` 导入为键位预设，或自行在游戏内调整并同步修改 `common/gamepad.py`。

## 相关说明

- 帧画面底部 40px 会在送入 YOLO 前裁剪，以减少 HUD 误检。
- `x360_keyboard.py` 使用 `pynput` 全局热键，属于独立工具，不参与自动轰炸主流程。
- 详细技术文档见 [CLAUDE.md](CLAUDE.md)。
