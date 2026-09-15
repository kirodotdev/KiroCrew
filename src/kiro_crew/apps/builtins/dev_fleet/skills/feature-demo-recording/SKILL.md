---
name: feature-demo-recording
description: Record a demo video of a web feature from a real browser. Two modes -- a NARRATED film where measured voiceover drives the timeline (designed slides, subtitles, punch-in camera, rendered from an HTML timeline), and a SILENT evidence clip for a PR or a QA pass. Use when the user asks to record a video, demo, or screen recording of a feature, dashboard, or web UI flow.
---

# Feature Demo Recording

Record a real browser and cut it into either of two things:

- **Narrated film** (default when someone will *watch and listen*): designed brand
  slides, spoken narration, subtitles, and a camera that punches in on whatever is
  being talked about.
- **Silent evidence clip** (a PR, a QA pass): the same capture with subtitles and
  punch-in, no audio, authored durations.

Both share one pipeline. The only difference is whether the timeline comes from
measured speech or from durations you write down.

## The rule that makes it work

**Narration first, then record.** Generate the audio, measure every line with
`ffprobe`, and pace the capture to those numbers. Alignment is then true by
construction instead of repaired in the edit. Recording first and narrating after
accumulates drift you cannot fix without re-recording -- measured at about 3s by the
last beat on a two-minute cut, versus under a few hundred ms when the recorder
holds absolute targets read out of the measured timeline.

In silent mode there is nothing to measure, so you write each beat's duration in
the script instead. Same file, same recorder, `narrate.py --silent`.

## Pipeline

```
script.json ──narrate.py──> narration.mp3 + narr.json      (measured timeline)
              (--silent: no audio, durations taken from the script)
                                   │
                                   ▼  the recorder reads the beat targets
   record.py (adapted) ──> page.webm + events.json          (capture + beat log)
                                   │
                  ffmpeg ──> assets/footage.mp4
                                   │
      compose.py (narr.json + events.json) ──> index.html   (HTML timeline)
                                   │
                  hyperframes ──> renders/*.mp4
                                   │
      verify_align.py ──> drift / audio / picture / streams  (gate before delivery)
```

Only `record.py` is written per video. Everything else is generic.

## How to invoke these scripts

Two rules come from the shared path-safety gate (`references/_pathcheck.py`), which
every script under `references/` routes its reads and writes through:

1. **Use an interpreter that can import `kiro_crew`.** The gate consults the
   CENTRALIZED sensitive-path check rather than a local copy of the denylist, so
   when the package is not importable it fails closed with exit 78 rather than
   guessing. Any interpreter the package is installed into will do -- a
   provisioned worktree venv (`<worktree>/.venv/bin/python`) is the usual one but
   is NOT guaranteed to exist. Do NOT trust `command -v kirocrew` to name one: on
   a host using a version manager it resolves to a shim whose interpreter cannot
   import the package. Verify whichever you pick:
   `<py> -c "import kiro_crew"`. Exit 78 with "cannot import kiro_crew" means the
   interpreter, not the script.
2. **Run from the video project directory, with paths inside it.** Outputs must
   resolve within the working directory and inputs go through the descriptor-pinned
   read gate, so `--out /tmp/x.html` is refused by design.

**The gate applies to `references/` only.** Your own recorder -- the copy of
`record_template.py` that lives in the video project -- imports playwright and the
standard library, and is not gated. It therefore needs the opposite thing: an
interpreter with **playwright** installed, which is often NOT the one that can
import `kiro_crew`. Two interpreters is the normal case, and a
`ModuleNotFoundError: playwright` there is not the exit-78 gate.

```bash
PY=<worktree>/.venv/bin/python      # must satisfy: $PY -c "import kiro_crew"
REC=python3                         # must satisfy: $REC -c "import playwright"
cd <video-project-dir>
$PY <skill>/references/deps.py
```

## What leaves the machine

Be straight with the user about this, because a demo often captures an internal
dashboard. Narration goes through the same speech providers the product's own
voice-reply path uses, so the answer depends on which one is picked:

- The **capture never leaves**: Playwright runs headless and local, and the
  transcode is local ffmpeg.
- **`--provider piper`** (preferred, what `auto` picks when it is installed): local
  neural TTS, so the narration text never leaves either. Needs the `piper` binary
  and an `.onnx` voice model.
- **`--provider polly`**: synthesis happens in the caller's OWN AWS account through
  the `aws` CLI. Costs a little, needs credentials, stays inside their account.
- **`--silent`**: no speech at all, nothing sent.

There is deliberately no third-party speech service. Narration text is product
content and a demo often narrates an internal surface, so the only destinations this
pipeline will speak to are the machine it runs on and the caller's own account. When
neither is available it stops and says so rather than reaching for a convenient
endpoint.
- The **renderer is fetched**: `hyperframes` comes from npm on demand and the
  composition loads GSAP from a CDN, so a render needs network the first time.

`references/deps.py` prints which providers exist and which one `auto` will pick.
For a strictly air-gapped run use `piper` or `--silent`, and pre-warm the npm/CDN
caches -- do not claim the whole pipeline is local without checking.

## Step 0 -- dependencies

```bash
$PY references/deps.py            # report, exit 1 if anything is missing
$PY references/deps.py --install  # install what is installable
```

Only user-level installs (pip `--user`, the Playwright browser cache, the npx
cache). Anything needing root -- a language runtime, a system font -- is reported
with the exact command for a human rather than escalated silently. Existing tooling
on disk is reused, not rebuilt.

| Dependency | Why | If missing |
|---|---|---|
| a speech provider | narration (none needed in silent mode) | reported, not auto-installed. piper counts as ready only with a voice model (`KC_VIDEO_PIPER_MODEL`), because `resolve_provider` needs one -- a binary alone would make the doctor claim a local default it will not actually pick. `auto` has no third-party fallback to reach for |
| `ffmpeg` with **libx264** | transcode the capture, normalise clips | `pip install --user imageio-ffmpeg` (auto). The ffmpeg Playwright bundles is vp8/webm-only with no mp4 muxer, so the doctor checks for the encoder, not just the binary |
| `ffprobe` | measures durations; the timeline depends on it | NOT provided by imageio-ffmpeg -- needs a full ffmpeg install, so the doctor marks it human-installable |
| `playwright` + chromium | drive and record the real UI | `pip install --user playwright` + `playwright install chromium` (auto) |
| Node 18+ / `npx` | runs the renderer | reported, never installed -- this skill does not install a language runtime |
| `hyperframes` (pinned) | HTML timeline -> MP4 | fetched by npx at RENDER time. The doctor only asks the registry whether the pinned version resolves (`npm view`, metadata, nothing executed -- a check that runs a downloaded package is a worse hazard than the one it reports). It tries the configured registry FIRST and falls back to `--registry=https://registry.npmjs.org` when that one refuses (an authenticated mirror answers E401 for public packages), and tells you which applies |
| a CJK font | only for CJK subtitles | reported. An *undeclared* family fails the render gate rather than degrading, so the family name is configuration |

## Step 1 -- write and measure the narration

Copy `references/script.example.json`, write the lines, then:

```bash
$PY references/narrate.py script.json --out-dir assets/audio
$PY references/narrate.py script.json --out-dir assets/audio --provider piper \
        --piper-model ~/.local/share/piper/en_US-amy-medium.onnx
$PY references/narrate.py script.json --out-dir assets/audio --silent   # evidence clip
```

`--provider auto` prefers piper, then polly, and stops if neither is available --
there is no third-party fallback to fall through to. Check what that resolves
to on the current host before running it on sensitive text.

Roles: `intro` (a designed slide), `footage` (over the capture), `outro`. `say` is
spoken, `cap` is the subtitle -- keep them separate, because a glyph like the
command key reads badly aloud and a subtitle must be shorter than a sentence. In
silent mode give each line a `dur`.

**The number of `footage` lines is the number of beats the recorder marks.** That is
the contract between steps 1 and 2.

Budget about 2.5 spoken words per second, and keep any single footage line under
roughly 15s. A longer line leaves the frame sitting still while the voice keeps
going, which reads as a dead shot; put long explanations on a *slide*, where a
static frame is the intended look.

## Step 2 -- record, paced by the timeline

Copy `references/record_template.py` and adapt only the beat blocks. The template
carries the parts that are easy to get wrong: absolute-target pacing, `preroll_s`
capture, `beat` tagging, and cursor honesty.

```python
hold_until(targets[n] - lead, page)   # idle until just before the beat
...do the work (click, type, navigate)...
hold_until(targets[n], page)          # absorb whatever the work cost
mark(page, "what is on screen", box, kind="focus", beat=n)
```

When the work cannot fit the lead -- a page reload on a short cut -- do the work and
mark when the result is on screen, and say so in a comment. Watch the run log for
`! overran beat target`.

Selector note: production bundles often strip `data-testid`, so probe the running
page for roles and accessible names instead of trusting selectors from source.

## Step 3 -- transcode

`FF` is whatever `deps.py` reported for ffmpeg: an imageio-provided one is NOT on
PATH, so a bare `ffmpeg` here would fail on a host the doctor called green.

```bash
FF=ffmpeg    # or the path deps.py printed for the ffmpeg row
"$FF" -y -i "$(cat MAIN_WEBM)" -c:v libx264 -preset medium -crf 20 \
      -pix_fmt yuv420p -an assets/footage.mp4
```

## Step 4 -- compose and render

```bash
$PY references/compose.py --brand brand.json   # -> index.html
npm run check     # in a hyperframes project; fix everything it reports
                  # a private mirror answers E401 for public packages; pin
                  # --registry=https://registry.npmjs.org in package.json
npm run render
```

Run `check` before every render. It has caught a silent-audio bug, overlapping
tweens, an undeclared font and failing subtitle contrast in this pipeline -- each of
which would otherwise have shipped.

## Step 5 -- verify, then deliver

```bash
$PY references/verify_align.py renders/<file>.mp4
```

Four checks, each for a failure that has actually shipped: per-beat **drift**
against its line; **audio** loudness (a silent track reads about -91 dB; skipped in
silent mode); **picture** luminance in slide and footage windows (a near-black
footage window means the video element never seeked); and **streams** plus duration
against the composition. Non-zero exit means do not deliver.

Then hand over the mp4 path, or `file_send` it if the user asked for Slack.

## Scene-design rules

1. **Subtitles and slides are English-only** unless the narration language is
   deliberately something else. They are our cards, not app content.
2. **Never let real user data into the frame.** Record from an isolated pod, or
   create fresh demo data. A real session list carries chat titles and internal
   references.
3. **Pre-seed any state a beat depends on.** If a beat needs a precondition, set it
   up in the pre-roll, which is outside the narrated timeline.
4. **Capture in dark mode when the brand frames are dark.** Cutting from a designed
   dark slide to a white dashboard is jarring, and near-white subtitles over a light
   UI fail contrast no matter how heavy the shadow (the subtitle uses a solid pill,
   so readability holds either way).

## House style, and where it comes from

This is worth stating plainly, because "brand slides plus a punch-in camera over
screen capture" is a whole genre and a reader could reasonably ask whose look this is.

**The palette is not a choice made here.** `useTheme.tsx` sets
`DEFAULT_COLOR_THEME = 'kiro'`, so the Kiro theme is what a viewer already sees, and
the brand frames use its tokens verbatim from the `[data-theme="kiro-dark"]` block in
`website/src/index.css`: `--bg:#19161d`, `--bg-elevated:#211d25`, `--accent:#8e48ff`,
`--accent-hover:#9f63ff`, `--text-strong:#f2f1f4`, `--muted:#938f9b`,
`--border:#352f3d`. **The capture is recorded on the same theme** -- the recorder seeds
`mc-color-theme=kiro` and `mc-theme=dark` -- so a slide and the dashboard behind it are
one product rather than two. Getting this wrong is visible: an earlier cut here put
emerald brand frames (the plain `dark` theme) around a Kiro-purple dashboard, and that
mismatch is exactly what makes a film look assembled from parts.

**The method is the distinctive part, not the look.** Everything here follows from one
rule -- the voice is measured first and the picture is paced to it. The genre norm is
the opposite: record at a human pace, then compress the dead air afterwards. That
inversion is what produces the properties below, and none of them are stylistic
preferences; each is a number this pipeline paid for once.

- **A beat lands on its line, not near it.** The recorder holds absolute targets read
  out of the measured timeline, so reload and typing overhead is absorbed. Budget:
  drift under a few hundred ms across a whole film. Duration-driven pacing measured
  about 3s of accumulated drift by the last beat, which is where this rule came from.
- **Roughly 2.5 spoken words per second**, so a footage line over about 15s is a
  design error rather than a long sentence: the frame sits still while the voice keeps
  going. Long explanations belong on a slide, where a static frame is the intent.
- **A hold longer than 9s drifts instead of freezing**, and the drift is sequenced
  strictly between the punch-out and the next punch-in, because two tweens on one
  property leave the value undefined on the overlapping frames.
- **A zoom that computes to 1.0 is a dead beat.** Clamp to a minimum or frame a
  sub-region; a large focal element otherwise yields no push-in at all.
- **The cursor appears only for real pointer actions.** A dot teleported onto a
  keyboard beat claims a click nobody made.
- **Subtitles sit on a solid pill** rather than relying on a text shadow, so
  readability does not depend on what the captured UI is doing underneath.
- **Delivery is gated, not eyeballed.** `verify_align.py` fails on per-beat drift,
  silence, a near-black footage window, or a duration that disagrees with the
  composition -- the same posture as a build gate, applied to a video.

**What is genuinely inherited.** The idea of punching in on the point that was just
clicked is not original to this skill; it is common to the genre, and an earlier
revision of this bundle carried an implementation derived from an external project.
That implementation is gone -- easing now comes from GSAP inside the composition, and
every constant above was set here. What remains shared with the genre is the concept,
which is the level at which everyone shares it.

## Landmines (each paid for once)

1. **An `<audio>` element with no `id` is not discovered by the renderer** -- the
   file ships completely silent while every other check passes.
2. **`events.json` timestamps are on the footage clock, not the video's.** The
   pre-roll happens before the beat clock starts, so the clip is offset by
   `preroll_s`. Getting this wrong puts voice and picture seconds apart with nothing
   else looking broken.
3. **Never let two tweens animate the same property at once.** The long-hold drift is
   sequenced strictly between the punch-out and the next punch-in, or the scale is
   undefined on the overlapping frames and a seeking renderer bakes it in.
4. **A zoom that computes to 1.0 is a dead beat.** A large focal element yields no
   push-in; clamp to a minimum or frame a sub-region.
5. **Only show the cursor for real pointer actions.** A dot teleported onto a
   keyboard beat claims a click nobody made.
6. **npm's default registry may be an authenticated mirror** -- pass the public one.
7. **Do not mint a dashboard credential from the recorder.** A local safety policy
   blocks command lines pairing a product name with the word token, and moving that
   same mint into a child process routes around the control instead of satisfying
   it. The operator hands the finished URL in `KC_VIDEO_TARGET_URL`; the recorder
   only falls back to the bare pod URL, which is enough when the target asks for no
   credential.
8. **A FRESH pod shows first-run modals**, and the gate is server-state driven: set
   the dashboard onboarded config as well as the localStorage keys. Changing pod
   config reloads the pod, so any credential in a URL you were handed is stale
   afterwards -- get the URL after the config change, not before. An
   already-provisioned pod needs none of this.
9. **The composition is generated, so fix the generator.** `compose.py` writes
   `index.html`; a palette or layout change made in the generated file alone is
   silently reverted by the next compose, and -- worse -- a stale generator paired
   with a good `index.html` looks fine until someone re-runs the documented step.
   After changing either, re-compose and check the OUTPUT carries the change.
10. **The verifier pairs beats exactly only when the recorder tags them.** Each
    primary mark carries `beat=<index>`; without the tag `verify_align.py` falls
    back to nearest-in-order inference, which reports a drift figure that is a
    guess. A recorder predating the tag pairs by inference and says so in its
    output -- believe that line before believing the number.

## Not this skill

- A still screenshot -> `web-verify`.
- A quick GIF of one interaction with no cutting -> `browser-recording`.

## Files

- `references/_pathcheck.py` -- the shared path-safety gate every script reads and writes through (centralized sensitive-path check, fail-closed)
- `references/deps.py` -- detect + user-level install + honest failure
- `references/narrate.py` -- STEP 1: TTS, measure, emit the timeline (`--silent` supported)
- `references/compose.py` -- STEP 3: narr.json + events.json -> `index.html`
- `references/verify_align.py` -- STEP 5: drift / audio / picture / streams gate
- `references/record_template.py` -- STEP 2: copy and adapt per video
- `references/script.example.json` -- script format
- `references/brand.example.json` -- palette, fonts, outro text
