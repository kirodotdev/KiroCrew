/**
 * Screenshot harness for the keyboard focus ring.
 *
 * What it has to prove, and why one frame cannot: the ring's whole purpose is to
 * appear for ONE audience and not the other, so a single frame of a focused
 * control says nothing. Each pair below drives the SAME control two ways --
 * Tab, then a real mouse click -- and the difference between the two frames IS
 * the change.
 *
 * The frames exercise the REAL built stylesheet (`website/dist/assets/src-*.css`,
 * the file `index.css` compiles into) against a minimal fixture rather than a
 * dashboard route. That is deliberate, and it is the honest scope: the change is
 * entirely in the stylesheet's selectors, no component logic participates, and
 * the two states being compared are decided by the browser's own
 * `:focus-visible` heuristic. Driving a real route instead makes the evidence
 * WEAKER, not stronger -- the harness then has to Tab blindly through the app's
 * live tab order and re-find the same element in a second browser context, and
 * both attempts silently captured a NEIGHBOURING control whose own state read as
 * the opposite finding.
 *
 * `:focus-visible` cannot be faked. A programmatic `.focus()` is judged against
 * the last real interaction, and a pointer press inside a context that has
 * already seen a Tab keeps that context's keyboard modality -- so the pointer
 * frame comes from a SECOND context whose only input is ever a mouse click.
 *
 * deviceScaleFactor is 2 against a small viewport: a 2px outline inside a
 * full-page capture is invisible at review size, and every output edge stays far
 * under the 2000px the model provider accepts.
 *
 * Frames:
 *   button-keyboard.png  Tab lands on a plain button   -> global 2px accent ring
 *   button-pointer.png   the same button, clicked      -> no ring at all
 *   focusring-keyboard.png  Tab lands on a `.focus-ring` field -> border + glow
 *   focusring-pointer.png   the same field, clicked    -> ring KEPT on purpose:
 *                        a text input matches :focus-visible even on a click,
 *                        since keyboard input is expected next
 *
 * Usage: node scripts/capture-focus-ring.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/focus-ring'
mkdirSync(OUT, { recursive: true })

const { srv, base } = await serveDist()

/** The compiled stylesheet index.css becomes, found by content hash. */
const cssHref = (() => {
  const assets = readdirSync(new URL('../dist/assets/', import.meta.url))
  const name = assets.find((f) => /^src-.*\.css$/.test(f))
  if (!name) throw new Error('no dist/assets/src-*.css -- run `npm run build` first')
  return `${base}/assets/${name}`
})()
console.log(`stylesheet: ${cssHref.slice(base.length)}`)

/** Two controls, each labelled, on the app's own dark surface. */
const fixture = `<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<link rel="stylesheet" href="${cssHref}">
<style>
  body{background:var(--bg);color:var(--text);font:400 13px/1.5 system-ui;
       margin:0;padding:26px;display:flex;flex-direction:column;gap:26px;width:520px}
  .row{display:flex;flex-direction:column;gap:7px}
  .cap{color:var(--muted);font-size:11px}
</style></head><body>
  <div class="row">
    <span class="cap">plain button — inherits the global ring</span>
    <button id="btn" class="px-3 py-2 rounded-lg border border-border bg-card text-text"
            style="width:210px">Run workflow</button>
  </div>
  <div class="row">
    <span class="cap">text field — carries the focus-ring class</span>
    <input id="fld" class="focus-ring bg-bg-elevated border border-border rounded-md px-3 py-2
           text-text placeholder:text-muted outline-none" style="width:270px"
           placeholder="Session name">
  </div>
  <div class="row">
    <span class="cap">borderless input inside a bordered wrapper — the WRAPPER cues focus</span>
    <div class="flex items-center gap-2 bg-bg-elevated border border-border rounded-md px-3 py-2
                focus-within:border-accent" style="width:270px">
      <input id="emb" class="flex-1 min-w-0 bg-transparent border-none outline-none text-text
             placeholder:text-muted" placeholder="Filter sessions">
    </div>
  </div>
</body></html>`

const browser = await chromium.launch()
const view = { viewport: { width: 560, height: 260 }, deviceScaleFactor: 2, colorScheme: 'dark' }
// Wide enough that the embedded case's WRAPPER border falls inside the crop: the
// focused element there is the inner input, but the cue is painted one box out.
const PAD = 22

async function open(context) {
  const page = await context.newPage()
  await page.setContent(fixture, { waitUntil: 'load' })
  await page.waitForTimeout(320)
  return page
}

/** The focused element's id, its `:focus-visible` verdict, and a padded clip. */
const focused = (page) => page.evaluate((pad) => {
  const el = document.activeElement
  if (!el || el === document.body) return null
  const r = el.getBoundingClientRect()
  return {
    id: el.id, tag: el.tagName.toLowerCase(), matchesVisible: el.matches(':focus-visible'),
    clip: { x: Math.max(0, r.x - pad), y: Math.max(0, r.y - pad),
            width: r.width + pad * 2, height: r.height + pad * 2 },
  }
}, PAD)

async function pair(id, names) {
  // Keyboard: Tab until this control holds focus, so the state is the browser's
  // own verdict on a real key press rather than a scripted focus() call.
  const kb = await browser.newContext(view)
  const kbPage = await open(kb)
  let target = null
  for (let i = 0; i < 8 && !target; i++) {
    await kbPage.keyboard.press('Tab')
    await kbPage.waitForTimeout(70)
    const f = await focused(kbPage)
    if (f && f.id === id) target = f
  }
  if (!target) {
    console.error(`${names[0]}: Tab never reached #${id}`)
    await kb.close()
    return false
  }
  await kbPage.screenshot({ path: `${OUT}/${names[0]}`, clip: target.clip })
  console.log(`${names[0]}  <${target.tag}#${target.id}> :focus-visible=${target.matchesVisible}`)
  await kb.close()

  // Pointer: a SECOND context, whose only input is ever this click.
  const pt = await browser.newContext(view)
  const ptPage = await open(pt)
  await ptPage.click(`#${id}`)
  await ptPage.waitForTimeout(220)
  const after = await focused(ptPage)
  if (!after || after.id !== id) {
    console.error(`${names[1]}: pointer focus landed on <${after ? after.tag : 'none'}>, not #${id}`)
    await pt.close()
    return false
  }
  await ptPage.screenshot({ path: `${OUT}/${names[1]}`, clip: target.clip })
  console.log(`${names[1]}  <${after.tag}#${after.id}> :focus-visible=${after.matchesVisible}`)
  await pt.close()
  return true
}

const ok = [
  await pair('btn', ['button-keyboard.png', 'button-pointer.png']),
  await pair('fld', ['focusring-keyboard.png', 'focusring-pointer.png']),
  // The embedded case, and the reason it earns a frame of its own: most of this
  // codebase's fields are a borderless input inside a bordered wrapper, and there
  // the cue belongs on the WRAPPER -- an inner box-shadow would hug the input rect
  // instead of the visible field boundary, so the two would read as a mismatched
  // double ring. Only the wrapper's border should change here.
  await pair('emb', ['embedded-keyboard.png', 'embedded-pointer.png']),
].every(Boolean)

await browser.close()
srv.close()
console.log(ok ? 'done' : 'INCOMPLETE')
process.exit(ok ? 0 : 1)
