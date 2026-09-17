// Acceptance walk of the composer's inline paste pills, recorded as video +
// numbered screenshots, with a machine-readable results.json. Drives the REAL
// ChatInput (src/composer/__harness__/chatinput.html) in a real Chromium.
//
//   cd website && npx vite --port 5199 --strictPort --host 127.0.0.1 &
//   node scripts/capture-composer-pills.mjs [outDir] [baseUrl]
//
// Every step asserts; the script exits non-zero if any check fails.
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

import { mkdirSync } from 'node:fs';
const OUT = process.argv[2] || '../temp-screenshots/composer-pills';
const BASE = process.argv[3] || 'http://127.0.0.1:5199';
const VIDEO_DIR = `${OUT}/video`;
mkdirSync(OUT, { recursive: true });
const W = 1000, H = 720;
const browser = await chromium.launch({ headless: true });
const ctx = await browser.newContext({ viewport: { width: W, height: H }, permissions: ['clipboard-read', 'clipboard-write'], recordVideo: { dir: VIDEO_DIR, size: { width: W, height: H } } });
await ctx.route(u => u.pathname.startsWith('/api/'), route => {
  const p = new URL(route.request().url()).pathname;
  const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
  if (p === '/api/slash-commands') return json([{ name: 'help', description: 'Show help' }, { name: 'clear', description: 'Clear the conversation' }, { name: 'compact', description: 'Compact context' }]);
  if (p === '/api/skills') return json([{ key: 'prepare-pr', name: 'prepare-pr', description: 'Drive changes to a review-ready PR', source: 'kirocrew' }, { key: 'web-verify', name: 'web-verify', description: 'Screenshot your own front-end change', source: 'kirocrew' }]);
  if (p.startsWith('/api/file-search')) return json({ root: '/repo', results: [{ path: 'website/src/composer/ComposerEditor.tsx', name: 'ComposerEditor.tsx', size: 1200, mtime: 0, kind: 'file' }, { path: 'website/src/components/ChatInput.tsx', name: 'ChatInput.tsx', size: 9000, mtime: 0, kind: 'file' }] });
  return json({});
});
const page = await ctx.newPage();
const errs = []; page.on('pageerror', e => errs.push(String(e).slice(0, 200)));
const R = { steps: [] };
const step = (id, title, ok, detail) => { R.steps.push({ id, title, ok: !!ok, detail }); console.log((ok ? 'PASS ' : 'FAIL ') + id + ' ' + title + (ok ? '' : ' ' + JSON.stringify(detail))); };

// ---------- helpers ----------
const root = () => page.locator('[data-composer-input]');
const val = () => page.evaluate(() => window.__ci.value());
const blocks = () => page.evaluate(() => window.__ci.blocks().map(b => ({ seq: b.seq, lines: b.lines, len: b.content.length })));
const paste = (t) => page.evaluate((t) => { const dt = new DataTransfer(); dt.setData('text/plain', t); document.querySelector('[data-composer-input]').dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt })); }, t);
const pause = (ms = 700) => page.waitForTimeout(ms);
// On-screen caption so the recording explains itself.
const caption = async (n, text) => { await page.evaluate(([n, text]) => { let el = document.getElementById('__cap'); if (!el) { el = document.createElement('div'); el.id = '__cap'; el.style.cssText = 'position:fixed;left:16px;top:14px;z-index:99999;max-width:640px;padding:8px 12px;border-radius:8px;background:#1f1633;color:#fff;font:600 14px/1.35 system-ui,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.25)'; document.body.appendChild(el); } el.innerHTML = `<span style="opacity:.6;font-weight:500">Step ${n}</span> &nbsp; ${text}`; }, [n, text]); await pause(500); };
const shotComposer = async (name, extraTop = 0, extraBottom = 0) => { const r = await root().boundingBox(); await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: Math.max(0, r.x - 24), y: Math.max(0, r.y - 20 - extraTop), width: Math.min(W, r.width + 48), height: r.height + 40 + extraTop + extraBottom } }); };
const shotDialog = async (name) => { const top = await page.evaluate(() => { const d = document.querySelector('[role=dialog]'); return d ? Math.max(0, d.getBoundingClientRect().top - 16) : 0; }); const r = await root().boundingBox(); await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: 0, y: top, width: W, height: Math.min(H - top, r.y + r.height + 24 - top) } }); };
const pillText = (seq) => page.evaluate((seq) => document.querySelector(`[data-paste-seq="${seq}"]`)?.textContent ?? null, seq);
const pressN = async (key, n) => { for (let i = 0; i < n; i++) await page.keyboard.press(key); };
const typeSlow = async (t) => { await page.keyboard.type(t, { delay: 28 }); };

await page.goto(`${BASE}/src/composer/__harness__/chatinput.html`, { waitUntil: 'networkidle' });
await root().waitFor({ timeout: 20000 });
// Push the composer to the bottom like the real app so previews open ABOVE it.
await page.evaluate(() => { document.body.style.cssText = 'margin:0;min-height:100vh;display:flex;flex-direction:column;justify-content:flex-end;padding-bottom:24px;background:#0b0d12'; });
await pause(400);

// ---------- 1. The composer is the rich editor; placeholder ----------
await caption(1, 'The chat composer is the rich editor — no setting to flip. Empty state shows the placeholder.');
const isRich = await page.evaluate(() => { const el = document.querySelector('[data-composer-input]'); return el.tagName !== 'TEXTAREA' && el.getAttribute('contenteditable') === 'true'; });
const ph = await page.evaluate(() => { const el = document.querySelector('[data-composer-input]'); const r = el.getBoundingClientRect(); const o = document.querySelector('[data-composer-placeholder]'); if (!o) return null; const c = o.getBoundingClientRect(); return { text: o.textContent.trim().slice(0, 40), dyTop: Math.round(c.top - r.top), absolute: getComputedStyle(o).position === 'absolute' }; });
await shotComposer('01-empty-placeholder');
step('1', 'The composer is a contenteditable editor (no <textarea>, no setting); the placeholder is an overlay on the first line', isRich && ph && Math.abs(ph.dyTop) <= 8 && ph.absolute, { isRich, ph });

// ---------- 2. Small paste stays plain text ----------
await caption(2, 'A short paste is inserted as plain text.');
await root().click();
await paste('short paste'); await pause(500);
const v2 = await val();
await shotComposer('02-small-paste-plain');
step('2', 'A small paste (below the collapse threshold) is inserted as plain text', v2 === 'short paste' && (await page.locator('[data-paste-seq]').count()) === 0, { v2 });

// ---------- 3. Big paste → inline pill at the caret with first-line snippet ----------
await caption(3, 'Type a sentence, move the caret into the middle, paste 6 lines of code → an inline pill lands exactly at the caret.');
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await typeSlow('Please review this code and tell me what is wrong');
await pressN('ArrowLeft', ' and tell me what is wrong'.length); await pause(400);
const code = 'def main():\n    print("hi")\n    return 0\n\nif __name__ == "__main__":\n    main()';
await paste(code); await pause(700);
const v3 = await val(); const b3 = await blocks(); const t3 = await pillText(1);
await shotComposer('03-big-paste-inline-pill');
step('3', 'A big paste becomes an INLINE pill at the caret (no forced newline), labelled with its first line + line count', v3 === 'Please review this code[ Paste #1 · 6 lines ] and tell me what is wrong' && b3.length === 1 && b3[0].lines === 6 && /def main\(\):/.test(t3) && /6 lines/.test(t3), { v3, b3, t3 });

// ---------- 4. Caret after the pill; arrows jump over it ----------
await caption(4, 'The caret sits right after the pill. ← and → jump over the pill as one unit; typing lands on either side.');
await typeSlow('X'); await pause(300);
const v4a = await val();
await page.keyboard.press('Backspace'); await pause(200);
await page.keyboard.press('ArrowLeft'); await pause(300); await typeSlow('Y'); await pause(300);
const v4b = await val();
await page.keyboard.press('ArrowRight'); await pause(300); await typeSlow('Z'); await pause(400);
const v4c = await val();
await shotComposer('04-arrows-jump-over-pill');
step('4', 'Caret lands after the pill; ←/→ cross it atomically; typing lands on the chosen side', v4a === 'Please review this codeX[ Paste #1 · 6 lines ]X and tell me what is wrong'.replace('codeX', 'code') && v4b === 'Please review this codeY[ Paste #1 · 6 lines ] and tell me what is wrong' && v4c === 'Please review this codeY[ Paste #1 · 6 lines ]Z and tell me what is wrong', { v4a, v4b, v4c });
await page.keyboard.press('Backspace'); await pause(150); await page.keyboard.press('ArrowLeft'); await pause(150); await page.keyboard.press('ArrowLeft'); await pause(150); await page.keyboard.press('Delete'); await pause(300);

// ---------- 5. Backspace right after a pill deletes just the pill; typing right after it then Backspace deletes only the character ----------
await caption(5, 'Backspace right after a pill removes the whole pill. Typing a character after it and pressing Backspace removes only that character.');
await page.keyboard.press('ArrowRight'); await pause(200); // caret was before the pill after the cleanup; hop over it
await typeSlow('Q'); await pause(250); await page.keyboard.press('Backspace'); await pause(300);
const v5a = await val();
step('5a', 'Type-then-Backspace next to a pill deletes only the character (the pill survives)', v5a === 'Please review this code[ Paste #1 · 6 lines ] and tell me what is wrong', { v5a });
await page.keyboard.press('Backspace'); await pause(500);
const v5b = await val(); const b5 = await blocks();
await shotComposer('05-backspace-removes-pill');
step('5b', 'Backspace right after a pill deletes the WHOLE pill in one keystroke and drops its block', v5b === 'Please review this code and tell me what is wrong' && b5.length === 0, { v5b, b5 });
await caption(6, 'Ctrl+Z brings the pill back.');
await page.keyboard.press('Control+Z'); await pause(600);
const v6 = await val();
await shotComposer('06-undo-restores-pill');
step('6', 'Ctrl/Cmd+Z after removing a pill brings it back', v6.includes('[ Paste #1 · 6 lines ]'), { v6 });

// ---------- 7. Keyboard access: Tab to the pill, → hands the caret back, typing never replaces it ----------
await caption(7, 'Keyboard access: Tab focuses the pill; → hands the caret back to the text. Typing never replaces the pill.');
await page.evaluate(() => document.querySelector('[data-paste-seq="1"]').focus()); await pause(500);
const focusedChip = await page.evaluate(() => document.activeElement?.getAttribute('data-paste-seq'));
await shotComposer('07-pill-keyboard-focus');
await page.keyboard.press('ArrowRight'); await pause(200); await typeSlow('W'); await pause(300);
const v7 = await val();
step('7', 'Tab-focused pill (focus ring) → puts the caret after it; the next keystroke inserts text, never replaces the pill', focusedChip === '1' && v7 === 'Please review this code[ Paste #1 · 6 lines ]W and tell me what is wrong', { focusedChip, v7 });
await page.keyboard.press('Backspace'); await pause(200);

// ---------- 8. Click → editable preview above; Esc returns the caret ----------
await caption(8, 'Click the pill: an editable preview opens above it (full content, line count, Cancel / Save).');
await page.locator('[data-paste-seq="1"]').click({ position: { x: 24, y: 8 } });
await page.waitForSelector('[role=dialog] textarea', { timeout: 5000 }); await pause(700);
const p8 = await page.evaluate(() => { const ta = document.querySelector('[role=dialog] textarea'); const d = ta.closest('[role=dialog]').getBoundingClientRect(); const c = document.querySelector('[data-paste-seq="1"]').getBoundingClientRect(); return { lines: ta.value.split('\n').length, editable: !ta.readOnly && !ta.disabled, above: d.bottom <= c.top, label: ta.closest('[role=dialog]').querySelector('[data-testid$="-lines"]').textContent, focused: document.activeElement === ta }; });
await shotDialog('08-preview-open-above');
step('8a', 'Clicking a pill opens the editable preview ABOVE it with the full content and line count', p8.lines === 6 && p8.editable && p8.above && /6 lines/.test(p8.label) && p8.focused, p8);
await caption(9, 'Escape closes the preview and puts the caret right after the pill.');
await page.keyboard.press('Escape'); await pause(400); await typeSlow('Q'); await pause(300);
const v8 = await val();
step('8b', 'Escape closes the preview and hands the caret back right after the pill', v8 === 'Please review this code[ Paste #1 · 6 lines ]Q and tell me what is wrong', { v8 });
await page.keyboard.press('Backspace'); await pause(200);

// ---------- 9. Edit + Save updates content, count and snippet; Cancel discards ----------
await caption(10, 'Edit the paste in the preview and Save: the pill label, line count and stored content all update.');
await page.locator('[data-paste-seq="1"]').click({ position: { x: 24, y: 8 } });
await page.waitForSelector('[role=dialog] textarea'); await pause(400);
await page.fill('[role=dialog] textarea', ''); await page.type('[role=dialog] textarea', 'import os\nprint(os.getcwd())\nx = 1', { delay: 15 }); await pause(500);
await shotDialog('09-preview-edited');
await page.click('[role=dialog] >> text=Save'); await pause(600);
const v9 = await val(); const b9 = await blocks(); const t9 = await pillText(1);
await shotComposer('10-after-save-snippet-updated');
step('9', 'Save writes the edit back: token line count, block content and the pill snippet all update', v9 === 'Please review this code[ Paste #1 · 3 lines ] and tell me what is wrong' && b9[0].lines === 3 && /import os/.test(t9) && /3 lines/.test(t9), { v9, b9, t9 });
await caption(11, 'Cancel discards an edit.');
await page.locator('[data-paste-seq="1"]').click({ position: { x: 24, y: 8 } });
await page.waitForSelector('[role=dialog] textarea'); await pause(300);
await page.fill('[role=dialog] textarea', 'DISCARD ME'); await pause(400);
await page.click('[role=dialog] >> text=Cancel'); await pause(400);
const b10 = await page.evaluate(() => window.__ci.blocks()[0].content);
step('10', 'Cancel discards the edit', b10 === 'import os\nprint(os.getcwd())\nx = 1', { b10 });

// ---------- 11. Long paste: preview fills the room, scrolls, stays in viewport; resize handle ----------
await caption(12, 'Paste 60 long lines at the end. Its preview fills the space above the pill, scrolls both ways, and never leaves the window.');
await page.keyboard.press('End'); await typeSlow(' ');
const long = Array.from({ length: 60 }, (_, i) => `line ${i + 1}: ` + 'lorem ipsum dolor sit amet, consectetur adipiscing elit '.repeat(i % 4 === 0 ? 3 : 1)).join('\n');
await paste(long); await pause(600);
await page.locator('[data-paste-seq="2"]').click({ position: { x: 24, y: 8 } });
await page.waitForSelector('[role=dialog] textarea'); await pause(700);
const s11 = await page.evaluate(() => { const ta = document.querySelector('[role=dialog] textarea'); const p = ta.closest('[role=dialog]').getBoundingClientRect(); const t = ta.getBoundingClientRect(); const c = document.querySelector('[data-paste-seq="2"]').getBoundingClientRect(); return { panelTop: Math.round(p.top), panelBottom: Math.round(p.bottom), chipTop: Math.round(c.top), taH: Math.round(t.height), scrolls: ta.scrollHeight > ta.clientHeight + 2, hScroll: ta.scrollWidth > ta.clientWidth, taInside: t.right <= p.right && t.bottom <= p.bottom, resize: getComputedStyle(ta).resize }; });
await shotDialog('11-preview-long-paste-fills-room');
step('11', 'A 60-line paste: the preview fills the room above the pill, scrolls both axes, stays inside the panel and the viewport', s11.panelTop >= 8 && s11.panelBottom <= s11.chipTop && s11.scrolls && s11.hScroll && s11.taInside && s11.resize === 'none', s11);
await caption(13, 'The resize handle sits on the corner away from the pill (top-right). Drag down-left to shrink…');
const handle = page.locator('[data-testid$="-resize"]');
const hcls = await handle.getAttribute('class');
const size = () => page.evaluate(() => { const ta = document.querySelector('[role=dialog] textarea'); const p = ta.closest('[role=dialog]').getBoundingClientRect(); return { w: Math.round(p.width), h: Math.round(ta.getBoundingClientRect().height), bottom: Math.round(p.bottom), left: Math.round(p.left) }; });
const before12 = await size();
const drag = async (dx, dy) => { const h = await handle.boundingBox(); const x = h.x + h.width / 2, y = h.y + h.height / 2; await page.mouse.move(x, y); await page.mouse.down(); const steps = 12; for (let i = 1; i <= steps; i++) { await page.mouse.move(x + dx * i / steps, y + dy * i / steps); await page.waitForTimeout(25); } await page.mouse.up(); await pause(300); };
await drag(-120, 200); const shrunk = await size();
await shotDialog('12-preview-resized-smaller');
await caption(14, '…and up-right to grow. Width and height change together; the bottom edge stays anchored over the pill.');
await drag(200, -120); const grown = await size();
await shotDialog('13-preview-resized-larger');
step('12', 'Resize handle on the top-right; down-left shrinks, up-right grows (both axes, width clamped at the viewport edge); the bottom edge stays anchored over the pill', /top-0/.test(hcls) && shrunk.w === before12.w - 120 && shrunk.h === before12.h - 200 && grown.w === Math.min(shrunk.w + 200, W - before12.left - 8) && grown.h === shrunk.h + 120 && grown.bottom === before12.bottom, { hcls, before12, shrunk, grown });
await page.keyboard.press('Escape'); await pause(400);

// ---------- 13. Long first line → truncated snippet ----------
await caption(15, 'A long first line is truncated in the pill label; hover shows the full line.');
const t13 = await page.evaluate(() => { const c = document.querySelector('[data-paste-seq="2"]'); const sn = c.querySelector('[data-testid=paste-chip-snippet]'); return { text: c.textContent, snippetW: Math.round(sn.getBoundingClientRect().width), truncated: sn.scrollWidth > sn.clientWidth, title: c.getAttribute('title') }; });
await page.locator('[data-paste-seq="2"]').hover(); await pause(600);
await shotComposer('14-two-pills-snippets');
step('13', 'A long first line is CSS-truncated (≤140px, ellipsis); the hover title carries the WHOLE first line and names the actions', t13.snippetW <= 141 && t13.truncated && /60 lines/.test(t13.text) && t13.title?.startsWith('line 1: lorem ipsum') && !t13.title.includes('…') && t13.title.endsWith('\nClick to edit · drag to reorder'), t13);

// ---------- 14. Drag to reorder: live gap + drop ----------
await caption(16, 'Drag the second pill into the sentence: an insertion caret follows the pointer; drop moves the pill there.');
const synthStart = (seq) => page.evaluate((seq) => { const el = document.querySelector(`[data-paste-seq="${seq}"]`); el.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() })); }, seq);
const synthOver = (x, y) => page.evaluate(([x, y]) => new Promise(res => { document.querySelector('[data-composer-input]').dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer(), clientX: x, clientY: y })); requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(res, 80))); }), [x, y]);
const synthDrop = (x, y) => page.evaluate(([x, y]) => { const r = document.querySelector('[data-composer-input]'); r.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer(), clientX: x, clientY: y })); document.querySelector('.pill-host.dragging')?.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() })); }, [x, y]);
const target = await page.evaluate(() => { const el = document.querySelector('[data-composer-input]'); const tn = Array.from(el.querySelectorAll('span[data-lexical-text]')).find(s => s.textContent.startsWith('Please')); const r = document.createRange(); r.setStart(tn.firstChild, 'Please '.length); r.setEnd(tn.firstChild, 'Please '.length); const b = r.getBoundingClientRect(); return { x: b.left, y: b.top + b.height / 2 }; });
await synthStart(2); await pause(300);
// sweep the pointer across a few positions so the gap visibly travels in the recording
const pillBox = await page.locator('[data-paste-seq="1"]').boundingBox();
for (const x of [pillBox.x + pillBox.width + 60, pillBox.x - 40, target.x + 120, target.x]) { await synthOver(x, target.y); await pause(350); }
const mid14 = await page.evaluate(() => { const gap = document.querySelector('.drop-gap'); const host = document.querySelector('.pill-host.dragging'); return { gaps: document.querySelectorAll('.drop-gap').length, dimmed: document.querySelectorAll('.pill-host.dragging').length, gapWidth: gap ? Math.round(gap.getBoundingClientRect().width) : 0, cursorBg: gap?.firstElementChild ? getComputedStyle(gap.firstElementChild).backgroundColor : '', cursorHeight: gap?.firstElementChild ? Math.round(gap.firstElementChild.getBoundingClientRect().height) : 0, cursorWidth: gap?.firstElementChild ? Math.round(gap.firstElementChild.getBoundingClientRect().width) : 0, gapOpacity: gap ? Number(getComputedStyle(gap).opacity) : 0, hostOpacity: host ? Number(getComputedStyle(host).opacity) : 1 }; });
await shotComposer('15-mid-drag-gap-opens');
await synthDrop(target.x, target.y); await pause(600);
const v14 = await val(); const after14 = await page.evaluate(() => ({ gaps: document.querySelectorAll('.drop-gap').length, dimmed: document.querySelectorAll('.pill-host.dragging').length, nested: document.querySelectorAll('.pill-host .pill-host').length }));
await shotComposer('16-after-drop-reordered');
// The indicator must be RENDERED, not merely mounted: an invisible caret is exactly the regression
// a DOM count cannot see (the styling once lived in a stylesheet that was swapped out). The host is
// deliberately 0 wide (the text does not part); the caret inside must be visible.
step('14', 'Dragging a pill dims it and shows an insertion caret in the text under the pointer; dropping moves it there (no nesting, caret cleaned up)', mid14.gaps === 1 && mid14.dimmed === 1 && mid14.gapWidth === 0 && mid14.gapOpacity > 0.9 && mid14.cursorBg !== 'rgba(0, 0, 0, 0)' && mid14.cursorHeight >= 12 && mid14.cursorWidth >= 2 && mid14.hostOpacity < 1 && v14 === 'Please [ Paste #2 · 60 lines ]review this code[ Paste #1 · 3 lines ] and tell me what is wrong ' && after14.gaps === 0 && after14.dimmed === 0 && after14.nested === 0, { mid14, v14, after14 });

// ---------- 15. Drag ghost geometry ----------
const ghost = await page.evaluate(() => new Promise(res => { const o = DataTransfer.prototype.setDragImage; DataTransfer.prototype.setDragImage = function (el, x, y) { res({ x, y, pad: el.style.paddingTop, opacity: el.firstElementChild.style.opacity, realPills: el.querySelectorAll('[data-paste-seq]').length }); return o.call(this, el, x, y); }; const el = document.querySelector('[data-paste-seq="1"]'); el.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() })); setTimeout(() => { document.querySelector('.pill-host.dragging')?.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() })); }, 50); setTimeout(() => res(null), 500); }));
step('15', 'The drag image is a translucent clone anchored 22px BELOW the pointer, so the drop point is never covered', ghost && ghost.y === 0 && ghost.pad === '22px' && Number(ghost.opacity) < 1 && ghost.realPills === 0, ghost);
await pause(200);

// ---------- 16. ✕ removes ----------
await caption(17, 'The ✕ on a pill removes it (no preview opens).');
await page.locator('[data-paste-seq="2"] button').click(); await pause(500);
const v16 = await val(); const b16 = await blocks();
await shotComposer('17-x-removes-pill');
step('16', 'The ✕ on a pill removes it and its block; no preview opens', v16 === 'Please review this code[ Paste #1 · 3 lines ] and tell me what is wrong ' && b16.length === 1 && (await page.locator('[role=dialog]').count()) === 0, { v16, b16 });

// ---------- 17. Chinese IME ----------
await caption(18, 'Chinese IME: while composing "nihao" nothing is committed; the commit inserts 你好 after the pill.');
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace'); await pause(400);
await paste('l1\nl2\nl3\nl4\nl5'); await pause(300); await typeSlow(' ');
const cdp = await ctx.newCDPSession(page);
for (const t of ['n', 'ni', 'nih', 'niha', 'nihao']) { await cdp.send('Input.imeSetComposition', { text: t, selectionStart: t.length, selectionEnd: t.length }); await pause(160); }
await shotComposer('18-ime-composing');
const mid17 = await val();
await cdp.send('Input.insertText', { text: '你好' }); await pause(500);
const v17 = await val(); const pills17 = await page.locator('[data-paste-seq]').count();
await shotComposer('19-ime-committed');
step('17', 'Chinese IME: during composition the value is not committed; commit inserts 你好 after the pill, pill intact', /^\[ Paste #\d+ · 5 lines \] 你好$/.test(v17) && pills17 === 1, { mid17, v17, pills17 });

// ---------- 18. Shift+Enter / Enter send / history ----------
await caption(19, 'Shift+Enter adds a line. Enter sends: the host gets the token value + blocks (ChatPage expands them), then the composer clears.');
await page.keyboard.press('Shift+Enter'); await typeSlow('second line'); await pause(400);
const v18a = await val();
await shotComposer('20-shift-enter-newline');
await page.keyboard.press('Enter'); await pause(600);
const sent = await page.evaluate(() => window.__ci.sent()); const v18b = await val(); const b18 = await blocks();
await shotComposer('21-after-send-cleared');
step('18', 'Shift+Enter adds a line; Enter sends the token value + blocks (expanded by the host) and clears pills/blocks', /^\[ Paste #\d+ · 5 lines \] 你好\nsecond line$/.test(v18a) && sent.length === 1 && sent[0] === 'l1\nl2\nl3\nl4\nl5 你好\nsecond line' && v18b === '' && b18.length === 0, { v18a, sent, v18b, b18 });
await caption(20, '↑ recalls the last prompt (the expanded text, as the app stores it); ↓ at the end returns to the empty draft.');
await page.keyboard.press('ArrowUp'); await pause(500);
const v19 = await val();
await shotComposer('22-history-recall');
await page.keyboard.press('Control+End'); await pause(200); await page.keyboard.press('ArrowDown'); await pause(400);
const v19b = await val();
step('19', '↑ on an empty composer recalls the last sent prompt (expanded text); ↓ at the end walks back out to the empty draft', v19 === 'l1\nl2\nl3\nl4\nl5 你好\nsecond line' && v19b === '', { v19, v19b });

// ---------- 19. Pickers ----------
await caption(21, 'Typed triggers still work: "/" opens the command menu…');
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await typeSlow('/'); await pause(600);
const slash = await page.evaluate(() => { const m = document.querySelector('[role=listbox]'); return m ? { items: m.querySelectorAll('[role=option]').length } : null; });
await shotComposer('23-slash-menu', 170);
await page.keyboard.press('Escape'); await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await caption(22, '…"$" opens the skill picker (Enter inserts the skill)…');
await typeSlow('use $pre'); await pause(700);
const skill = await page.evaluate(() => ({ items: Array.from(document.querySelectorAll('[role=option]')).map(e => e.textContent.trim().slice(0, 40)) }));
await shotComposer('24-skill-picker', 100);
await page.keyboard.press('Enter'); await pause(500);
const vSkill = await val();
await shotComposer('25-skill-inserted');
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await caption(23, '…and "@" opens the file mention (Enter inserts the path).');
await typeSlow('look at @Compos'); await pause(900);
const file = await page.evaluate(() => ({ items: Array.from(document.querySelectorAll('[role=option]')).map(e => e.textContent.trim().slice(0, 60)) }));
await shotComposer('26-file-mention-picker', 140);
await page.keyboard.press('Enter'); await pause(500);
const vFile = await val(); const fileLog = await page.evaluate(() => window.__ci.logs().find(l => l.startsWith('file ')));
await shotComposer('27-file-inserted');
step('20', 'The "/" command menu, "$" skill picker and "@" file mention all open, and Enter inserts the picked item at the caret', slash && slash.items > 0 && skill.items.some(t => /prepare-pr/.test(t)) && /^use \$prepare-pr ?$/.test(vSkill) && file.items.some(t => /ComposerEditor/.test(t)) && /ComposerEditor\.tsx/.test(vFile) && /ComposerEditor/.test(fileLog || ''), { slash, skill, vSkill, file, vFile, fileLog });
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');

// ---------- 20. Copy expands ----------
await caption(24, 'Copying a selection that spans a pill puts the expanded pasted text on the clipboard.');
await typeSlow('before '); await paste('c1\nc2\nc3\nc4'); await typeSlow(' after'); await pause(300);
await page.keyboard.press('Control+A'); await pause(300);
const clip = await page.evaluate(() => new Promise(res => { document.addEventListener('copy', (e) => { res({ text: e.clipboardData.getData('text/plain') }); }, { once: true }); const ok = document.execCommand('copy'); setTimeout(() => res({ text: null, ok }), 500); }));
await shotComposer('28-select-all-copy');
step('21', 'Copying a selection that spans a pill puts the EXPANDED pasted text on the clipboard (no "[ Paste #N ]" zombies)', clip.text && clip.text.includes('c1\nc2\nc3\nc4') && !clip.text.includes('[ Paste'), clip);
await caption(25, 'Round 2 — editing then Escape: the first press keeps the panel and shows the unsaved hint; the second discards.');
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await typeSlow('Notes '); await paste('n1\nn2\nn3\nn4'); await pause(300);
await page.click('[data-paste-seq="1"]'); await pause(500);
await page.click('[role=dialog] textarea'); await page.keyboard.press('Control+End'); await page.keyboard.type('\nn5 (edited)'); await pause(200);
const liveCount = await page.textContent('[data-testid="paste-preview-editor-lines"]');
await page.keyboard.press('Escape'); await pause(300);
const hint = await page.textContent('[data-testid="paste-preview-editor-unsaved"]').catch(() => null);
const openAfterFirstEsc = await page.locator('[role=dialog]').count();
await shotDialog('29-unsaved-hint-after-escape');
await page.keyboard.press('Escape'); await pause(300);
const b22 = (await blocks())[0];
step('22', 'Live line count follows the edit; a dirty Escape keeps the panel open with the unsaved hint; a second Escape discards and the block is unchanged', liveCount === '5 lines' && hint === 'Unsaved changes — press Esc again to discard' && openAfterFirstEsc === 1 && (await page.locator('[role=dialog]').count()) === 0 && b22.lines === 4, { liveCount, hint, openAfterFirstEsc, lines: b22.lines });

await caption(26, 'Editing then clicking back into the composer SAVES the edit instead of discarding it.');
await page.click('[data-paste-seq="1"]'); await pause(500);
await page.click('[role=dialog] textarea'); await page.keyboard.press('Control+End'); await page.keyboard.type('\nn5 (kept)'); await pause(200);
const r23 = await root().boundingBox(); await page.mouse.click(r23.x + r23.width - 120, r23.y + 8); await pause(400);
const b23 = (await blocks())[0]; const b23content = await page.evaluate(() => window.__ci.blocks()[0].content);
await shotComposer('30-click-away-saved');
step('23', 'A pointerdown outside a preview with unsaved edits SAVES them: the panel closes and the block carries the edit', (await page.locator('[role=dialog]').count()) === 0 && b23.lines === 5 && b23content.endsWith('n5 (kept)'), { lines: b23.lines, tail: b23content.slice(-12) });

await caption(27, 'Escape during an IME composition cancels the candidate list, not the panel.');
await page.click('[data-paste-seq="1"]'); await pause(500);
await page.evaluate(() => { const ta = document.querySelector('[role=dialog] textarea'); ta.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true })); ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); });
await pause(200);
const openDuringIme = await page.locator('[role=dialog]').count();
await page.evaluate(() => { document.querySelector('[role=dialog] textarea').dispatchEvent(new CompositionEvent('compositionend', { bubbles: true })); });
await pause(120); await page.keyboard.press('Escape'); await pause(300);
step('24', 'An Escape the IME owns (mid-composition) leaves the preview open; a plain Escape afterwards closes it', openDuringIme === 1 && (await page.locator('[role=dialog]').count()) === 0, { openDuringIme });
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');

// ---------- 25. Round 2: the preview flips BELOW the pill when there is no room above ----------
await caption(28, 'With no room above (tall viewport, long paste), the preview opens BELOW the pill; the resize grip moves to the bottom-right corner.');
await page.setViewportSize({ width: W, height: 1600 }); await pause(300);
await typeSlow('Long '); await paste(Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join('\n')); await pause(300);
await page.click('[data-paste-seq="1"]'); await pause(600);
const geom = await page.evaluate(() => { const d = document.querySelector('[role=dialog]'); const p = document.querySelector('[data-paste-seq="1"]'); if (!d || !p) return null; const dr = d.getBoundingClientRect(), pr = p.getBoundingClientRect(); const grip = d.querySelector('[data-testid$="-resize"]'); return { below: dr.top > pr.bottom, gripBottomRight: grip.className.includes('bottom-0'), gripTransform: getComputedStyle(grip).transform, inside: dr.bottom <= innerHeight - 4, top: pr.bottom - 120, height: Math.min(1600, dr.bottom + 40) }; });
if (geom) await page.screenshot({ path: `${OUT}/31-preview-opens-below-pill.png`, clip: { x: 0, y: Math.max(0, geom.top), width: W, height: geom.height - Math.max(0, geom.top) } });
step('25', 'With no room above, the preview opens BELOW the pill, stays inside the viewport, and the resize grip sits bottom-right un-mirrored', geom && geom.below && geom.gripBottomRight && (geom.gripTransform === 'none' || geom.gripTransform === '') && geom.inside, geom);
await page.keyboard.press('Escape'); await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await page.setViewportSize({ width: W, height: H }); await pause(300);

// ---------- 26. Round 7: the preview WRITES THROUGH; Cancel restores ----------
await caption(29, 'Round 7 — the preview writes through: typing updates the pill label and the stored block at once (nothing lives only in the panel); Cancel puts the original back.');
await typeSlow('Draft '); await paste('w1\nw2\nw3\nw4'); await pause(300);
await page.click('[data-paste-seq="1"]'); await pause(500);
await page.click('[role=dialog] textarea'); await page.keyboard.press('Control+Home'); await page.keyboard.type('live edit: ', { delay: 20 }); await pause(400);
const live26 = await page.evaluate(() => { const ta = document.querySelector('[role=dialog] textarea'); const b = window.__ci.blocks()[0]; const c = document.querySelector('[data-paste-seq="1"]'); return { open: !!ta, focused: document.activeElement === ta, caret: ta.selectionStart, taHead: ta.value.slice(0, 11), blockHead: b.content.slice(0, 11), blockLines: b.lines, snippet: c.querySelector('[data-testid=paste-chip-snippet]')?.textContent }; });
await shotDialog('32-preview-live-write-through');
await page.click('[role=dialog] >> text=Cancel'); await pause(400);
const b26 = await page.evaluate(() => window.__ci.blocks()[0].content);
step('26', 'Typing in the preview writes through — the stored block and the pill snippet update at once while the textarea keeps focus and its caret; Cancel restores the original', live26.open && live26.focused && live26.caret === 11 && live26.taHead === 'live edit: ' && live26.blockHead === 'live edit: ' && live26.blockLines === 4 && /^live edit: w1/.test(live26.snippet || '') && b26 === 'w1\nw2\nw3\nw4' && (await page.locator('[role=dialog]').count()) === 0, { live26, b26 });
await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');

// ---------- 27. Round 7: the open preview follows its pill on resize ----------
await caption(30, 'The open preview follows its pill: narrow the window so the line re-wraps — the panel re-anchors to where the pill went.');
await typeSlow('A sentence long enough to wrap once the window narrows, with the pill sitting at its end '); await paste('r1\nr2\nr3\nr4'); await pause(300);
await page.click('[data-paste-seq="1"]'); await pause(500);
const anchored = () => page.evaluate(() => { const d = document.querySelector('[role=dialog]'); const p = document.querySelector('[data-paste-seq="1"]'); if (!d || !p) return null; const dr = d.getBoundingClientRect(), pr = p.getBoundingClientRect(); return { dLeft: Math.round(dr.left), dBottom: Math.round(dr.bottom), pLeft: Math.round(pr.left), pTop: Math.round(pr.top) }; });
const before27 = await anchored();
const NARROW = 620;
await page.setViewportSize({ width: NARROW, height: H }); await pause(700);
const after27 = await anchored();
await shotDialog('33-preview-follows-pill-after-resize');
// Same placement rule the editor applies on open: left clamped into the viewport
// (panel 420 wide, 8px margin), bottom edge 6px over the pill.
const near = (a, b) => Math.abs(a - b) <= 1;
step('27', 'Narrowing the window re-wraps the line and moves the pill; the open preview re-anchors to it instead of drifting', before27 && after27 && (before27.pLeft !== after27.pLeft || before27.pTop !== after27.pTop) && near(after27.dLeft, Math.max(8, Math.min(after27.pLeft, NARROW - 420 - 8))) && near(after27.dBottom, after27.pTop - 6), { before27, after27 });
await page.keyboard.press('Escape'); await page.keyboard.press('Control+A'); await page.keyboard.press('Backspace');
await page.setViewportSize({ width: W, height: H }); await pause(300);

await caption(31, 'Done — every check passed.'); await pause(1200);

R.errs = errs; R.pass = R.steps.filter(s => s.ok).length; R.total = R.steps.length;
writeFileSync(`${OUT}/results.json`, JSON.stringify(R, null, 2));
const videoPath = await page.video().path();
await ctx.close(); await browser.close();
console.log(`\n${R.pass}/${R.total} passed; page errors: ${errs.length}`);
console.log('VIDEO ' + videoPath);
process.exitCode = R.pass === R.total ? 0 : 1;
