#!/usr/bin/env python3
"""STEP 3 -- compose the HTML timeline that gets rendered to MP4.

    python3 compose.py --narr assets/audio/narr.json --events events.json \\
                       --out index.html [--brand brand.json]

Emits a single self-contained composition: designed intro slides, the real screen
capture with a per-beat punch-in and a click ripple, a subtitle layer, and an
outro. Every time in the output comes from measured data -- narration durations
from narr.json, beat timestamps from events.json. Nothing is eyeballed.

Two alignment facts that are easy to get wrong and expensive to discover late:

1. events.json timestamps are on the FOOTAGE clock, which starts AFTER the
   recorder's pre-roll (navigation, first-run modals, reaching a known state).
   The clip must therefore be offset by `preroll_s`, or voice and picture sit
   several seconds apart with everything else looking correct.

2. Two tweens must never animate the same property at once. The long-hold drift
   below is sequenced strictly between the punch-out and the next beat's
   punch-in; overlapping them leaves the scale undefined on the overlapping
   frames, which a seeking renderer will happily bake in.

brand.json (all optional):
{ "accent":"#8e48ff", "bg":"#19161d", "outro_eyebrow":"KIRO CREW",
  "outro_title_lead":"Command", "outro_title_tail":" Bar",
  "font_stack":"\\"Inter\\",\\"Droid Sans Fallback\\",sans-serif",
  "font_face_local":"Droid Sans Fallback" }
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pathcheck import read_json_input, safe_input_path, safe_open_output  # noqa: E402

from kiro_crew.security import (  # noqa: E402
    redact_credentials,
    redact_exfiltration_urls,
)

TARGET_FILL = 0.55  # how much of the frame the focal element should fill

MINZ = 1.15  # always push in a little; a zoom of 1.0 reads as a dead beat
MAXZ = 1.45
LEAD = 0.35  # start easing in slightly before the beat lands
HOLD = 3.4  # how long to stay pushed in

# brand.json reaches the generated CSS as raw text, and CSS is a place where a
# value can end its own declaration and open a <style>-escaping construct. Text
# fields go through html.escape; these do not (a colour is not HTML), so they are
# checked against an allowlist instead of escaped. Deliberately no `(){};:<>` in
# any of them -- the parentheses in `src:local(...)` are OURS, not the input's.
_COLOR_RE = re.compile(r"\A#[0-9A-Fa-f]{3,8}\Z")
_FAMILY_RE = re.compile(r"\A[A-Za-z0-9 _-]{1,64}\Z")
_STACK_RE = re.compile(r"\A[A-Za-z0-9 ,'\"_-]{1,200}\Z")
_LANG_RE = re.compile(r"\A[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*\Z")


def _attr(value: str) -> str:
    """Escape a value that lands inside a double-quoted HTML attribute.

    ``html.escape`` with quote=True is what makes this safe for an attribute:
    escaping the text form only (as an earlier round did) leaves a crafted
    path free to close the quote and add an event handler.
    """
    return _html.escape(str(value), quote=True)


def _css(brand: dict, key: str, default: str, pattern: re.Pattern[str]) -> str:
    """Return brand[key] only if it matches its allowlist, else refuse loudly.

    Falling back to the default silently would hide a malformed brand file and
    ship a film in the wrong palette, so this is a hard error.
    """
    value = brand.get(key, default)
    if not isinstance(value, str) or not pattern.match(value):
        raise SystemExit(
            f"brand.json: {key}={value!r} is not an allowed CSS value "
            f"(must match {pattern.pattern}); it is interpolated into a stylesheet"
        )
    return value


DRIFT_MIN_DWELL = 9.0  # a hold longer than this drifts instead of sitting still
DRIFT_TO = 1.07


def _clean(text) -> str:
    """Scrub credential-shaped spans out of authored brand prose.

    Reports a count only: naming what was removed would print the secret into the
    transcript and undo the redaction.
    """
    raw = "" if text is None else str(text)
    if not raw:
        return raw
    out, urls = redact_exfiltration_urls(raw)
    out, creds = redact_credentials(out)
    if out != raw:
        print(f"  ! brand text: redacted {len(urls) + len(creds)} credential-shaped span(s)")
    return out


def esc(text) -> str:
    """HTML-escape interpolated prose. Script-authored text is still text: a
    stray `<` corrupts the composition and a close tag can escape its element."""
    return _html.escape("" if text is None else str(text), quote=True)


def js_str(text) -> str:
    """JSON-encode for embedding inside a <script> element. `</` is broken up
    because an HTML parser ends the script at `</script>` even inside a string."""
    return json.dumps("" if text is None else str(text), ensure_ascii=False).replace("</", "<\\/")


def zoom_for(e, cw, ch):
    fx, fy = e["focal"]["x"] / cw, e["focal"]["y"] / ch
    frac = max(e["bbox"]["w"] / cw, e["bbox"]["h"] / ch)
    z = min(MAXZ, max(MINZ, TARGET_FILL / frac)) if frac > 0 else 1.3
    return fx, fy, round(z, 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--narr", default="assets/audio/narr.json")
    ap.add_argument("--events", default="events.json")
    ap.add_argument("--footage", default="assets/footage.mp4")
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--brand", default=None)
    args = ap.parse_args()

    narr = read_json_input(args.narr)
    ev = read_json_input(args.events)
    b = read_json_input(args.brand) if args.brand else {}

    # Defaults are the product's DEFAULT colour theme, kiro-dark, copied from
    # website/src/index.css (the [data-theme="kiro-dark"] block). Not a look chosen
    # here: useTheme.tsx sets DEFAULT_COLOR_THEME = "kiro", so these are the colours
    # a viewer already sees in the product, and the capture is recorded on the same
    # theme so a slide and the dashboard behind it are the same product.
    accent = _css(b, "accent", "#8e48ff", _COLOR_RE)
    accent_h = _css(b, "accent_hi", "#9f63ff", _COLOR_RE)
    bg = _css(b, "bg", "#19161d", _COLOR_RE)
    bg_el = _css(b, "bg_el", "#211d25", _COLOR_RE)
    font_stack = _css(b, "font_stack", '"Inter","Droid Sans Fallback",sans-serif', _STACK_RE)
    font_local = _css(b, "font_face_local", "Droid Sans Fallback", _FAMILY_RE)
    # brand.json is the OTHER authored input, and its prose is rendered into the
    # film exactly like a caption. narrate.py scrubs the narration script at its
    # source; this is the same scrub for this source, so a credential in a brand
    # file cannot be published on a slide.
    o_eyebrow = _clean(b.get("outro_eyebrow", ""))
    o_lead = _clean(b.get("outro_title_lead", ""))
    o_tail = _clean(b.get("outro_title_tail", ""))
    lang = _css(b, "lang", "en", _LANG_RE)

    LINES, TOTAL = narr["lines"], narr["total"]
    CW, CH = ev["viewport"]["width"], ev["viewport"]["height"]
    VOFF = float(ev.get("preroll_s", 0.0))
    silent = bool(narr.get("silent"))

    intro = [line for line in LINES if line["role"] == "intro"]
    foot = [line for line in LINES if line["role"] == "footage"]
    outro_lines = [line for line in LINES if line["role"] == "outro"]
    for role, got in (("footage", foot), ("outro", outro_lines)):
        if not got:
            raise SystemExit(
                f"narration has no {role!r} line -- a composition needs at least one; "
                "narrate.py accepts a script without one, so this is checked here"
            )
    outro = outro_lines[0]
    beats = sorted(ev["events"], key=lambda e: e["t_ms"])
    if not beats:
        raise SystemExit("events.json has no beats")

    Tfoot = foot[0]["start"]
    clip_start = round(Tfoot - VOFF - beats[0]["t_ms"] / 1000.0, 3)
    if clip_start < 0:
        raise SystemExit(
            f"clip_start would be {clip_start}s: the capture needs to begin before the "
            "composition does. The timeline is shorter than the recording it has to "
            "cover -- the first footage line starts at "
            f"{Tfoot:.3f}s but the capture needs {VOFF + beats[0]['t_ms']/1000.0:.3f}s "
            "of lead (pre-roll plus the first beat). Either lengthen the lines before "
            "the footage, or re-record paced by THIS timeline."
        )
    outro_start = outro["start"]
    footage_dur = round(outro_start - clip_start, 3)
    DUR = round(TOTAL + 0.8, 2)

    # An <audio> without an id is NOT discovered by the renderer and the video ships
    # silent; in silent mode there is no track to reference at all.
    # The track has to be the one this timeline was measured from: a custom --narr
    # with a hardcoded src loads a DIFFERENT recording than the timing came from.
    narr_dir = pathlib.Path(args.narr).resolve().parent
    out_dir = pathlib.Path(args.out).resolve().parent
    # The gate cannot hand a descriptor across a process boundary: the browser and
    # ffmpeg open these during `npm run render`, long after this script exits. So the
    # check is on the RESOLVED path (realpath, sensitive-path refused) and the HTML
    # references that resolved file -- a symlink re-pointed afterwards no longer
    # decides what gets baked into the film.
    footage_real = safe_input_path(args.footage)
    footage_src = os.path.relpath(footage_real, out_dir).replace(os.sep, "/")
    audio_real = safe_input_path(narr_dir / "narration.mp3")
    audio_src = os.path.relpath(audio_real, out_dir).replace(os.sep, "/")
    audio_el = (
        ""
        if silent
        else (
            '<audio id="narration" data-start="0" data-duration="%.3f"'
            ' data-track-index="9" src="%s"></audio>' % (TOTAL, _html.escape(audio_src, quote=True))
        )
    )

    cam = []
    for idx, e in enumerate(beats):
        fx, fy, z = zoom_for(e, CW, CH)
        tc = clip_start + VOFF + e["t_ms"] / 1000.0
        tin = max(clip_start, tc - LEAD)
        nxt = (
            clip_start + VOFF + beats[idx + 1]["t_ms"] / 1000.0
            if idx + 1 < len(beats)
            else clip_start + footage_dur
        )
        next_tin = nxt - LEAD if idx + 1 < len(beats) else nxt
        # Two tweens on the same property must never overlap, so the push-in is
        # capped by the gap to the next one. Beats closer together than the nominal
        # 0.55s would otherwise write `scale` concurrently and the rendered camera
        # move becomes undefined on the shared frames.
        zoom_dur = max(0.12, min(0.55, next_tin - tin - 0.05))
        cam.append(
            f'  tl.set("#vid",{{transformOrigin:"{fx*100:.1f}% {fy*100:.1f}%"}},{tin:.3f});\n'
            f'  tl.to("#vid",{{scale:{z},duration:{zoom_dur:.3f},ease:"power2.out"}},'
            f"{tin:.3f});\n"
        )
        # Beats closer together than HOLD would otherwise schedule this reset ON TOP
        # of the next punch-in. When there is no room, skip the reset entirely and
        # let the next beat's tween continue from the current scale.
        tout = min(clip_start + footage_dur, tc + HOLD, next_tin - 0.6)
        has_reset = tout - tin >= 1.2
        if has_reset:
            cam.append(
                f'  tl.to("#vid",{{scale:1,duration:0.55,ease:"power2.inOut"}},{tout:.3f});\n'
            )
        d_from, d_to = round(tout + 0.6, 3), round(next_tin - 0.8, 3)
        if has_reset and d_to - d_from > DRIFT_MIN_DWELL:
            cam.append(
                f'  tl.to("#vid",{{scale:{DRIFT_TO},duration:{d_to-d_from:.3f},ease:"none"}},{d_from:.3f});\n'
                f'  tl.to("#vid",{{scale:1,duration:0.55,ease:"power2.inOut"}},{d_to:.3f});\n'
            )
        if e.get("kind") == "click":
            cam.append(
                f'  tl.set("#ripple",{{left:{fx*CW:.0f},top:{fy*CH:.0f},scale:0.4,opacity:0.9}},{tc:.3f});\n'
                f'  tl.to("#ripple",{{scale:1.6,opacity:0,duration:0.7,ease:"power2.out"}},{tc:.3f});\n'
            )
    cam_js = "".join(cam)

    divs, js = [], []
    for i, s in enumerate(intro):
        end = intro[i + 1]["start"] if i + 1 < len(intro) else Tfoot
        sid = f"slide{i}"
        divs.append(
            f'<div class="layer" id="{sid}"><div class="grid"></div><div class="sc">'
            f'<div class="eb">{esc(s.get("eyebrow", ""))}</div>'
            f'<div class="ti">{esc(s.get("title", ""))}</div>'
            f'<div class="su">{esc(s.get("sub", ""))}</div></div></div>'
        )
        js.append(
            f'  gsap.set("#{sid}",{{opacity:0,visibility:"hidden"}});\n'
            f'  tl.set("#{sid}",{{visibility:"visible"}},{s["start"]:.3f});\n'
            f'  tl.fromTo("#{sid} .eb",{{y:20,opacity:0}},{{y:0,opacity:1,duration:0.5}},{s["start"]+0.05:.3f});\n'
            f'  tl.fromTo("#{sid} .ti",{{y:36,opacity:0}},{{y:0,opacity:1,duration:0.6,ease:"power3.out"}},{s["start"]+0.15:.3f});\n'
            f'  tl.fromTo("#{sid} .su",{{y:24,opacity:0}},{{y:0,opacity:1,duration:0.6,ease:"power2.out"}},{s["start"]+0.35:.3f});\n'
            f'  tl.to("#{sid}",{{opacity:1,duration:0.35}},{s["start"]:.3f});\n'
            f'  tl.to("#{sid}",{{opacity:0,duration:0.35}},{end-0.35:.3f});\n'
            f'  tl.set("#{sid}",{{visibility:"hidden"}},{end:.3f});\n'
        )

    caps = ",\n".join(
        f'    {{t:{line["start"]:.3f},d:{line["dur"]:.3f},x:{js_str(line["cap"])}}}'
        for line in foot
    )

    html = f"""<!doctype html>
<html lang="{lang}" data-resolution="landscape">
<head><meta charset="UTF-8"/>
<!-- Pinned by version AND by content: without integrity, a compromised CDN
     response would run inside the composition next to the captions and the
     footage. Hash verified against the published 3.14.2 build. -->
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"
  integrity="sha384-sG0Hv1tP1lZCk9KQmrIbY/XNwi+OY84GQqhMscbnsoBFqAz8KNCil1kvfL3Hbbk2"
  crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<style>
  /* A font used without a declaration fails the render gate and silently falls back.
     `src: local(...)` satisfies it for an OS-bundled face with no downloadable file. */
  @font-face{{font-family:'{font_local}';src:local('{font_local}');}}
  :root{{--accent:{accent};--accent-h:{accent_h};--bg:{bg};--bg-el:{bg_el};
    --strong:#f2f1f4;--muted:#938f9b;--line:#352f3d;}}
  *{{box-sizing:border-box;}}
  body,html{{margin:0;width:{CW}px;height:{CH}px;overflow:hidden;background:var(--bg);font-family:{font_stack};}}
  #stage{{width:{CW}px;height:{CH}px;position:relative;background:var(--bg);overflow:hidden;}}
  #camera{{position:absolute;inset:0;overflow:hidden;background:#000;z-index:1;}}
  #vid{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;}}
  #vignette{{position:absolute;inset:0;z-index:2;pointer-events:none;box-shadow:inset 0 0 220px rgba(0,0,0,.5);}}
  .ripple{{position:absolute;z-index:3;width:130px;height:130px;margin:-65px 0 0 -65px;
    border:4px solid var(--accent);border-radius:50%;opacity:0;pointer-events:none;}}
  .layer{{position:absolute;inset:0;z-index:5;
    background:radial-gradient(1300px 860px at 50% -6%,var(--bg-el),transparent 60%),var(--bg);}}
  .grid{{position:absolute;inset:0;opacity:.4;
    background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
    background-size:120px 120px;mask-image:radial-gradient(circle at 50% 42%,black,transparent 82%);}}
  .sc{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
    justify-content:center;text-align:center;padding:0 170px;}}
  .eb{{color:var(--accent);font-weight:800;font-size:32px;letter-spacing:6px;text-transform:uppercase;margin-bottom:26px;}}
  .ti{{color:var(--strong);font-weight:900;font-size:124px;letter-spacing:-3px;line-height:1.06;}}
  .su{{color:var(--muted);font-weight:500;font-size:44px;margin-top:30px;max-width:1240px;line-height:1.4;}}
  #outro .ti{{font-size:140px;}} #outro .ti .c{{color:var(--accent);}}
  #capbar{{position:absolute;left:80px;right:80px;bottom:74px;z-index:6;text-align:center;}}
  /* A captured UI can be a LIGHT surface, where near-white caption text fails contrast
     no matter how heavy the shadow. A solid pill makes readability independent of the frame. */
  .cap{{position:absolute;left:0;right:0;bottom:0;text-align:center;}}
  .cap .pill{{display:inline-block;background:rgba(14,16,21,.90);border:1px solid rgba(178,127,255,.28);
    color:var(--strong);font-weight:800;font-size:46px;line-height:1.3;padding:16px 34px;
    border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,.45);}}
  .barwrap{{position:absolute;left:0;right:0;bottom:0;height:8px;background:var(--line);z-index:7;overflow:hidden;}}
  #bar{{position:absolute;inset:0;transform-origin:left center;background:linear-gradient(90deg,var(--accent),var(--accent-h));}}
</style></head>
<body>
<div id="stage" data-composition-id="root" data-width="{CW}" data-height="{CH}" data-start="0" data-duration="{DUR}">
  <div id="camera"><video id="vid" class="clip" data-start="{clip_start:.3f}" data-duration="{footage_dur:.3f}"
    data-track-index="0" src="{_attr(footage_src)}" muted playsinline></video></div>
  <div id="vignette"></div>
  <div class="ripple" id="ripple"></div>
  {''.join(divs)}
  <div class="layer" id="outro"><div class="grid"></div><div class="sc">
    <div class="eb">{esc(o_eyebrow)}</div>
    <div class="ti">{esc(o_lead)}<span class="c">{esc(o_tail)}</span></div>
    <div class="su">{esc(outro.get("sub", ""))}</div></div></div>
  <div id="capbar"></div>
  <div class="barwrap"><div id="bar"></div></div>
  {audio_el}
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{paused:true}}); const DUR={DUR};
    tl.fromTo("#bar",{{scaleX:0}},{{scaleX:1,duration:DUR,ease:"none"}},0);
    gsap.set("#vid",{{scale:1,transformOrigin:"50% 50%"}});
    gsap.set("#ripple",{{opacity:0}});
{cam_js}
{''.join(js)}
    gsap.set("#outro",{{opacity:0,visibility:"hidden"}});
    tl.set("#outro",{{visibility:"visible"}},{outro_start:.3f});
    tl.fromTo("#outro",{{opacity:0}},{{opacity:1,duration:0.45}},{outro_start:.3f});
    tl.fromTo("#outro .ti",{{y:40,opacity:0}},{{y:0,opacity:1,duration:0.7,ease:"power3.out"}},{outro_start+0.15:.3f});
    tl.fromTo("#outro .su",{{y:24,opacity:0}},{{y:0,opacity:1,duration:0.7,ease:"power2.out"}},{outro_start+0.5:.3f});
    const CAPS=[
{caps}
    ];
    const cb=document.getElementById("capbar");
    CAPS.forEach(c=>{{const el=document.createElement("div");el.className="cap";
      const pill=document.createElement("span");pill.className="pill";pill.textContent=c.x;
      el.appendChild(pill);cb.appendChild(el);
      gsap.set(el,{{opacity:0,visibility:"hidden",y:14}});
      tl.set(el,{{visibility:"visible"}},c.t);
      tl.to(el,{{opacity:1,y:0,duration:0.22}},c.t);
      tl.to(el,{{opacity:0,y:-8,duration:0.18}},c.t+c.d+0.05);
      tl.set(el,{{visibility:"hidden"}},c.t+c.d+0.24);}});
    window.__timelines["root"]=tl;
  </script>
</div></body></html>
"""
    with safe_open_output(args.out, replace=True) as fh:
        fh.write(html)
    ripples = sum(1 for e in beats if e.get("kind") == "click")
    print(
        f"preroll={VOFF}s clip_start={clip_start} footage_dur={footage_dur} "
        f"outro_start={outro_start} DUR={DUR}"
    )
    print(
        f"slides={len(intro)} captions={len(foot)} beats={len(beats)} "
        f"ripples={ripples} audio={'none' if silent else 'narration.mp3'}"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
