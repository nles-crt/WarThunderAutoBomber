# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: WarThunderAutoBomber

Automated bombing bot for War Thunder. Uses YOLO to detect bombing zones on screen, OCR to identify game scenes, and a virtual Xbox 360 gamepad to fly and drop bombs automatically.

## Commands

```bash
# Run the bomber (background, auto-detect game window)
python main.py

# Run the keyboard-to-gamepad utility (pynput global hotkeys)
python x360_keyboard.py

# Dependencies
pip install ultralytics rapidocr_onnxruntime vgamepad opencv-python numpy pywin32 mss torch requests pynput PyQt5

# Offline checks (no game needed)
python tools/namecheck.py --import app.py                   # names + class structure (slow: imports torch/cv2)
python tools/namecheck.py app.py gui.py common/nav_law.py    # AST only, fast
python tools/nav_replay.py selftest                         # nav law: plant sweep
python tools/nav_replay.py replay logs/<run>.log            # nav law: real trajectory
python tools/results_check.py                               # battle-results parser: recorded frames
python tools/scene_check.py                                 # scene keyword table: recorded frames
```

No test framework or build system is configured — this is a runtime-only application.
`PyQt5` is optional: without it `main.py` falls back to a headless idle loop and the bot still runs.

Because so little of this code can be run off-line, `tools/` holds the four things that can be.

**`tools/namecheck.py` is the important one, and it must be run in both of its modes after any
structural edit to `app.py`.** Plain mode is AST-only and catches a misspelled or unimported name
that `py_compile` cannot — this repo shipped one such bug silently (`get_health` at `app.py:868`).

`--import app.py` additionally imports the module and checks every `self.X` against the class,
because the AST mode cannot see a **structural** break. The failure it exists for: a helper written
as `def helper():` at **column 0** but placed *inside* the class body **terminates the class** —
every `def` after it silently becomes a nested function of that helper instead of a method, and the
only symptom is `AttributeError: 'App' object has no attribute ...` at runtime, in flight. It is
valid syntax and every name resolves, so `py_compile` and the AST mode both pass it.

```bash
python tools/namecheck.py --import app.py
```

Neither mode is a substitute for actually flying the thing; they only say the names exist.

## Architecture

### Data Flow

```
main.py → App.__init__() → spawns 5 daemon threads + 1 listener:
  ├─ _state_monitor_loop:  OCR scene detection (target period `scene_scan_interval`, default 3s)
  │   └─ determines: start/stop tracking, open bay, press UI buttons
  ├─ _target_info_loop:    8111 API logging (every 1s) + flight-state sampling for the HUD
  ├─ _recog_loop:          YOLO detection (≤30 fps)
  ├─ _control_loop:        map errors → joystick values (≤33 Hz)
  │    └─ _auto_bomb_sequence: guide → bomb view → aim → drop → navigate
  ├─ _navigate_to_airfield: post-bomb navigation (spawned on demand)
  └─ pynput keyboard.Listener: F12 master switch

main thread → gui.run_gui(app) → QApplication.exec_()
                ├─ MainWindow: param tabs, hot-applies to the running App
                └─ HudWindow:  translucent overlay pinned to the game window
```

### Key Modules

| File | Role |
|------|------|
| `main.py` | Entry — signal/atexit cleanup, hands the main thread to Qt, falls back to idle loop |
| `app.py` | Core orchestrator — all loops, control algorithms, bomb sequence |
| `gui.py` | PyQt5 param window + translucent HUD overlay; hot-applies config to the live `App` |
| `common/config.py` | Loads `config.json` into module-level constants |
| `common/gamepad.py` | `Gamepad` class wrapping `vgamepad` — buttons, joysticks, triggers |
| `common/window.py` | Win32 window search + MSS screen capture |
| `common/utils.py` | `map_val()` — pixel deviation → joystick value with deadzone/sensitivity |
| `common/wt8111.py` | Requests to WT's local `:8111` HTTP API — player pos, airfields, bombing zones |
| `common/nav_law.py` | `NavLaw` — the post-bomb glideslope coupler as pure numerics (no IO, no deps), so `tools/nav_replay.py` can close the loop around the *shipping* law |
| `detect/yolo.py` | Ultralytics YOLO wrapper with `_refine_box_center()` for thin bboxes |
| `common/results.py` | Battle-results OCR parser as pure functions (no IO, no deps), so `tools/results_check.py` can regression-test it against recorded frames — same reason `nav_law.py` exists |
| `detect/scene.py` | OCR via `rapidocr_onnxruntime` — scene classification + UI button triggers |
| `x360_keyboard.py` | Standalone utility — keyboard → virtual gamepad via pynput (not used by bomber) |
| `tools/nav_replay.py` | Offline check for the nav leg — `selftest` (plant sweep, no game) and `replay <log>` (feed a real trajectory through the law). stdlib only |
| `tools/results_check.py` | Offline regression for the battle-results parser — feeds the `战果汇总` frames recorded in `logs/` through `common/results.py`. Stdlib only; skips (not fails) if the fixture logs were cleaned |
| `tools/scene_check.py` | Offline regression for the scene keyword table — picks frames out of the recorded `logs/scenes_*.jsonl` by marker word and asserts what the **current** `detect/scene.py` says about them. Imports `detect.scene`, so it loads cv2/rapidocr; skips (not fails) if the fixture logs were cleaned |

### Live Parameter Tuning (`gui.py`)

`app.py` imports config values **by value** (`from common.config import MIN_V, ...`), so reloading
`common.config` has no effect. But `app.py`'s functions read those names from their own module
globals **at call time**, so `setattr(sys.modules["app"], "MIN_V", v)` takes effect immediately on
already-running threads. That is the whole hot-apply mechanism.

Four values are shadowed by App instance attributes and must be written to the instance instead:
`self.sense`, `self.lock_side`, `self.bomb_cycles`, `self.bomb_count`.
`model_conf` / `model_iou` are YOLO instance attributes (`app.yolo.conf` / `.iou`).

`config.json` is read/written as **raw JSON** (`_`-prefixed documentation keys preserved verbatim);
`common/config.py` reads it once at import and exposes no write path.

### Master Switch (F12)
All gamepad output funnels through the single `Gamepad` class in `common/gamepad.py`, so the
emergency stop is one gate there rather than dozens of call sites in `app.py`:
`Gamepad.enabled` + `set_enabled()`; every output method starts with `if not self.enabled: return`.
`update()` / `reset()` / `halt()` / `destroy()` are deliberately **not** gated — `set_enabled(False)`
leans on `halt()` to recentre the sticks. `App.master_on` mirrors the flag for the HUD; it is
independent of `self.running`, which internal logic writes in several places.

**Inside a sortie, master-off is a pause, not a lost target.** `_control_loop` early-returns while
`master_on` is false and so stops refreshing `_last_aim_state`; the aim loop in
`_auto_bomb_sequence` reads that state, and without a guard it sees a stale frame and — after
`stolen_timeout` — concludes the target was stolen, writing off the round and sending the aircraft
to the airfield to self-destruct. That is the opposite of what F12 means. `App._wait_master_on()`
parks both the guide loop and the aim loop until the switch comes back, and the aim loop resets
`stale_since` on resume so the paused time does not count against the timeout. A master-off that
outlives the sortie (left battle / stopped) returns `False`, and the sequence unwinds **without**
setting `finished` — `finished` means "fly to the airfield", which pressing stop must never reach.

### The HUD must be masked out of the bot's own vision

`App._mask_hud()` blacks out the HUD's rectangle in every captured frame. This is not cosmetic:

`detect/scene.py` needs only a couple of loose keywords to call 战斗中, and the HUD prints exactly
those: `表速: 520km/h`, `高度: 2840m`, `节流阀 100%`, `燃油量 42%`. With the HUD visible, OCR reads
the HUD's own text and the bot believes it is in combat **even while sitting in the hangar** — it
will pull the bay lever and enter bomb view forever. (The 战斗中 anchor gate below narrows *which*
words count, but the HUD prints those too, so it does not make the mask optional.)

`HudWindow._publish_hud_rect()` writes `app.hud_rect` as offsets **relative to the game client
area**, so a moving window can't desync the mask. It is cleared (set to `None`) whenever the HUD is
hidden. Consequences to keep in mind:

- The mask is a plain rectangle, so **a bigger HUD means less of the game the bot can see.** At 720p
  the default HUD covers ~18% of the frame. `show_log_lines: 0`, a smaller `hud_scale`, or another
  `hud_corner` are the knobs for this.
- `_fit_size()` runs in `_tick()` **before** `_reposition()`. Sizing must not lag positioning: if the
  HUD resizes during `paintEvent`, `_reposition` has already computed x from the old width and the
  HUD hangs off-screen for up to a second with a mismatched mask.
- Anything else parked over the game window has the same poisoning effect — including this GUI's own
  `MainWindow`. Keep it off the game area; while the game is occluded the bot reads foreign text and
  will press scene buttons (`[手柄] 点按 X/B/A`) at the wrong time.

### Scene recognition (`detect/scene.py`) — the keyword table is empirical, not guessed

Full-frame OCR, keyword substring match, score arbitration (highest score wins, ties go to whichever
entry comes first in `_SCENES`), then `_stabilize_scene` debounce (2 consecutive matching scans, per
`app.py`'s `SceneDetector(stabilize_frames=2)`). Two mechanisms sit on top of the plain score:

**Anchor gates.** An entry may carry a 4th element, a list of anchor words:

```python
("战斗中", ["km/h", "m/s", "表速", "高度", "速度"] + _COMBAT_ANCHORS, 1, _COMBAT_ANCHORS)
```

A scene with a gate is declared **only if at least one anchor word hits**; the loose words still
count toward the score for arbitration against *other* scenes. This exists because the 战斗中 loose
words are everywhere on the hangar's vehicle stat card — a real hangar frame OCRs as
`最大速度：417 km/h`, `指示空速上限 480 km/h`, `最大带起落架速度(表速)：300 km/h`, which alone is 4
points and wins outright. The anchors (`节流阀 / 燃油量 / 海拔高度 / 雷达高度 / 油温 / 水温 / 弹舱`)
only appear on the in-flight HUD. `gated_out` in the JSONL scene log lists scenes that would have
won but were gated, so "why wasn't this 战斗中?" is answerable from the log alone.

**Decisive scenes (`_DECISIVE`).** 匹配中 and 机组锁定 win on presence, ignoring score. The
matchmaking overlay is drawn *on top of* the hangar, so the hangar's 社区/商店/科技树 still OCR
fine and 大厅 scores 3 while 匹配中 scores 1 — by score, 大厅 always wins, and then `_handle_lobby`
starts pressing X (join battle), which lands on the queue overlay and cancels the match the bot
itself started. Their keywords are specific enough that a bare presence check is safe.

**The debounce used to neuter exactly that, until the raw gate below.** `_stabilize_scene`'s
per-scene counters are cleared only by a frame that *matches the current scene*, not by every
frame — so while the queue overlay and the current scene alternate every other frame (the
overlay is semi-transparent over the hangar, so the same underlying screen keeps re-reading as
大厅 or 未知), 匹配中's counter never reaches 2 and `current_scene` never becomes 匹配中. Every
guard that keys on the settled scene is then bypassed at once. Measured against the shipping
`_stabilize_scene` off-line: with 未知 current and the raw stream alternating 匹配中/未知, the
未知 branch's press *decision* (`_unknown_streak >= UNKNOWN_ACK_STREAK`) fires on 28 of 30
frames — the throttle valve then caps the actual presses at one per `UI_RETRY_S`, but the queue
is already cancelled by the first one. The three-way rotation 匹配中/大厅/未知 does settle, so
the bug is specifically "the current scene keeps reappearing", which is what a semi-transparent
overlay does.

Seven scenes deliberately have **no `SCENE_BUTTONS` entry** — that table is pressed every scan, so
it can only hold keys that are safe to repeat indefinitely. Each owns its cadence in `app.py`, and
every one of their presses goes through the throttle valve (see the next section):

- **匹配中** — presses *nothing*, ever. The only handler action is recording how long the queue
  took; `match_wait_warn` (default 300s) emits one warning line, no key. Any button cancels the
  queue. A scan whose **raw** (pre-debounce) verdict is 匹配中 is forced to 匹配中 in
  `_state_monitor_loop` — one choke point, so every downstream guard is bypassed together rather
  than each re-deciding: the `SCENE_BUTTONS`/`KEYWORD_BUTTONS` skip, `_handle_matchmaking`,
  the 未知 branch's X, and `_handle_lobby`'s X *and* its `self.stop()`. `_handle_lobby`'s own
  `正在等待游戏 in texts` check stays as a second line of defence.
  **The keyword that actually fires is `等待时间`.** The four originals were guesses that had
  never once hit — `正在等待游戏` scored exactly 1 frame in 2450 and that frame was the bot's own
  window. A live sortie finally recorded the panel (`logs/scenes_20260928_175652.jsonl`
  18:05:27 / 18:06:24): OCR reads it as `等待时间 | 0:00 → 0:29 | 当前排队状况: 40 → 28 | 取消`.
  `等待时间` appears in 235 of 2450 recorded frames, every one of them queuing and nowhere else,
  so it is the one word the table can be built on. Two consequences of the original miss, both
  visible in that log: the queue reads as 大厅 (the panel is transparent over the hangar), and
  **`取消` in those frames is the panel's cancel-matchmaking button** — so `_handle_lobby`'s
  "a popup with 取消 → press B" branch was pressing B straight onto it and X was re-joining:
  the bot cancelling and re-queueing its own match, every `UI_RETRY_S`.
- **机组锁定** — the crew-lock screen after a crash. Presses **B once** (guarded by
  `_crew_lock_cancelled`, reset when the scene changes) to dismiss the screen, then waits for the
  aircraft to unlock; it does *not* pick a plane. Picking one would fly a different aircraft than
  the one configured; letting the normal spawn flow take over does not. Repeated B here is a
  key-loop: B is what the screen wants, so the scene never changes and the press never stops.
- **大厅** — the hangar. `_handle_lobby` owns the cadence: one X on entry, then one retry every
  `UI_RETRY_S` (30s). It *used* to be in `SCENE_BUTTONS`, and that alone was the bug —
  a per-scan X means one press per 3s scan, forever. From `logs/scenes_20260928_131739.jsonl`:
  260+ `tap:X` between 13:34:59 and 13:48:02, zero battles entered. The old handler compounded it
  by clearing its own timer only inside the `elapsed > 60` branch, so the 30–60s stretch was also
  pressing every scan; and that branch's cure was a B to "back out to the main menu", which is how
  stray B presses appear in the log. The shared clock gates the press instead, and the only B
  left is the escape from a popup that shows `取消` (B to close it, then X to join).
- **战果汇总** — the results / scoreboard screen. Pulled from `SCENE_BUTTONS` for the same reason
  as 大厅, and it is the sharper case: **X on a results screen is not reliably "advance"**. On the
  post-death overlay it lands on 「加入战斗！」, which raises the 需选载具 popup below.
  `_handle_post_battle` presses the throttled X instead, so the bot still leaves the screen — at
  most once per 30s rather than every scan.
- **需选载具** — the 「开始任务前必须先点选一台可供出击的载具。」 popup: one 确定 button, no other
  choice. It appears when the result overlay's 「加入战斗！」 is pressed on a **non-respawn** map
  after death — the line-up has no aircraft left to spawn. The bot cannot pick one for the player
  (nothing in the vehicle list signals which are available), so `_handle_needs_vehicle` presses
  确定 once and keeps retrying the join on the 30s clock. Deliberately a retry and not a stop:
  stopping parks the whole automation until a human notices.
- **选择新的研发项** — the mods / research screen that appears after a battle
  (`logs/scenes_20260928_131739.jsonl` 13:17:53, `..._114629.jsonl` 11:46:37). `_handle_mods_screen`
  presses **X**. It was **B** until a live report that B does not clear this screen — X is what
  advances it. X here means "confirm the selection", so the bot does pick a research item. That
  is a deliberate trade (a screen that never clears is worse) and one line to put back.
  Four strings exclusive to that screen
  (`选择新的研发项` / `改装件整体研发进度` / `购买所有研发完毕的改装件` / `分配研发点`), any one of which is
  enough (`min_match=1`). `飞行性能` / `生存能力` / `武器配置` are deliberately **not** keywords — they
  are the mod category tabs and the hangar's own mods page carries them too. The contamination
  check that matters is against the results screen, which also carries `载具研发进度` /
  `改装件研发进度` / `研发载具` / `研发改装件` — but none contain 整体, so `改装件整体研发进度`
  cannot steal it.
- **购买改装件确认** — the `还有改装件未购入。要立刻购买吗？` yes/no prompt. `_handle_buy_prompt`,
  **B**: the bot does not spend the player's silver. This is the one screen where X must *not*
  be pressed — X here is "confirm the purchase", and that is 1520 象. `是` / `否` are
  deliberately not keywords — the sample frame OCRs `否` as `香`, so the two buttons are not
  readable well enough to key on.

Both go through `_press_throttled`, so a screen that somehow fails to clear gets one more press
in 30 s rather than a per-scan key loop — the same trade as `_handle_needs_vehicle`.

They are two handlers rather than one, and that is not tidiness: **X means opposite things on
them** — pick a research item on the first, spend silver on the second. One shared
`_handle_leave_screen` had to hard-code a single button for both, which is the bug the split
fixes.

`战果汇总`'s keyword list gained `任务进行中 / 初步战果 / 完整收益` to make it out-score the false
`选择载具` reading (see the 需选载具 bullet above for that chain). Only one of the three could
plausibly appear while still flying, and if it does the frame usually hits a 战斗中 anchor too and
**战斗中 wins the tie** — it sits earlier in `_SCENES`. In all 9 recorded logs that overlap happens
exactly once (`131739#1`: `水温` vs `任务进行中`, both 1 point) and resolves to 战斗中.

`选择载具` is set to `min_match=2` because a hangar frame legitimately hits `历史性能` once
(`空战·历史性能（3.0权重）`) and one point was enough to press X. **This is the one change that can
cost you auto-join-battle** — if the bot stops entering matches, set it back to 1.

Both the 机组锁定 keywords and its B-cancel are **inferred, not observed** — `logs/` has no sample
of that screen. B is cancel / X is confirm throughout this repo. Expect to retune it once the scene
log has a real sample.

### The X-press throttle valve (`UI_RETRY_S` / `App._retry_next`)

Four screens need "press X on a screen I don't fully understand": 大厅 (join battle), 战果汇总
(leave the results), 需选载具 (dismiss the popup) and 未知 (close whatever dialog this is). They
share **one** clock — `App._retry_next`, with `UI_RETRY_S = 30` — through
`App._press_throttled(btn, ...)`, which is the general form. `App._press_x_throttled()` survives
as a one-line wrapper so the four existing call sites did not move. The two screens above press
**X** and **B** respectively on the same clock, which is why the button had to become a parameter
instead of a second timer.

The single clock is the point, not an implementation detail. Per-handler timers were tried and one
of them leaked every time: 大厅's used to be cleared inside its own `elapsed > 60` branch, which
turned "retry after 30s" back into "press every scan" for the 30–60s stretch. **Nothing resets
`_retry_next`**, so there is no state in which a scene change can zero a timer and press again
immediately. The cost is real and accepted: moving between two of those screens, the first press
can wait up to `UI_RETRY_S`. `UI_RETRY_S` is the one knob for all six screens that press through
it.

Two more rules live in the same block of `_state_monitor_loop`:

- **未知 presses X only — never B or A — and only after `UNKNOWN_ACK_STREAK` (3) consecutive
  unknown scans.** It used to add B and A on *every* scan. B is cancel/exit and A is confirm, so on
  a screen the bot has not understood it would quit the battle, cancel the match it had just
  started, or confirm a dialog it should not have; both of the user's "keeps pressing xb" reports
  came from there. X is the safe one — worst case it closes a dialog. `_unknown_streak` now gates
  the press; before, it was incremented and never compared against anything ("统计用").
  **The screens above do not contradict this.** They are *recognised* — they get their own
  scene and their own handler, so the press is a decision with a known meaning, *including*
  which of X or B means "leave" and which means "spend". 未知 is by definition the case where
  no decision is available, and there X stays the only key.
- **Handlers append to `self._actions`** — the same list object the scan loop hands to
  `_scene_log_write` — so the JSONL's `actions` column names every key, including those pressed
  inside a handler. Before this a handler's `gpad.tap()` was invisible in the log and the press had
  to be inferred from context, which is what made the 需选载具 chain take three passes to find.
- **The keyword table is a per-scan table too, and X now goes through the throttle as well.**
  `KEYWORD_BUTTONS["加入战斗"] = "X"` was the second half of the same bug: **the hangar's own
  to-battle button is literally `加入战斗`** (`logs/scenes_20260928_140707.jsonl` idx0/2/3 are
  hangar frames with those four characters in the OCR text), so that entry fired every 3s while the
  bot sat in the hangar. It hid from the first pass because the log filed it as
  `tap:X(加入战斗)`, not `tap:X` — same hole, different label. Keyword presses for X now call
  `_press_x_throttled` with the keyword kept as the action label, so the distinction survives;
  A and B still press directly, because each carries the meaning of its own screen.
  `_handle_select_vehicle`'s X is deliberately **not** throttled and logs itself as
  `tap:X(选择载具)`: on the spawn screen that key *is* 「加入战斗！», and a failed press should be
  retried immediately, not in 30s.
- **A keyword that is a substring of an unrelated on-screen button fires on the wrong screen.**
  `KEYWORD_BUTTONS`'s `研发完毕` → `B` was meant to close the 「该等级的所有改装件均研发完毕！」
  popup, but the mods screen has a button literally labelled
  `购买所有研发完毕的改装件（1100条）`. From `logs/scenes_20260928_131739.jsonl` 13:17:53, that
  frame's `actions` is `['tap:X', 'tap:B', 'tap:A', 'tap:X(选择新的研发)', 'tap:B(研发完毕)']` —
  **X and B at once**, one confirm and one cancel. The screen cleared 3 s later, so the bot's
  escape from it was an accidental misfire, not design. Narrowed to `均研发完毕` (present in both
  popup samples, absent from both mods-screen samples), and the now-redundant `选择新的研发: X`
  entry was deleted. Off-line: `tools/scene_check.py`.

### Scene log (`logs/scenes_*.jsonl`)

One JSON object per scan, written by `_scene_log_write`, gated by `scene_log` / `scene_log_texts`.
The useful columns are `actions` (what was actually pressed this round — `tap:X`, and scene
buttons via `tap:{btn}({kw})`) and `gated_out`; `raw` vs `scene` distinguishes "classified wrong" from "classified right, still
debouncing". Console lines stay short (`_log_decision` prints the score breakdown on change), so the
JSONL is where the OCR full text lives. `scene_scan_interval` is a **target period** — the loop
measures its own elapsed time and sleeps `max(0, interval - elapsed)`, so full-frame OCR (~0.8s) is
subtracted rather than added. `ocr_ms` is logged so you can tell whether 3s is actually reachable.

### Battle results (`logs/results_*.jsonl`)

The scene log above writes **every scan**, so one results screen leaves 8 identical lines buried among
thousands of 战斗中 frames — useless as battle data. This file is the per-battle record: **one line
per results screen**, gated by `battle_log` (default on). Written by `App._battle_log_write`; the
parser is `common/results.py` (pure functions, so `tools/results_check.py` can regression-test it
off-line against real recorded frames).

**One line per screen, not per frame — and that is free, because the screen is static.** In
`logs/scenes_20260928_162128.jsonl` the 8 `战果汇总` frames (16:21:42–16:22:03) have byte-identical
`texts`. The hook therefore fires on `changed` (the scene-entry frame) and needs no dedup state.

The hook is placed **after** the handler dispatch in `_state_monitor_loop`, not inside a handler:
`actions` is already collected by then, so recording the keys alongside the text later needs no
move. `_handle_post_battle` is deliberately untouched.

Two traps, both read off real logs:

- **Do not record the tail of a scene visit.** `_stabilize_scene` needs 2 consecutive scans to
  change the lock, so the last frame of a visit carries the *next* screen's text under the old
  label. The 9th frame of that fixture has `raw: 大厅` but `scene: 战果汇总` and 81 hangar tokens.
  `changed` skips it for free; a per-frame writer would have logged the hangar as a battle result.
- **The bot's own windows poison it, exactly as they poison OCR.** The 3 frames in
  `logs/scenes_20260928_114345.jsonl` were also classified `战果汇总`, and their `texts` are this
  program's own GUI and terminal (`投弹视角距离 (m)`, `python main.py`, even `All 40 assertions
  pass`). Corroborating detail: those 3 frames differ from each other while the real screen's 8 are
  identical — a terminal redraws, a static screen does not. `self_window_hits()` returns the
  **list of markers that hit**, not a bool, so a `"suspect": true` line says which words. It is a
  **flag, not a filter**: the data is always written, `suspect` is there to filter on later. A
  missed mark costs one bad row; a wrong filter would cost a battle.

**The label table is empirical, not guessed.** Every one of the 9 labels in that fixture follows
`值 = 标签后面紧跟着的那个 token`. The same frame has a row of **unlabelled bare numbers**
(855 / 169 / 2676 / `1688条` / 721 / 5871 / 16881) whose labels are not in OCR order — **these are
deliberately not parsed.** Naming them would be inventing data. They stay in `texts`. Same rule as
the scene keyword table: find a real sample in `logs/` before widening `LABELS`. A missing label
drops one field from `fields` and never loses anything, because `texts` is the whole screen.

`battle_log` **hot-applies**, unlike `scene_log`: the file handle is opened lazily inside
`_battle_log_write` and the flag is re-read from the module global on every write, so the GUI
checkbox takes effect on the next battle with no restart — and nothing leaves an empty file behind
when it is off.

### 未知画面记录 (`logs/unknown_*.jsonl`)

`未知` is where every unmodelled screen ends up, and until now it was a black box: the scene log
does write the OCR full text of every frame, but at ~3 MB/hour under a thousand 战斗中 lines,
nobody reads it. **Both scenes added above were dug out of that pile** — that, not a hypothetical,
is the argument for this file. Written by `App._unknown_log_write`, gated by `unknown_log`
(default on), one line per distinct unrecognised *picture*.

**Deduplicated by content, not by scene entry — and that is the whole design.** `changed` (the
scene-entry frame, which is what the battle log uses) would **not** have recorded the mods
screen. From `logs/scenes_20260928_114629.jsonl`:

| t | `scene` | `raw` | tokens | what it was |
|---|---|---|---|---|
| 11:46:34 | 未知 | 未知 | 6 | the 「该等级的所有改装件均研发完毕！」 popup |
| 11:46:37 | 未知 | 未知 | 46 | the mods screen |
| 11:46:43 | 未知 | 未知 | 4 | the popup again |

Three consecutive frames, all `未知` in both `scene` and `raw`, with the content changing in the
*middle* of the streak. `changed` fires on the transition *into* 未知, which happened at 11:46:34 —
so the one frame that mattered would have been skipped. The key is the `md5` of the joined
`texts`, compared against `_unknown_last_md5`, which is **never reset on a scene change**: the
file then reads as "every screen the bot failed to recognise, in first-appearance order".
Checked off-line against the real logs: the 279 recorded 未知 frames collapse to 108 lines, no
two adjacent lines alike, and the 139-frame run below becomes **one** line.

Each line carries `raw` / `scores` / `gated_out` besides the text, and those three are what make
it answerable rather than merely informative: `scores` says how many points short the right scene
was, `gated_out` says whether an anchor gate rather than the score was what stopped it. `actions`
is there too, and it is **why the hook sits after the generic-button block in
`_state_monitor_loop` rather than inside a handler**: only by then has every key pressed this
round been appended, and "what did it press on the screen it did not understand" is the first
question anyone asks of a 未知 frame.

That question is not rhetorical. In `logs/scenes_20260928_131739.jsonl`, 139 consecutive frames
from 13:48:05 were all the same unrecognised purchase dialog, and the only way it was ever
diagnosed was its `actions` column reading `[tap:X, tap:B, tap:A]` on **every** frame — the old
未知 handler. Seven minutes of the bot mashing X, B and A at a dialog. (Current code presses X
only, throttled, after `UNKNOWN_ACK_STREAK`.) The text identifying that dialog was in the scene
log the whole time; it was just unreadable at that size. That is the failure this file prevents.

Like `battle_log`, `unknown_log` **hot-applies**: the handle is opened lazily inside
`_unknown_log_write` and the flag is re-read from the module global on every write, so unticking
it in the GUI stops it at once and leaves no empty file behind when it is off.

**This file and `tools/scene_check.py` read the same `logs/`.** Both it and `results_check.py`
skip rather than fail when the logs have been cleaned.

### Stopping the program (Ctrl+C)

Running the bot under Qt means the main thread sits in `QApplication.exec_()`, which is C code. Python
signal handlers only run when the interpreter executes bytecode, so **installing a handler alone is
not enough — Ctrl+C appears to do nothing and the process keeps flying.** `gui.run_gui()` therefore
adds an empty 200 ms `QTimer` to keep returning to Python, and routes `SIGINT` / `SIGTERM` /
`SIGBREAK` to `qapp.quit()` rather than `main.py`'s `sys.exit()` (raising `SystemExit` out of a Qt
slot is undefined behaviour). `exec_()` then returns normally and `main.py`'s `finally` runs
`app.stop()`; the virtual gamepad is released by the `atexit` hook.

### Control Modes (in app.py `_control_output`)

1. **track** — normal chase: left stick = roll/yaw, right stick = camera
2. **guide** — approach phase: same as track, full throttle
3. **bomb_coarse** — in bomb view but not yet fine-aiming
4. **bomb_fine** — bomb sight PID-style fine correction (left stick = aircraft, right stick = sight)

### The dive positive feedback (and why there is no `dive_angle` parameter)

**There is no dive-angle parameter, and adding one back is the wrong fix.** A `dive_angle` /
`dive_gain` / `dive_alt_full` triple (fed by `_guide_pitch()`) existed once and was removed —
here is the reasoning, so it doesn't get re-added.

Guide-phase tracking is pure visual servoing: it pulls the bombing-zone box toward screen centre.
But the zone sits **on the ground**, so pointing the nose at it *is* diving — and at a fixed
altitude, **the closer you get, the steeper the angle required to keep pointing at it**. That is a
positive feedback loop, not a setpoint:

```
target far  ← zone slightly below reticle
target near ↓ zone drops further below the reticle
             ↓   nose pushed further down
             ↓      steeper still → overspeed → airframe breaks up
```

Center the target ⇒ the dive angle grows without bound. No fixed `dive_angle` can counteract it:
whatever angle you pick, the loop drives past it as the range closes.

The fix is in `_tracking_stick` (constants `GUIDE_DIVE_BAND` / `GUIDE_DIVE_FADE`, `app.py:70-71`):
once the target has dropped more than `GUIDE_DIVE_BAND` of the frame height below the reticle, the
nose-down component of the right stick is tapered linearly to zero over the next `GUIDE_DIVE_FADE`.
The aircraft **holds its current attitude** rather than chasing the target further down — the way a
human dive-bombs — and the rest of the dive profile is established in bomb view.

Three details that are load-bearing:

- The taper runs **after** the existing clamp and the `MIN_V * 0.35` minimum-output floor. Put it
  before them and the floor immediately re-lifts the nose-down command back up, cancelling it.
- Only the **nose-down** side is tapered (`raw_ry < 0 and dy > 0`). The nose-up side keeps full
  authority — there must always be enough elevator to pull out.
- The same factor also bleeds `_track_iy`, the pitch integrator. Without that it winds up against
  the held-down output (`_track_iy` only decays when `|dy| <= 3`, which is exactly *not* the case
  here) and later releases its full 0.18 as a hard nose-down jolt. Anti-windup is part of the fix,
  not an add-on.

Sign convention: screen Y grows downward, so `dy = ty - ay > 0` means the target is *below* the
reticle, and `map_val(-dy, ...)` returns `ry < 0` = nose down. Same sign on both ⇒ "pushing steeper".

### Dive-angle and airspeed warnings (`dive_angle_warn` / `max_ias_warn`)

**Display only.** They colour the HUD row red and never enter a control value — over-limit is a
warning, not an intervention. That is a deliberate split, so don't wire them into the controller.
`/state` has **no pitch/attitude field**; the *actual* dive angle shown on the HUD is derived by
`_update_flight_state()` from the altitude delta between samples over the horizontal distance
covered (`atan2(-Δalt, horizontal displacement)`, horizontal distance scaled by `get_map_size()`).
It is instrumentation, never feedback.

### 飞往机场：为什么是闭环下滑道

The post-bomb leg (`App._navigate_to_airfield`) was rewritten from an open-loop timer into a
glideslope coupler. The trigger was 「俯冲过头了，而且并不是微调俯冲，航线也有问题」 — and it
was a structural defect, not tuning. Three things were wrong at once, and each is worth keeping
in mind before "just changing a gain".

**① The loop runs at ~6 Hz, not 0.25 Hz — and the reason is a Windows bug, not the game.**
The old code measured 2.0–2.2 s per `/:8111` request, which is why its gains assumed ~4 s of dead
time. That was `localhost` resolving to `::1` first: the game listens on IPv4 only, and `::1`
sends neither RST nor SYN-ACK, so `socket.create_connection`'s per-address loop burns the whole
timeout before falling through to `127.0.0.1`. Measured: `curl localhost` 226 ms, `curl -6
localhost` hangs 2.04 s, `curl 127.0.0.1` 24 ms. `BASE_URL` is now `http://127.0.0.1:8111` and
a request costs ~30 ms. (A keep-alive `Session` and a `/map_info.json` TTL cache were tried and
**reverted** — at 33 ms neither pays for itself, and a cache risks serving a stale map size
across battles.) Everything in this leg is therefore written to be correct at *any* dt: gains in
1/s, not "per step".

**② The vertical channel must be closed, and the old one was not closed at all.** It emitted
`-pitch / 0 / -0.6` and never a nose-up value, so an over-steep dive was irreversible. In
`logs/20260928_152045.log` a commanded 14° flew as an actual 36° descent with `ry` pinned at
−0.28, and the same command sequence in `..._145602.log` gave 12°→28°→34°→**6°**. The second
half of the illusion: the number printed as 「俯冲角」 was the *desired line-of-sight angle to the
airfield*, never the aircraft's flight-path angle. That is why the log now prints **航迹** and
**计划** as two separate numbers — if you are reading one angle, you are reading the wrong one.

The law lives in `common/nav_law.py`, not inline in `app.py`, and that is load-bearing: the
offline tool must drive *the code that actually flies* (`tools/nav_replay.py`), or it only proves
that two similar-looking formulas both converge. Three rules inside it:

- **No integral term.** Sample period and plant are both uncertain; an I term is a guaranteed
  oscillation. Do not add one back.
- **Pull authority exceeds push authority** (`nav_pitch_pull_max` > `nav_pitch_push_max`) —
  there must always be enough elevator to pull out. Same principle as the guide dive taper.
- **The rate term is the main path-angle actuator, the position term is the trim.** `nav_pitch_ka
  = 0` leaves a 1.3 km steady-state altitude error — a pure rate loop can never correct position,
  so the position term is structurally required, not decoration. The two time scales sit a decade
  apart (rate ~seconds, position ~15 s), which is why they do not fight.

**The gains are simulation-calibrated, not hand-guessed.** The originally-planned `nav_pitch_kv
= 0.015` porpoises in the offline sim — flight-path angle swinging 0→30° around an 18° plan, i.e.
exactly the reported 「不是微调俯冲」. The stable boundary sweeps out near 0.005; 0.003 ships with
2× margin. `nav_pitch_ka = 0.002` cuts the arrival residual from 152 m to 74 m and still does not
oscillate against the fastest plant tried. **`tools/nav_replay.py selftest` must pass before
either is raised** — the sim is a *hypothesis* about the plant, not a measurement, so treat the
first sortie as a calibration flight and read 航迹/计划/高差/vs off the log.

**③ The throttle is an airspeed loop now, not a distance ramp.** It used to be
`*= (dist_m - 5000) / 5000`, cutting power toward zero at 5 km regardless of altitude or speed —
so an aircraft already below the glideslope got *less* power and sank further. Throttle is the
dominant path-angle actuator under a mouse-aim instructor (the same `ry` gave −3°/+35°/+5.6° at
throttle 0.80/0.43/0.00), so it now holds IAS and takes a bounded trim from the sink-rate error
(±0.16 cap — it trims, it does not rescue). `nav_throttle_min` is a floor for the same reason:
「油门 0.00」 at 5 km out is how the second incident turned into a stall.

**④ The aileron feed defaults to off (`nav_aileron_gain = 0`).** `track`/`guide` mirror the
lateral command to the left stick; this leg deliberately does **not** by default. The left/right
X signs are independently flippable, mirroring fights the mouse-aim instructor, and it would
invalidate the turn-rate calibration `nav_turn_rate_max` rests on. One line to turn on once
verified in-game.

**⑤ Arrival is a level-off flyover, not a crash and not a freeze.** `dist_m <= arrival_m`
(`airfield_arrival_ratio × map_size`) sets `ry_t = 0`, `rx = 0` and skips the lateral/vertical
loops entirely — chasing a line-of-sight angle as `dist → 0` blows up in `atan2`. The intent per
the existing comment is "low and slow so the AA gets it", which is a flyover, not a dive into the
ground. If you want an actual terminal dive, that is a new feature (a last-kilometre branch), not
a tweak to this one.

**⑥ `self.running = False` during this leg is deliberate.** It stops `_control_loop` and
`_recog_loop` from fighting this loop for the sticks — this leg is the *only* writer. Do not
"fix" it. F12 is honoured inside the loop (it logs 「主开关关闭，停止导航」 and breaks), so the
master switch still stops the aircraft here.

The `nav_*` knobs are in `config.json` with `_nav_*` siblings; the five worth touching from the
GUI are in the 导航 group (`nav_pitch_kv`, `nav_pitch_ka`, `nav_yaw_kp`, `nav_yaw_kd`,
`nav_ias_ref`). The rest are config-only on purpose — see the table under "Configuration".

### Aggression System

`self.aggression` starts high (0.7) during guide phase, decays toward 0. Controls how aggressively the tracking stick responds:
- `thresh = 0.015 + agg * 0.025` — deadzone shrinks as agg drops
- `scl = 0.10 + agg * 0.15` — max stick output decreases over time

### Bomb Sequence (`_auto_bomb_sequence`)

1. **Guide** — full throttle, track target until `distance ≤ bomb_view_distance` (8111) or `bbox ratio ≥ guide_box_ratio` (fallback). Under F12 it parks here (see "Master Switch").
2. **Bomb View** — open the bomb bay (LT+RT, **once per sortie** — see below), press Y, switch to bomb sight, close the weapon selector (`_close_weapon_selector`, **here** — not at drop time), fine-aim with damped PI controller.
   Losing sight of the target does **not** exit the view: the aim loop stays in the sight and keeps
   re-acquiring while it waits out `stolen_timeout` (default 20 s). That window has to cover a dive
   — the successful run in `logs/20260928_125002.log` needed 36 s of in-sight tracking before the
   drop — and the old 5 s value was the direct cause of the view being entered and abandoned over
   and over. Note the timeout fires on any dropout, not only on a teammate stealing the zone: a
   YOLO miss or a box under 25 px looks the same from inside the aim loop.
3. **Drop** — LB+X when aligned (dx ≤ pixel_deviation, dy ≤ pixel_deviation_y), or on crossing zero. Nothing else: `_drop_bombs` presses LB+X and nothing more.
4. **Post-drop** — exit bomb view, -50% throttle, optionally navigate to enemy airfield
   (a closed-loop glideslope coupler — see 「飞往机场：为什么是闭环下滑道」)

### The bomb bay opens on entering bomb view, once per sortie

LT+RT is a **toggle** (`ID_BAY_DOOR`) with no reliable state readback — the HUD draws it as an
icon and 8111 doesn't expose it — so the code pulls it exactly once per sortie (`_bay_pulled`),
and the only real decision is *when*. It used to be at combat entry, gated on OCR seeing `炸弹`
and not `开启`. It is now at bomb-view entry, immediately before the Y tap.

Two reasons. Flying the whole approach with the doors open is pure drag, and the approach can run
past 100 s (`logs/20260928_125002.log`); the doors are useless until the release, and bomb-view
entry is still ~28 s ahead of the drop (measured), plenty of time for them to finish opening. And
it drops the OCR dependency, which was the fragile half: the `is_open` check re-evaluated on every
scan, so any frame that missed `开启` produced a *second* pull — which closes the doors it had just
opened.

The pull is guarded on `master_on`, because `Gamepad.pull_bay_lever()` starts with the usual
`if not self.enabled: return`; without the guard, a pull during F12-off would still set
`_bay_pulled` and the bay would stay shut for the rest of the sortie.

**Known gap:** `_bay_pulled` is per sortie, not per round, so round 2 of a multi-round sortie
enters bomb view without re-opening. If the aircraft closes its doors on release, round 2 drops
nothing — either keep `bomb_cycles: 1` or re-arm the pull per cycle. Do not simply delete the flag:
every extra pull is a coin flip on the doors.

### Why the weapon selector is closed at bomb-view entry, not at drop time

`wt_controls/*.blk` binds (joyButton index → action; cross-checked against the three
bindings the code already depends on — `press_lb_x`, `pull_bay_lever`, the Y bomb-view tap):

| joyButton | button | actions |
|---|---|---|
| 15+22 | **Y+LB** | `ID_OPEN_VISUAL_WEAPON_SELECTOR` (a combo) |
| 22+28 | **LB+X** | `ID_BOMBS_SERIES` (the drop) |
| 27 | **B** | `ID_EXIT_SHOOTING_CYCLE_MODE` **and** `ID_TACTICAL_MAP` |
| 29 | Y | `ID_CAMERA_BOMBVIEW` |
| 30+31 | LT+RT | `ID_BAY_DOOR` |

**B is the key that must not be pressed next to the drop.** It is double-bound, so a B tap
immediately before the LB+X burst is a second action racing the drop. That is where
`_close_weapon_selector` used to live (0.25 s before the burst) and it now runs once at
bomb-view entry instead — which is both ~28 s before the drop (measured,
`logs/20260928_111150.log`) and naturally once per cycle, since a cycle enters bomb view
exactly once. `_drop_bombs` is kept to LB+X and nothing else.

Note the selector itself is `Y+LB`, and `press_lb_x()` presses LB+X without Y, so the drop
burst does not open it. If a burst still stops after the first bomb, the cause is elsewhere —
do not "fix" it by adding keys back into `_drop_bombs`.

B's `ID_TACTICAL_MAP` is mitigated by `USEROPT_HOLD_BUTTON_FOR_TACTICAL_MAP:b=yes` (hold opens
the map, `tap(B, 0.1)` should not). **This was never verified in-game** — if the tactical map
appears on entering bomb view, that is the cause.

### There is no bomb-count reader any more

One used to live in `detect/scene.py` (`_extract_bomb_count`) and drove whether a sortie was
over. It was deleted: it never decided anything useful, and the logs showed it could not even
read the value it was built for (it reported `8[弹舱】` before *and* after a drop). **How many
rounds to fly is `bomb_cycles`; nothing OCR reads feeds that decision.** The HUD prints the bay
as `8/8[弹舱]`, and `弹舱` is still load-bearing — but only as an anchor word for the 战斗中
gate (see above), never as a number.

### 8111 API Module (`common/wt8111.py`)

- Requires War Thunder local HTTP server on `localhost:8111`
- Optional import — `app.py` checks `WT8111_OK` flag
- Provides: player position/heading, altitude, bombing zone targeting, airfield detection
- Coordinates normalized 0-1, converted to world coordinates via `map_info.json`

## Configuration

All tunables in `config.json` (no code changes needed). Key parameters:
- `sense` / `min_v` — stick sensitivity curve
- `pixel_deviation` / `pixel_deviation_y` — bomb aim deadzone
- `bomb_view_distance` — when to enter bomb view (meters via 8111)
- `fly_to_airfield` — optional post-bomb navigation
- `airfield_arrival_ratio` — arrival radius as a fraction of `map_size` (default 0.016 ≈ 1 km on a
  65536 m map). Below this the leg goes to the level-off flyover branch.
- `nav_*` — the post-bomb glideslope coupler (see 「飞往机场：为什么是闭环下滑道」). All hot-apply:
  `nav_turn_rate_max` / `nav_yaw_kp` / `nav_yaw_kd` / `nav_yaw_dz` / `nav_omega_tau` / `nav_yaw_max`
  (lateral rate-command PD), `nav_pitch_kv` / `nav_pitch_ka` / `nav_pitch_push_max` /
  `nav_pitch_pull_max` / `nav_pitch_rate` (vertical), `nav_throttle_nom` / `nav_throttle_k` /
  `nav_throttle_kv` / `nav_throttle_min` / `nav_ias_ref` (airspeed loop),
  `nav_aileron_gain` (**default 0 = off**, see ④), `nav_diff_window` (differencing window; 1 s
  because `/state` altitude is 1 m quantized and the loop is ~6 Hz), `nav_target_alt`.
  Only the five in the GUI's 导航 group are meant to be touched by feel; the rest want the
  offline sim first.
- Sign-flip params (`*_x_sign`, `*_y_sign`) — fix inverted controls per mode
- `dive_angle_warn` / `max_ias_warn` — HUD red-line thresholds, display only
- `hud_*` / `show_log_lines` — HUD position, opacity, scale, manual offset
- `follow_window` — re-read the game window rect every second and write it back to `app.rect`
  (default on). Off restores the old one-shot snapshot behaviour if the write-back misbehaves.
- `scene_scan_interval` — scene-scan **target** period in seconds (default 3.0). Full-frame OCR time
  is subtracted from it, not added. See "Scene log".
- `scene_log` / `scene_log_texts` — write `logs/scenes_*.jsonl`, and whether OCR full text goes in
  it. `scene_log` is read at `App.__init__` (**restart required** to turn it on); `scene_log_texts`
  is read per write and hot-applies.
- `match_wait_warn` — seconds in queue before one warning line (default 300, 0 = never). Records
  only; it never presses a key. **Hot-applies** (GUI 「排队超时警告 (s)」, 场景 group). The clock
  starts when the *raw* verdict first reads 匹配中 (`_queue_since`), i.e. when the overlay
  appears rather than when it finally settles — before the raw gate above, that timer was never
  started at all, so this setting could not fire in the one case it exists for.
- `battle_log` — write `logs/results_*.jsonl`, one line per results screen (see "Battle results").
  **Hot-applies**, unlike `scene_log` next to it: the handle is opened lazily and the flag re-read
  per write, so toggling it in the GUI takes effect on the next battle.
- `unknown_log` — write `logs/unknown_*.jsonl`, one line per distinct screen the classifier could
  not place (see "未知画面记录"). **Hot-applies**, same lazy-handle mechanism. Default on: it is
  the only source of new keywords for a table that is empirical and can only be widened from
  real recorded samples.
- `hotkey_master` — master-switch key (pynput name, e.g. `f12`); **restart required** to change
- `auto_bomb` — **restart required** (read once in `App.start()`, `app.py:261`)

Not in `config.json`: the anti-feedback taper is `GUIDE_DIVE_BAND` / `GUIDE_DIVE_FADE` in `app.py`
(see "The dive positive feedback"). They are code constants, not tunables — there is deliberately
no dive-angle knob to turn.

Every key has a sibling `_<key>` string documenting it; GUI saves must preserve those keys verbatim.

## Notable Details

- Frame bottom 40px cropped before YOLO to reduce HUD false positives
- `_refine_box_center` fixes oblong YOLO bboxes by finding densest pixel region inside
- PI controllers with slew-rate limiting on both tracking and bomb-aim loops
- Gamepad cleanup registered via `atexit` + `signal` handlers in `main.py`
- `x360_keyboard.py` is a separate keyboard → gamepad tool, NOT part of the auto-bomber flow
- The HUD window uses `Qt.WindowTransparentForInput` + `Qt.WindowDoesNotAcceptFocus` so clicks pass
  through to the game, and is positioned with `win32gui.SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)`.
  **Exclusive fullscreen cannot be overlaid by any topmost window** — use borderless windowed mode.
- No DPI awareness is set anywhere in this repo, and none should be added: `SetProcessDpiAwareness`
  would change the coordinate space used by `mss` capture and `get_rect()`, breaking working
  recognition. HUD misplacement under non-100% scaling is compensated with `hud_offset_x/y` instead.
- Import order matters: `PyQt5` must be imported **after** `app` (which loads `cv2`). Both ship Qt
  platform plugins and importing Qt first breaks `cv2`'s.
- `Gamepad` keeps a module-level `_gpad_ref`; do not create a second `vgamepad` instance
  (`x360_keyboard.py` does, and would conflict with a running `App`).
