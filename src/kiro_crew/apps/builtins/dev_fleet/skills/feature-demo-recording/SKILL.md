---
name: feature-demo-recording
description: Record a polished headless-browser demo video of a web feature (mouse-follow cursor, caption cards, scene script) and deliver it to Slack. Use when the user asks to "record a video / demo / screen recording" of a feature, dashboard, or web UI flow.
---

# Feature Demo Recording

Produce a **polished, narrated, Screen-Studio-style** recording of a web feature using headless
Playwright — an injected cursor that follows the real mouse, click ripples, premium caption
cards, and an **automatic spring-eased zoom (punch-in) on every click** plus **dead-air
trimming** — then transcode to mp4 and (optionally) send it to Slack.

This skill is the distilled, reusable version of the Session Grid demo. The hard parts
(cursor overlay that survives navigation, caption styling, picking the *right* webm, auth, the
"don't shoot Chinese/PII into the frame" rule, **auto-zoom camera + spring easing + dead-air
trim**) are already solved in `references/`. **You write a thin scene script; the harness records
an event log, and a pure-Python post-processor turns it into the cinematic cut.**

The auto-zoom/easing/trim technique is ported from the open-source
[`preston176/screen-demo-skill`](https://github.com/preston176/screen-demo-skill) (Playwright +
Remotion), but re-implemented **fully local in Python** (Pillow + imageio-ffmpeg) — **no Node,
no Remotion, no Steel cloud browser**, so internal dashboards never leave the machine.

---

## When to use

- "Record a video of <feature>" / "take a screen recording" / "make a demo"
- "Show off <dashboard flow> and send it to me on Slack"
- Any time the deliverable is a **video walkthrough** of a live web UI.

Not for: static screenshots (just use `browser_take_screenshot`), or recording a native
desktop/terminal app (this is browser-only).

---

## The flow (6 steps)

```
1. SETUP    — one-time: venv with Playwright + Chromium + Pillow + h264 ffmpeg (references/setup.sh)
2. SCRIPT   — write scenes: a list of (caption, action) using the harness API
3. AUTH     — get a fresh tokenized dashboard URL (kirocrew token, or ask the user)
4. RECORD   — run the script headless → webm + events.json  (cursor/captions/video + event log)
5. POLISH   — render.sh: auto-zoom (spring punch-in on clicks) + dead-air trim → demo.mp4
6. SHIP     — file_send the mp4 to Slack (or hand back the path)
```

Track these as todos if the request is non-trivial — RECORD often needs 2-3 iterations to get
selectors right, and you don't want to lose the POLISH/SHIP steps.

**Two-layer design:** RECORD produces the raw webm **and** an `events.json` (every click's
timestamp + on-screen focal point + element size; every caption's span). POLISH replays that log
to drive the camera — so the cinematic part is deterministic, re-runnable, and tunable without
re-recording.

---

## Use subagents — delegate aggressively, work in parallel

Demo work decomposes cleanly into independent pieces. **Default to spawning subagents** for
anything that can run on its own; reserve the main thread for orchestration, the live
token-bearing recording, and final judgment. Spawn with `spawn_sub_agents`
(blocks, returns results — best when you need the output to continue) or
`spawn_run` with a `tasks` array (fire-and-wait for completion events).

**Where subagents help most (run these in parallel):**

1. **SCRIPT — scene authoring, fanned out.** Give each subagent the feature's scene list and have
   each draft 1-2 scenes (selectors + caption text) against the harness API, then merge the best.
   For a wide feature, one subagent per scene; for a tour, split by phase.
2. **SCRIPT — selector scouting.** Before writing clicks, spawn a subagent to open the app
   (read-only / its own token) and report the real `aria-label`/`title`/text for each control you
   need to target. Selector misses are the #1 cause of re-records — scout them first.
3. **REVIEW — adversarial check of the scene script** (do this *before* the expensive RECORD).
   Spawn a reviewer subagent to read `record.py` against this SKILL's scene-design rules and flag:
   English-only captions? PII/real-data risk in any scene? preconditions seeded before dependent
   actions? selectors as ordered lists? captions short enough? It returns a checklist; you fix
   before recording. Cheaper than discovering issues in the webm.
4. **VERIFY — post-render QA.** After POLISH, spawn a subagent to sample frames from the mp4 and
   confirm: punch-in zoom present on clicks, captions not occluding, no PII visible, duration sane.
   (This is the pixel-proof step — delegate it so the main thread can prep delivery.)
5. **RESEARCH — technique upgrades.** When asked to make the video cooler, spawn subagents to
   survey open-source approaches (e.g. how this skill's auto-zoom was ported from
   `preston176/screen-demo-skill`) in parallel with implementation.

**Division of labor, concretely:**

| Stage | Main thread | Subagents (parallel) |
|-------|-------------|----------------------|
| SCRIPT | merge + own the final `record.py` | draft scenes; scout selectors |
| REVIEW | apply fixes | adversarial scene-script review |
| RECORD | run it (holds the live token) | — (single, stateful, token-bearing) |
| POLISH | run `render.sh`, pick params | — (fast, local) |
| VERIFY | decide pass/fail | sample frames, check zoom/occlusion/PII |
| SHIP | `file_send` | draft the Slack message text |

**Keep on the main thread:** the actual RECORD run (it holds the short-lived token and is a
single stateful browser session — don't fan that out), and any step that *uses* the credential.
Hand subagents read-only or compute-only work; never pass a live token into a subagent prompt.

---

## Step 1 — Setup (one-time per machine)

Run the setup helper. It creates `~/.kiro/crew/workspace/.demo-recording-venv` with Playwright,
and reuses the already-installed browsers in `~/.cache/ms-playwright/` (they are typically
already present from prior Playwright usage, so this is usually instant).

```bash
bash <app-skills-dir>/feature-demo-recording/references/setup.sh
```

It prints the venv's python path and the bundled ffmpeg path. If the venv already exists it's a
no-op. See `references/setup.sh` for what it checks.

**ffmpeg**: Playwright bundles one at `~/.cache/ms-playwright/ffmpeg-*/ffmpeg-linux`. The setup
script finds it and writes the path to `.demo-recording-venv/FFMPEG_PATH`. No separate install.

---

## Step 2 — Write the scene script

Copy `references/record_template.py` to your working dir (e.g.
`~/.kiro/crew/workspace/uploads/<feature>-video/record.py`) and fill in the `SCENES`.

The harness gives you a `Demo` object with these methods (full reference:
`references/demo_harness.py` docstrings):

| Method | What it does |
|---|---|
| `d.caption(eyebrow, title, sub="", secs=3)` | Show a caption card, hold `secs`. **Captions are the narration.** |
| `d.cap_hide()` | Hide the current caption (before an action you want unobstructed) |
| `d.click(selectors, label="")` | Glide the cursor to the first visible match and click. `selectors` = list, tried in order |
| `d.click_side(selectors, side, label)` | Click the left-most / right-most match (for distinct panes/columns) |
| `d.type(text, delay=35)` | Type into the focused element (with the cursor parked there) |
| `d.press(key)` | Keyboard press, e.g. `"Enter"`, `"Meta+d"` |
| `d.focus_composer()` | Click the first visible `textarea` / contenteditable |
| `d.wait(ms)` | Plain wait |
| `d.shot(name)` | Debug screenshot -> `debug-<name>.png` (use liberally while iterating) |
| `d.goto_nav(text)` | Click a top-nav item by exact text (e.g. `"Schedule"`, then `"Chat"`) |

A scene is just calls in sequence. Example (one scene):

```python
d.caption("01 - Split", "Split a chat",
          "Press Cmd+D, then pick another session to view them side-by-side.", secs=3)
d.click(['[aria-label="Enter split view"]', '[title*="Split view" i]'], label="enter split")
d.wait(1500)
d.shot("split-opened")
```

The harness automatically:
- injects the **cursor + click-ripple + caption** overlay and **re-injects on every navigation**,
- seeds `localStorage` (`kc-onboarded=1`) so the theme modal never appears,
- records video at 1600x1000,
- **logs an event for every click** (timestamp + focal point = the exact click coords + the
  clicked element's size) **and every caption** (its on-screen span) -> `events.json`,
- at the end, picks the correct webm **by modification time >= run start** (see the hard-won
  lesson below), prints `MAIN_WEBM: <path>`, and writes `events.json`.

You don't call any zoom API in your scenes — just `d.click(...)` and `d.caption(...)` as usual.
The POLISH step reads `events.json` and adds the punch-in zooms automatically.

### Scene-design rules (learned the hard way)

1. **Captions are English-only and describe what's on screen right now.** They are *your* cards,
   not app content — keep them in English even if the app UI is in another language.
2. **Never let real user data into the frame.** If the app shows a session list / history /
   inbox with PII or non-demo content, either (a) create fresh demo data first, (b) filter/search
   to demo-only rows, or (c) hide just those scroll regions via injected CSS. The harness exposes
   `extra_init_css` for (c) — see template. Prefer creating fresh demo state over hiding.
3. **Pre-seed any state an action depends on.** The Session Grid "Fork" button was a no-op
   because it cloned the *active* session, which was an empty new chat. Fix: seed the source
   with a real message *before* the fork scene, focus that pane, and assert the button is enabled.
   Generalize: if an action needs preconditions, set them up in an earlier scene.
4. **Selectors: pass a list, most-specific first.** Prefer `aria-label`, then `title`, then
   visible text (`button:has-text("...")`). Always `d.shot()` after a click while iterating so you
   can see what actually happened.
5. **Pace for humans.** ~2-4 s per caption, ~600 ms settle after each click. A 6-scene feature
   tour lands around 90 s-2 min of webm.
6. **Captions must be TRANSPARENT — never a solid card that occludes the UI.** A caption is
   narration floating *over* the demo, not a panel that hides the very thing it describes. The
   harness renders caption text with **no background/box/backdrop** — readability comes from a
   strong multi-layer text-shadow "halo" (dark + tight layers) that works on light *or* dark
   backgrounds. It also **auto-hides** after its `secs` (pass `keep=True` to hold it) so the
   following click/zoom plays unobstructed. Keep captions short — long `sub` text spans more of
   the frame.

---

## Step 3 — Auth (fresh tokenized URL)

The recording navigates to the real dashboard, so it needs a valid token.

- Preferred: `kirocrew token` (TTL 20h) -> gives `http://localhost:5476?token=...`.
- **If `kirocrew token` is permission-blocked for you** (it has been, in agent contexts),
  **ask the user to paste a fresh tokenized URL.** Do not block on it silently.
- Write the URL to a sidecar file the recorder reads, so the JWT never appears inline in a
  command (inline JWTs can trip secret filters):

  ```bash
  printf '%s' "<TOKENIZED_URL>" > <workdir>/.tokenurl
  ```

  The template reads `KC_URL` env or `argv[1]`; pass it via the file + a tiny wrapper, or export
  `KC_URL="$(cat <workdir>/.tokenurl)"` just before running.

Tokens expire. If a run dies with "no composer / 403", the token is stale — get a fresh one.

---

## Step 4 — Record

```bash
cd <workdir>
KC_DEMO_REFS="<app-skills-dir>/feature-demo-recording/references" \
KC_URL="$(cat .tokenurl)" "$(cat ~/.kiro/crew/workspace/.demo-recording-venv/PY_PATH)" record.py
```

`KC_DEMO_REFS` points back at the skill's `references/` directory so the copied
`record.py` can import `demo_harness` (the support modules stay in the skill bundle).

Watch `run.log` / stdout. The harness prints each click with coordinates and a final
`MAIN_WEBM: <path>` + `EVENTS: <n>`. If selectors miss (`!! none visible for ...`), fix the
selector list and re-run — recording is cheap and idempotent (each run writes a new webm and
overwrites `events.json`).

**Iterate against screenshots.** The debug PNGs are your eyes; open the ones around a failing
scene before changing selectors.

---

## Step 5 — Polish (auto-zoom + dead-air trim)

One command turns the raw webm + `events.json` into the cinematic cut:

```bash
bash <app-skills-dir>/feature-demo-recording/references/render.sh \
     <workdir> <workdir>/<feature>-demo.mp4 --out-fps 30 --dead-air-speed 6
```

`render.sh` chains two pure-Python stages (no Node/Remotion/cloud):
- **`camera.py`** — reads `events.json`, emits zoom keyframes. Each click -> a punch-in target:
  focal = the click point; `zoom = min(max_zoom, max(1.0, 0.30 / target_frac))` so smaller
  targets zoom in more (capped at `--max-zoom`, default 1.6). Caption spans become full-speed,
  no-zoom windows.
- **`postprocess.py`** — replays frames, applies a **spring-eased** (Remotion config:
  `damping=200, stiffness=100, mass=1, overshootClamping` -> smooth, no overshoot; 18-frame
  transition) **zoom/pan around each focal point** (wide -> punch-in -> hold -> punch-out), and
  **time-compresses dead air** between click/caption windows by `--dead-air-speed`x (real cut —
  `screen-demo-skill`'s trim only *reported* dead air; ours actually removes it). Re-encodes h264.

Tunables: `--max-zoom` (1.6), `--lead-ms` (280, how early the zoom starts before a click),
`--hold-ms` (1400, how long it stays zoomed after), `--transition-frames` (18, easing length),
`--dead-air-speed` (6), `--speed` (overall playback speedup), `--out-fps` (30).

The camera is driven entirely by the event log, so you can **re-tune the look without
re-recording** — just re-run `render.sh` with different flags against the same webm.

> Captions are composited **during recording** (burned in), but the post-processor keeps caption
> spans at full speed and zoom=1.0, so they're never sped-up-unreadable or cropped by a zoom.

---

## Step 6 — Ship (deliver)

`render.sh` already produced an h264 mp4 (yuv420p + faststart — plays everywhere). If you want a
plain transcode *without* the auto-zoom (rarely), use the h264 ffmpeg directly:

```bash
FF="$(cat ~/.kiro/crew/workspace/.demo-recording-venv/FFMPEG_PATH)"
WEBM="$(cat <workdir>/MAIN_WEBM)"
"$FF" -y -i "$WEBM" -vf "scale=1280:-2" -c:v libx264 -pix_fmt yuv420p -crf 23 \
      -movflags +faststart <workdir>/<feature>-plain.mp4
```

> Warning: **ffmpeg must have libx264.** The ffmpeg Playwright *bundles* is a stripped webm/vp8-only
> build with **no** libx264 and no mp4 muxer — it errors with `Unrecognized option 'movflags'`.
> `setup.sh` installs `imageio-ffmpeg` (a full static build) and writes its path to `FFMPEG_PATH`,
> so use that. `postprocess.py` uses it automatically via imageio.

> Warning: **Pick the webm by the `MAIN_WEBM:` the recorder just printed** — NOT "the largest .webm in
> the dir." A past delivery shipped a *stale* video because an older, larger webm from a previous
> run was still sitting in the folder. The harness selects by mtime >= run-start and writes the
> path to `<workdir>/MAIN_WEBM`; `render.sh` reads that. Verify the mp4's duration/size changed
> from the last run before sending.

Then deliver. To DM the user on Slack, use the `file_send` MCP tool with the mp4 path and a one-line caption.
If Slack delivery isn't requested, just report the local mp4 path.

---

## Quick reference: the worked example

`references/session_grid_scenes.py` is the **complete, working 6-scene Session Grid script**
(split -> fork -> 2x2 grid -> persist -> close -> live-sync). Read it to see real selectors, the
fork precondition fix, and caption phrasing. Adapt it scene-by-scene for a new feature.

## Files in this skill

- `references/setup.sh` — make/verify the venv (Playwright + Pillow + numpy + imageio + h264 ffmpeg)
- `references/demo_harness.py` — the `Demo` class (cursor/caption/video/webm-picker **+ event log**). Don't edit per-demo.
- `references/record_template.py` — copy this, fill in `SCENES`, run it (RECORD)
- `references/session_grid_scenes.py` — the full Session Grid demo as a reference implementation
- `references/camera.py` — event log -> auto-zoom keyframes (POLISH stage 1)
- `references/spring.py` — overdamped spring easing (ported from Remotion's `spring()`)
- `references/postprocess.py` — spring zoom/pan + dead-air trim -> h264 mp4 (POLISH stage 2)
- `references/render.sh` — one-shot driver: `camera.py` -> `postprocess.py`

## Pipeline at a glance

```
record.py --> page@*.webm  +  events.json        (RECORD: harness)
                    |
   render.sh --> camera.py  (events.json -> camera.json: zoom keyframes)
                    |
              postprocess.py (webm + camera.json -> demo.mp4: spring zoom/pan + dead-air trim)
                    |
              file_send --> Slack                  (SHIP)
```
