/**
 * Scoped styles for the Crew Companion page. Injected once via `<style>{CC_CSS}</style>`,
 * exactly as file-explorer injects FE_CSS. All colours are dashboard CSS variables,
 * never hardcoded, so the page follows every theme. Interactive state is driven off
 * aria attributes (`aria-checked`, `aria-pressed`) so the visual and the accessible
 * state can never disagree.
 */
export const CC_CSS = `
.cc-page { max-width:880px; margin:0 auto; padding:24px 24px 48px; color:var(--text); font-size:13px; }
.cc-head-top { display:flex; align-items:center; gap:10px; }
.cc-turn-off { flex-shrink:0; }
.cc-h1 { margin:0; font-size:18px; font-weight:650; }
.cc-sub { font-size:13px; color:var(--muted); line-height:1.5; margin:6px 0 0; }

.cc-card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; margin-top:16px; }
.cc-card-head { display:flex; align-items:center; gap:6px; margin-bottom:10px; }
.cc-card-title { font-size:12px; font-weight:650; letter-spacing:.2px; margin:0; }
.cc-card-right { margin-left:auto; }

.cc-row { display:flex; align-items:center; gap:8px; padding:7px 0; border-top:1px solid var(--border); font-size:13px; }
.cc-row.is-first { border-top:none; }
.cc-muted { color:var(--muted); font-size:12px; }
.cc-note { color:var(--muted); font-size:12px; margin-top:8px; line-height:1.45; }
.cc-hint { color:var(--muted); font-size:12px; margin-bottom:8px; }

.cc-btn { display:inline-flex; align-items:center; gap:6px; font-size:12px; background:transparent; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:5px 10px; cursor:pointer; }
.cc-btn:hover:not(:disabled) { border-color:var(--accent); }
.cc-btn:disabled { opacity:.5; cursor:default; }

/* Toggle row */
.cc-toggle { display:flex; align-items:flex-start; gap:10px; padding:7px 0; }
.cc-toggle-text { flex:1; min-width:0; }
.cc-toggle-label { display:block; font-size:13px; }
.cc-toggle-hint { display:block; font-size:12px; color:var(--muted); margin-top:1px; }
.cc-switch { position:relative; flex-shrink:0; width:30px; height:18px; margin-top:2px; padding:0; border:none; background:transparent; cursor:pointer; }
.cc-switch:disabled { cursor:default; opacity:.55; }
.cc-switch-track { display:block; width:30px; height:18px; border-radius:999px; background:var(--border); transition:background 160ms ease; }
.cc-switch[aria-checked="true"] .cc-switch-track { background:var(--accent); }
.cc-switch-knob { position:absolute; top:3px; left:3px; width:12px; height:12px; border-radius:50%; background:var(--muted); transition:left 160ms cubic-bezier(.4,0,.4,1), background 160ms ease; pointer-events:none; }
.cc-switch[aria-checked="true"] .cc-switch-knob { left:15px; background:var(--accent-fg, #fff); }

/* Interval row */
.cc-every { display:flex; align-items:center; gap:6px; padding:7px 0; }
.cc-every-label { flex:1; font-size:13px; }
.cc-pill { font-size:12px; background:transparent; color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:3px 9px; cursor:pointer; }
.cc-pill[aria-pressed="true"] { background:var(--accent); color:var(--accent-fg, #fff); border-color:var(--accent); }
.cc-num { width:46px; padding:3px 4px; text-align:center; font-size:12px; border-radius:999px; border:1px solid var(--border); background:transparent; color:var(--muted); }
.cc-num.is-custom { background:var(--accent); color:var(--accent-fg, #fff); }
.cc-num::-webkit-inner-spin-button, .cc-num::-webkit-outer-spin-button { -webkit-appearance:none; appearance:none; margin:0; }
.cc-num { -moz-appearance:textfield; }

/* Reminders */
.cc-add { display:flex; gap:8px; margin-bottom:10px; }
.cc-add-input { flex:1; font-size:13px; padding:7px 10px; border-radius:8px; background:var(--bg); border:1px solid var(--border); color:var(--text); }
.cc-rem-when { font-weight:650; font-variant-numeric:tabular-nums; min-width:92px; }
.cc-rem-text { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.cc-rem-tag { color:var(--muted); font-size:12px; margin-left:auto; }
.cc-rem-done { opacity:.55; }
.cc-icon-btn { font-size:12px; background:transparent; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:2px 8px; cursor:pointer; }
.cc-icon-btn.is-remove { border:none; padding:2px 7px; }
.cc-icon-btn:hover { border-color:var(--accent); }

/* Memories */
.cc-mem-icon { width:18px; height:18px; flex:none; color:var(--muted); }

/* Offline state — shown when the desktop pet is not running */
.cc-offline { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:36px 24px; margin-top:16px; text-align:center; }
.cc-offline-ghost { width:46px; height:46px; color:var(--accent); opacity:.6; margin:0 auto 12px; display:block; }
.cc-offline-title { font-size:15px; font-weight:650; color:var(--text-strong); margin-bottom:6px; }
.cc-offline-body { font-size:13px; color:var(--muted); max-width:430px; margin:0 auto 18px; line-height:1.5; }
.cc-cta { display:inline-flex; align-items:center; gap:7px; font-size:13px; font-weight:600; background:var(--accent); color:var(--accent-fg,#fff); border:none; border-radius:9px; padding:9px 18px; cursor:pointer; }
.cc-cta:hover { background:var(--accent-hover, var(--accent)); }
.cc-cta:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.cc-offline-tip { margin-top:20px; padding-top:14px; border-top:1px solid var(--border); font-size:12px; color:var(--muted); }

/* Quit tip shown in the running (opened) state, under the header subtitle. */
.cc-quit-tip { margin:8px 0 0; font-size:12px; color:var(--muted); }

/* Focus visibility — the repo fails PRs on missing focus affordances. */
.cc-btn:focus-visible, .cc-pill:focus-visible, .cc-num:focus-visible,
.cc-add-input:focus-visible, .cc-icon-btn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.cc-switch:focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:999px; }
`
