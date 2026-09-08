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
pip install ultralytics rapidocr_onnxruntime vgamepad opencv-python numpy pywin32 mss torch requests pynput
```

No test framework or build system is configured — this is a runtime-only application.

## Architecture

### Data Flow

```
main.py → App.__init__() → spawns 4 daemon threads:
  ├─ _state_monitor_loop:  OCR scene detection (every 10s)
  │   └─ determines: start/stop tracking, open bay, press UI buttons
  ├─ _target_info_loop:    8111 API logging (every 1s, optional)
  ├─ _recog_loop:          YOLO detection (≤30 fps)
  └─ _control_loop:        map errors → joystick values (≤33 Hz)
       └─ _auto_bomb_sequence: guide → bomb view → aim → drop → navigate
```

### Key Modules

| File | Role |
|------|------|
| `main.py` | Entry — signal/atexit cleanup, instantiates `App` and idles |
| `app.py` | Core orchestrator — all loops, control algorithms, bomb sequence |
| `common/config.py` | Loads `config.json` into module-level constants |
| `common/gamepad.py` | `Gamepad` class wrapping `vgamepad` — buttons, joysticks, triggers |
| `common/window.py` | Win32 window search + MSS screen capture |
| `common/utils.py` | `map_val()` — pixel deviation → joystick value with deadzone/sensitivity |
| `common/wt8111.py` | Requests to WT's local `:8111` HTTP API — player pos, airfields, bombing zones |
| `detect/yolo.py` | Ultralytics YOLO wrapper with `_refine_box_center()` for thin bboxes |
| `detect/scene.py` | OCR via `rapidocr_onnxruntime` — scene classification + UI button triggers |
| `x360_keyboard.py` | Standalone utility — keyboard → virtual gamepad via pynput (not used by bomber) |

### Control Modes (in app.py `_control_output`)

1. **track** — normal chase: left stick = roll/yaw, right stick = camera
2. **guide** — approach phase: same as track, full throttle
3. **bomb_coarse** — in bomb view but not yet fine-aiming
4. **bomb_fine** — bomb sight PID-style fine correction (left stick = aircraft, right stick = sight)

### Aggression System

`self.aggression` starts high (0.7) during guide phase, decays toward 0. Controls how aggressively the tracking stick responds:
- `thresh = 0.015 + agg * 0.025` — deadzone shrinks as agg drops
- `scl = 0.10 + agg * 0.15` — max stick output decreases over time

### Bomb Sequence (`_auto_bomb_sequence`)

1. **Guide** — full throttle, track target until `distance ≤ bomb_view_distance` (8111) or `bbox ratio ≥ guide_box_ratio` (fallback)
2. **Bomb View** — press Y, switch to bomb sight, fine-aim with damped PI controller
3. **Drop** — LB+X when aligned (dx ≤ pixel_deviation, dy ≤ pixel_deviation_y), or on crossing zero
4. **Post-drop** — exit bomb view, -50% throttle, optionally navigate to enemy airfield

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
- Sign-flip params (`*_x_sign`, `*_y_sign`) — fix inverted controls per mode

## Notable Details

- Frame bottom 40px cropped before YOLO to reduce HUD false positives
- `_refine_box_center` fixes oblong YOLO bboxes by finding densest pixel region inside
- PI controllers with slew-rate limiting on both tracking and bomb-aim loops
- Gamepad cleanup registered via `atexit` + `signal` handlers in `main.py`
- `x360_keyboard.py` is a separate keyboard → gamepad tool, NOT part of the auto-bomber flow
