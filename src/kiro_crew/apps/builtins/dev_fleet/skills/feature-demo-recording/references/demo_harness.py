#!/usr/bin/env python3
# mypy: ignore-errors
"""
demo_harness.py — reusable headless-Playwright demo recorder.

Provides a `Demo` context manager that:
  - launches headless Chromium and records video (default 1600x1000),
  - injects a red cursor that FOLLOWS the real mouse, with click ripples,
  - injects a premium caption card (serif title + letter-spaced eyebrow) for narration,
  - RE-INJECTS all overlays on every navigation (add_init_script),
  - seeds localStorage so onboarding/theme modals don't appear,
  - on exit, selects the webm produced by THIS run (mtime >= run start) and prints
    `MAIN_WEBM: <path>`  — never "the largest webm", which may be a stale leftover.

You don't edit this file per-demo. Write a scene script that imports `Demo` and calls its
methods (see record_template.py / session_grid_scenes.py).

Usage:
    from demo_harness import Demo
    with Demo(url, out_dir) as d:
        d.caption("Eyebrow", "Title", "subtitle", secs=4)
        d.click(['button:has-text("New chat")'], label="new chat")
        ...
"""
import glob
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

OVERLAY_JS = r"""
(() => {
  const install = () => {
    if (!document.body) return;
    if (!document.getElementById('__demo_cursor')) {
      const c = document.createElement('div');
      c.id = '__demo_cursor';
      c.style.cssText = 'position:fixed;z-index:2147483647;left:50%;top:50%;width:22px;height:22px;'
        + 'margin:-11px 0 0 -11px;border-radius:50%;border:2.5px solid #ff7a5c;'
        + 'background:rgba(255,122,92,0.18);pointer-events:none;box-shadow:0 0 12px rgba(255,122,92,.55);'
        + 'transition:left .04s linear,top .04s linear,transform .08s ease-out;';
      document.body.appendChild(c);
      const st = document.createElement('style');
      st.textContent =
        '@keyframes __rip{from{transform:scale(1);opacity:.55}to{transform:scale(6);opacity:0}}'
      + '@keyframes __cin{from{opacity:0;transform:translate(-50%,10px)}to{opacity:1;transform:translate(-50%,0)}}';
      document.head.appendChild(st);
      window.__mc=(x,y)=>{const e=document.getElementById('__demo_cursor');if(e){e.style.left=x+'px';e.style.top=y+'px';}};
      document.addEventListener('mousemove',e=>window.__mc(e.clientX,e.clientY),true);
      document.addEventListener('mousedown',e=>{
        const cur=document.getElementById('__demo_cursor'); if(cur) cur.style.transform='scale(0.7)';
        const r=document.createElement('div');
        r.style.cssText='position:fixed;z-index:2147483646;left:'+e.clientX+'px;top:'+e.clientY+'px;'
          +'width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;background:rgba(255,122,92,.5);'
          +'pointer-events:none;animation:__rip .5s ease-out forwards;';
        document.body.appendChild(r); setTimeout(()=>r.remove(),520);
      },true);
      document.addEventListener('mouseup',()=>{const cur=document.getElementById('__demo_cursor'); if(cur) cur.style.transform='scale(1)';},true);
    }
    const SERIF="Georgia,'Iowan Old Style','Times New Roman','Noto Serif',serif";
    const SANS="-apple-system,'Segoe UI','Helvetica Neue','Noto Sans',Arial,sans-serif";
    window.__cap=(eyebrow,title,sub)=>{
      let w=document.getElementById('__demo_cap');
      if(!w){
        w=document.createElement('div'); w.id='__demo_cap';
        w.style.cssText='position:fixed;z-index:2147483647;left:50%;bottom:30px;transform:translateX(-50%);'
          +'max-width:70%;pointer-events:none;text-align:center;'
          +'background:none;border:none;box-shadow:none;animation:__cin .28s ease-out;';
        const HALO='0 1px 2px rgba(0,0,0,.95),0 0 8px rgba(0,0,0,.85),0 0 18px rgba(0,0,0,.75),'
          +'0 0 2px rgba(0,0,0,1)';
        const eb=document.createElement('div'); eb.id='__cap_eb';
        eb.style.cssText='font-family:'+SANS+';font-size:10.5px;font-weight:800;letter-spacing:2.4px;'
          +'text-transform:uppercase;color:#ffb59c;margin-bottom:5px;text-shadow:'+HALO+';';
        const ti=document.createElement('div'); ti.id='__cap_ti';
        ti.style.cssText='font-family:'+SERIF+';font-size:24px;font-weight:700;line-height:1.15;'
          +'letter-spacing:.2px;color:#fff;text-shadow:'+HALO+';';
        const su=document.createElement('div'); su.id='__cap_su';
        su.style.cssText='font-family:'+SANS+';font-size:13.5px;font-weight:600;line-height:1.4;'
          +'color:#fff;margin-top:6px;text-shadow:'+HALO+';';
        w.appendChild(eb); w.appendChild(ti); w.appendChild(su);
        document.body.appendChild(w);
      }
      w.style.display='block';
      document.getElementById('__cap_eb').textContent=eyebrow||'';
      document.getElementById('__cap_ti').textContent=title||'';
      document.getElementById('__cap_su').textContent=sub||'';
    };
    window.__capHide=()=>{const w=document.getElementById('__demo_cap'); if(w) w.style.display='none';};
  };
  if(document.body) install(); else document.addEventListener('DOMContentLoaded',install);
})();
"""


def is_ascii(s):
    try:
        return bool(s) and all(ord(c) < 128 for c in s)
    except Exception:
        return False


class Demo:
    def __init__(self, url, out_dir, width=1600, height=1000,
                 seed_localstorage=None, extra_init_css=None):
        self.url = url
        self.width, self.height = width, height
        self.seed = {"kc-onboarded": "1"} if seed_localstorage is None else seed_localstorage
        self.extra_css = extra_init_css
        self.log_lines = []
        self._t_start = None
        self._t_video0 = None
        self._events = []
        self._pw = self._browser = self._ctx = self.page = None
        # Validate output directory is not a sensitive location
        from _pathcheck import safe_open_output, safe_output_path
        self._safe_open = safe_open_output
        self.out = safe_output_path(out_dir, workdir=out_dir)
        os.makedirs(self.out, exist_ok=True)

    def __enter__(self):
        self._t_start = time.time()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
        self._ctx = self._browser.new_context(
            viewport={"width": self.width, "height": self.height},
            record_video_dir=self.out,
            record_video_size={"width": self.width, "height": self.height},
            device_scale_factor=1,
        )
        for k, v in self.seed.items():
            self._ctx.add_init_script(f"try{{localStorage.setItem({k!r},{v!r})}}catch(e){{}}")
        if self.extra_css:
            css_literal = json.dumps(self.extra_css)
            self._ctx.add_init_script(
                "try{var s=document.createElement('style');s.textContent=" + css_literal +
                ";(document.head||document.documentElement).appendChild(s);}catch(e){}"
            )
        self._ctx.add_init_script(OVERLAY_JS)
        self.page = self._ctx.new_page()
        self._t_video0 = time.time()
        self.log("goto", self.url[:48], "...")
        self.page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
        try:
            self.page.wait_for_selector("textarea, [contenteditable='true'], main", timeout=20000)
        except Exception as e:
            self.log("  initial wait timed out:", e)
        self.page.wait_for_timeout(2500)
        self.shot("00-loaded")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.cap_hide()
            self.page.wait_for_timeout(400)
        except Exception:
            pass
        try:
            self._ctx.close()
        finally:
            self._browser.close()
            self._pw.stop()
        fresh = [w for w in glob.glob(f"{self.out}/page@*.webm")
                 if os.path.getmtime(w) >= self._t_start - 1]
        fresh.sort(key=os.path.getmtime, reverse=True)
        with self._safe_open(f"{self.out}/run.log", workdir=self.out) as f:
            f.write("\n".join(self.log_lines))
        main = fresh[0] if fresh else None
        self.log("MAIN_WEBM:", main)
        print("MAIN_WEBM:", main)
        if main:
            with self._safe_open(f"{self.out}/MAIN_WEBM", workdir=self.out) as f:
                f.write(main)
        rec_ms = int((time.time() - (self._t_video0 or time.time())) * 1000)
        with self._safe_open(f"{self.out}/events.json", workdir=self.out) as f:
            json.dump({"viewport": {"width": self.width, "height": self.height},
                       "recording_ms": rec_ms, "events": self._events}, f, indent=2)
        self.log("EVENTS:", len(self._events), "->", f"{self.out}/events.json")
        return False

    def log(self, *a):
        s = " ".join(str(x) for x in a)
        print(s)
        self.log_lines.append(s)

    def shot(self, name):
        try:
            self.page.screenshot(path=f"{self.out}/debug-{name}.png")
        except Exception as e:
            self.log("  shot fail", name, e)

    def _record_event(self, cx, cy, w=0.0, h=0.0, kind="click", label=""):
        if self._t_video0 is None:
            return
        self._events.append({
            "t_ms": int((time.time() - self._t_video0) * 1000),
            "kind": kind, "label": label,
            "focal": {"x": float(cx), "y": float(cy)},
            "bbox": {"w": float(w), "h": float(h)},
            "viewport": {"width": self.width, "height": self.height},
        })

    def caption(self, eyebrow, title, sub="", secs=3.0, keep=False):
        if self._t_video0 is not None:
            t0 = int((time.time() - self._t_video0) * 1000)
            self._events.append({"t_ms": t0, "kind": "caption", "label": title,
                                 "dur_ms": int(secs * 1000),
                                 "focal": {"x": self.width / 2, "y": self.height / 2},
                                 "bbox": {"w": 0, "h": 0},
                                 "viewport": {"width": self.width, "height": self.height}})
        try:
            self.page.evaluate("([e,t,s])=>window.__cap&&window.__cap(e,t,s)", [eyebrow, title, sub])
        except Exception as e:
            self.log("  caption fail:", e)
        self.page.wait_for_timeout(int(secs * 1000))
        if not keep:
            self.cap_hide()

    def cap_hide(self):
        try:
            self.page.evaluate("()=>window.__capHide&&window.__capHide()")
        except Exception:
            pass

    def wait(self, ms):
        self.page.wait_for_timeout(int(ms))

    def _glide(self, x, y, steps=30):
        self.page.mouse.move(x, y, steps=steps)
        self.page.wait_for_timeout(150)

    @staticmethod
    def _center(loc):
        b = loc.bounding_box()
        return (b["x"] + b["width"] / 2, b["y"] + b["height"] / 2) if b else (None, None)

    def _first_visible(self, selectors, label=""):
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                pass
        self.log("  !! none visible for", label, selectors)
        return None

    def _all_visible(self, sel):
        out = []
        try:
            n = self.page.locator(sel).count()
        except Exception:
            n = 0
        for i in range(n):
            b = self.page.locator(sel).nth(i)
            try:
                if b.is_visible():
                    bb = b.bounding_box()
                    if bb:
                        out.append((bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2, b))
            except Exception:
                pass
        return out

    def click(self, selectors, label="", settle=600):
        if isinstance(selectors, str):
            selectors = [selectors]
        loc = self._first_visible(selectors, label)
        if not loc:
            return False
        cx, cy = self._center(loc)
        if cx is None:
            self.log("  !! no bbox for", label)
            return False
        bb = loc.bounding_box() or {}
        self._glide(cx, cy)
        self.page.wait_for_timeout(settle)
        self.page.mouse.click(cx, cy)
        self._record_event(cx, cy, bb.get("width", 0), bb.get("height", 0), "click", label)
        self.page.wait_for_timeout(settle)
        self.log("  clicked", label, f"@({int(cx)},{int(cy)})")
        return True

    def click_side(self, selectors, side="left", label="", settle=600):
        if isinstance(selectors, str):
            selectors = [selectors]
        cands = []
        for sel in selectors:
            cands += self._all_visible(sel)
        if not cands:
            self.log("  !! no match for", label, selectors)
            return False
        cands.sort(key=lambda t: t[0])
        cx, cy, loc = cands[0] if side == "left" else cands[-1]
        bb = (loc.bounding_box() or {}) if loc else {}
        self._glide(cx, cy)
        self.page.wait_for_timeout(settle)
        self.page.mouse.click(cx, cy)
        self._record_event(cx, cy, bb.get("width", 0), bb.get("height", 0), "click", label)
        self.page.wait_for_timeout(settle)
        self.log("  clicked", label, f"({side}) @({int(cx)},{int(cy)})")
        return True

    def focus_composer(self):
        loc = self._first_visible(['textarea', '[contenteditable="true"]'], "composer")
        if not loc:
            return False
        cx, cy = self._center(loc)
        self._glide(cx, cy)
        self.page.mouse.click(cx, cy)
        self.page.wait_for_timeout(300)
        return True

    def type(self, text, delay=35):
        self.page.keyboard.type(text, delay=delay)

    def press(self, key):
        self.page.keyboard.press(key)

    def goto_nav(self, text, settle=2000):
        return self.click([f':text-is("{text}")'], label=f"nav:{text}", settle=settle // 2)

    def first_visible(self, selectors, label=""):
        return self._first_visible(selectors, label)

    def all_visible(self, sel):
        return self._all_visible(sel)


def url_from_args():
    u = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("KC_URL", "")
    if not u:
        print("FATAL: pass the tokenized dashboard URL as argv[1] or KC_URL")
        sys.exit(2)
    return u
