// DASH-7 (D115 M89 M90, row b): a Bot's body in the real page, as pixels. The page's clock is stopped and stepped
// 0.1 s at a time, and the whole canvas is shot at each step, with the pose of the preview's head (the pose line)
// and where the Bot's feet are. `hem-measure.py` then finds the Bot's pixels and reads the hem's and the head's
// place against the feet. No server: the browser answers from XR_DIST.
// usage: XR_DIST=<dist> render-slot.sh node hem-row.mjs <walk|carry> <out-dir>
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { open, sleep } from './aura-readout-lib.mjs'

const [mode = 'walk', OUT = `/tmp/hem-${mode}`] = process.argv.slice(2)
mkdirSync(OUT, { recursive: true })
const STEP = 100
const READY = { platform: 'Linux', installed: true, authenticated: true, ready: true, initial_setup_complete: true, repair_required: false, docs_url: '', login_command: '', sso_login_command: '', bundled_cli: false, setup_allowed: true }
// FPS=15: the page's frames come at that rate on the page's own clock (its requestAnimationFrame is a timer of it).
const FPS = Number(process.env.FPS ?? 0)
const RAF = FPS ? [`(() => { const every = 1000 / ${FPS}; window.requestAnimationFrame = cb => setTimeout(() => cb(performance.now()), every); window.cancelAnimationFrame = id => clearTimeout(id) })()`] : []
// VIEW=2 (M112): the page twice as wide and tall, so a far Bot's eyes stand on more pixels; a carry's pixels with it.
const VIEW = Number(process.env.VIEW ?? 1)
const got = await open('probe=1&fixture=1', { viewport: { width: 960 * VIEW, height: 600 * VIEW }, api: { '/api/kiro-prerequisite': READY }, clock: true, init: RAF })
const { page } = got
const out = { mode, fps: FPS || 'rAF', dist: process.env.XR_DIST, load: readFileSync('/proc/loadavg', 'utf8').trim(), stepMs: STEP, frames: [], lines: [] }
const say = (k, v) => { out.lines.push({ k, ...v }); console.log(k, JSON.stringify(v)) }
const read = () => page.evaluate(() => {
  const r = window.__xrPages.read()
  return { head: r.head, turn: r.turn, view: r.view, crew: (r.crew ?? []).map(b => ({ slug: b.slug, state: b.state, feet: b.feet, top: b.top, walking: !!b.walking, act: b.act ?? null, carried: !!b.carried, forward: !!b.forward, drawn: !!b.drawn, seat: !!b.seat, flip: b.flip ?? null })) }
})
let box = null
const canvasBox = async () => box ?? (box = await page.evaluate(() => { const r = document.querySelector('[data-testid="xr-canvas"]').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height } }))
// Where a point of the room is on the screen, as the preview's lens draws it (the same lines as REVIEW-46's clip.mjs).
const onScreen = (r, point) => {
  const d = point.map((p, i) => p - r.head[i])
  const yaw = (r.turn.yawDeg * Math.PI) / 180, pitch = (r.turn.pitchDeg * Math.PI) / 180
  const vx = d[0] * Math.cos(yaw) + d[2] * Math.sin(yaw), vz = -d[0] * Math.sin(yaw) + d[2] * Math.cos(yaw)
  const cy = d[1] * Math.cos(pitch) + vz * Math.sin(pitch), cz = -d[1] * Math.sin(pitch) + vz * Math.cos(pitch)
  const t = Math.tan((r.view.fovDeg * Math.PI) / 360)
  return { x: box.x + ((vx / -cz / (t * (r.view.width / r.view.height)) + 1) * r.view.width) / 2, y: box.y + (box.height - r.view.height) + ((1 - (r.view.shift ?? 0) - cy / -cz / t) * r.view.height) / 2 }
}
let n = 0, ms = 0
/** One still: the page goes on `STEP` ms (in steps of 1/60 s, `between` is called before each), then the canvas is shot. */
const still = async (phase, slug, between = null) => {
  for (let k = 1; k <= 6; k += 1) { if (between) await between(k / 6); await page.clock.runFor(STEP / 6) }
  ms += STEP
  const name = `f${String(n).padStart(3, '0')}`
  n += 1
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: box })
  const r = await read()
  const b = slug ? r.crew.find(c => c.slug === slug) : null
  const f = { name, ms, phase, pose: { head: r.head, turn: r.turn, view: r.view }, slug, feet: b?.feet ?? null, top: b?.top ?? null, walking: b?.walking ?? null, carried: b?.carried ?? null }
  if (b) { f.feetPx = onScreen(r, b.feet); f.topPx = onScreen(r, b.top); f.feetPx.x -= box.x; f.feetPx.y -= box.y; f.topPx.x -= box.x; f.topPx.y -= box.y }
  out.frames.push(f)
  return { r, b }
}
try {
  await page.waitForSelector('[data-testid="xr-canvas"]', { timeout: 120000 })
  await page.waitForFunction(() => !!window.__xrPages, null, { timeout: 60000 })
  const end = Date.now() + 60000
  while (Date.now() < end) { const c = (await read()).crew; if (c.length >= 6 && c.every(b => b.drawn)) break; await sleep(400) }
  await canvasBox()
  await sleep(2500)
  // M186: the guide's card ("Guide from your assistant") stands over the room's lower middle, where a walking Bot's
  // feet go. It is closed by its own close button before any still, the same on every pack, and the log says so.
  const guideTitle = 'Guide from your assistant'
  const guideClosed = await page.evaluate(title => {
    const t = [...document.querySelectorAll('*')].find(el => el.children.length === 0 && el.textContent?.trim() === title)
    if (!t) return 'no guide'
    let card = t.parentElement
    while (card && !card.querySelector('button[aria-label]')) card = card.parentElement
    const b = card?.querySelector('button[aria-label]')
    if (!b) return 'no close button'
    b.click()
    return `closed by "${b.getAttribute('aria-label')}"`
  }, guideTitle)
  await sleep(400)
  say('guide', { closed: guideClosed, left: await page.evaluate(title => [...document.querySelectorAll('*')].some(el => el.children.length === 0 && el.textContent?.trim() === title), guideTitle) })
  // From here the page's time goes only as this row steps it.
  await page.clock.pauseAt(await page.evaluate(() => Date.now()) + 200)
  await page.clock.runFor(200)
  const r0 = await read()
  say('pose', { head: r0.head, turn: r0.turn, view: r0.view, canvas: box })
  say('crew', { crew: r0.crew.map(b => [b.slug, b.state, b.forward, b.feet]) })
  if (mode === 'walk') {
    // The ask held back sends the Bot in front home: a walk of the room's own (REVIEW-46's clip.mjs, `home`).
    const front = r0.crew.find(b => b.forward)
    say('front', { slug: front?.slug ?? null })
    for (let i = 0; i < 4; i += 1) await still('before', front?.slug)
    await page.evaluate(() => window.__xrPages.asksCome(false))
    let walked = false, after = 0
    for (let i = 0; i < 60 && after < 16; i += 1) {
      const { b } = await still('walk', front?.slug)
      if (b?.walking) walked = true
      else if (walked) after += 1
    }
    say('walked', { walked, frames: n })
  }
  if (mode === 'across') {
    // M102: the room's own walk (the Bot in front sent home), filmed ACROSS his look: in a session (the emulator), the
    // eye stood to the side of the walk's line, 1.6 m from its middle, looking at that middle.
    const { enterAura, parkHands, emu, AURA } = await import('./pages-lib.mjs')
    const front = r0.crew.find(b => b.forward)
    const home = (process.env.HOME_AT ?? '-0.551,1.097,-1.923').split(',').map(Number)
    const a = front.feet, m = [(a[0] + home[0]) / 2, (a[1] + home[1]) / 2, (a[2] + home[2]) / 2]
    const d = [home[0] - a[0], home[2] - a[2]], dl = Math.hypot(...d), nv = [-d[1] / dl, d[0] / dl]
    // The side of the line nearer the room's middle (the eye's start).
    const side = nv[0] * -m[0] + nv[1] * -m[2] > 0 ? 1 : -1
    const eye = [m[0] + side * nv[0] * 1.6, 1.6, m[2] + side * nv[1] * 1.6]
    const look = [m[0] - eye[0], m[1] + 0.15 - eye[1], m[2] - eye[2]]
    const yawDeg = Math.atan2(look[0], -look[2]) * 180 / Math.PI, pitchDeg = Math.atan2(look[1], Math.hypot(look[0], look[2])) * 180 / Math.PI
    await page.clock.resume()
    await enterAura(page)
    await parkHands(page)
    await emu.setHead(page, { position: eye, yawDeg: -yawDeg, pitchDeg })
    await emu.frames(page, 30)
    await page.clock.pauseAt(await page.evaluate(() => Date.now()) + 200)
    await page.clock.runFor(200)
    const vw = 960, vh = 600, DEG = Math.PI / 180
    const proj = p => {
      const y = yawDeg * DEG, t = pitchDeg * DEG, q = [p[0] - eye[0], p[1] - eye[1], p[2] - eye[2]]
      const x1 = q[0] * Math.cos(y) + q[2] * Math.sin(y), z1 = -q[0] * Math.sin(y) + q[2] * Math.cos(y)
      const y2 = q[1] * Math.cos(t) + z1 * Math.sin(t), z2 = -q[1] * Math.sin(t) + z1 * Math.cos(t)
      const k = Math.tan(AURA.fovyDeg / 2 * DEG)
      return { x: vw / 2 + (x1 / -z2 / (k * (vw / vh))) * (vw / 2), y: vh / 2 - (y2 / -z2 / k) * (vh / 2) }
    }
    box = { x: 0, y: 0, width: vw, height: vh }
    say('across', { slug: front.slug, from: a, home, eye, yawDeg, pitchDeg, lensDeg: AURA.fovyDeg })
    const shootAt = async (phase, slug) => {
      await page.clock.runFor(STEP); ms += STEP
      const name = `f${String(n).padStart(3, '0')}`; n += 1
      await page.screenshot({ path: `${OUT}/${name}.png`, clip: box })
      const r = await read(); const b = r.crew.find(c => c.slug === slug)
      const f = { name, ms, phase, pose: { eye, yawDeg, pitchDeg, lensDeg: AURA.fovyDeg, session: true }, slug, feet: b?.feet ?? null, top: b?.top ?? null, walking: b?.walking ?? null }
      if (b) { f.feetPx = proj(b.feet); f.topPx = proj(b.top) }
      out.frames.push(f)
      return b
    }
    for (let i = 0; i < 4; i += 1) await shootAt('before', front.slug)
    await page.evaluate(() => window.__xrPages.asksCome(false))
    let walked = false, after = 0
    for (let i = 0; i < 60 && after < 16; i += 1) {
      const b = await shootAt('walk', front.slug)
      if (b?.walking) walked = true
      else if (walked) after += 1
    }
    say('walked', { walked, frames: n })
  }
  if (mode === 'carry1') {
    // DASH-8 (a): the Bot in front, carried one way across his look (left 240 px in 0.6 s), held still 1.5 s, let go.
    // FLIP=1 (M112): a Bot whose art, and body, face the other way (`flip` in the probe), on the room's row.
    const flipped = r0.crew.filter(b => b.flip && !b.seat && b.drawn)
    if (process.env.FLIP === '1' && !flipped.length) throw new Error(`no mirrored Bot on the row: ${JSON.stringify(r0.crew.map(b => [b.slug, b.flip ?? null, !!b.seat]))}`)
    const bot = process.env.FLIP === '1' ? flipped.find(b => b.forward) ?? flipped[0] : r0.crew.find(b => b.forward) ?? r0.crew[0]
    const at = onScreen(r0, [bot.feet[0], bot.feet[1] + 0.12, bot.feet[2]])
    say('carry1-bot', { slug: bot.slug, flip: bot.flip ?? null, feet: bot.feet, screen: at })
    for (let i = 0; i < 4; i += 1) await still('before', bot.slug)
    await page.mouse.move(at.x, at.y)
    await page.mouse.down()
    for (let i = 0; i < 9; i += 1) await still('held', bot.slug)
    const from = { ...at }
    // CARRY_STILLS=3: the same 240 px in 0.3 s (a quick hand).
    const steps = Number(process.env.CARRY_STILLS ?? 6)
    // CARRY_DX: how far, and which way, in pixels of a 960-wide page (M112: +240, to the right, keeps a Bot on the
    // room's left side on the page).
    const CARRY_DX = Number(process.env.CARRY_DX ?? -240)
    for (let i = 0; i < steps; i += 1) await still('walk', bot.slug, async u => { await page.mouse.move(from.x + (CARRY_DX * VIEW * (i + u)) / steps, from.y) })
    // Held still to the end: the last stills are the background where it was lifted from (`hem-measure.py`).
    for (let i = 0; i < 15; i += 1) await still('walk', bot.slug)
    await page.mouse.up()
  }
  if (mode === 'lean') {
    // D118 4: the Bot in front (near and big), carried left, held still, carried right, held still, let go.
    const bot = r0.crew.find(b => b.forward) ?? r0.crew[0]
    const at = onScreen(r0, [bot.feet[0], bot.feet[1] + 0.12, bot.feet[2]])
    say('lean-bot', { slug: bot.slug, feet: bot.feet, screen: at })
    for (let i = 0; i < 4; i += 1) await still('before', bot.slug)
    await page.mouse.move(at.x, at.y)
    await page.mouse.down()
    for (let i = 0; i < 9; i += 1) await still('held', bot.slug)
    let cur = { ...at }
    const go = async (dx, frames, phase) => {
      const from = { ...cur }
      for (let i = 0; i < frames; i += 1) await still(phase, bot.slug, async u => { cur = { x: from.x + (dx * (i + u)) / frames, y: from.y }; await page.mouse.move(cur.x, cur.y) })
    }
    await go(-160, 5, 'carried-left')
    for (let i = 0; i < 8; i += 1) await still('held-left', bot.slug)
    await go(320, 6, 'carried-right')
    for (let i = 0; i < 8; i += 1) await still('held-right', bot.slug)
    await page.mouse.up()
    for (let i = 0; i < 10; i += 1) await still('let-go', bot.slug)
  }
  if (mode === 'carry') {
    const bot = r0.crew.find(b => b.state === 'idle' && !b.forward) ?? r0.crew[0]
    const at = onScreen(r0, [bot.feet[0], bot.feet[1] + 0.17, bot.feet[2]])
    say('carry-bot', { slug: bot.slug, feet: bot.feet, screen: at })
    for (let i = 0; i < 4; i += 1) await still('before', bot.slug)
    await page.mouse.move(at.x, at.y)
    await page.mouse.down()
    for (let i = 0; i < 9; i += 1) await still('held', bot.slug)
    // Left 180 px in 0.5 s, right 360 px in 0.6 s, then held still for 1.5 s, then let go.
    let cur = { ...at }
    const go = async (dx, frames, phase) => {
      const from = { ...cur }
      for (let i = 0; i < frames; i += 1) await still(phase, bot.slug, async u => { cur = { x: from.x + (dx * (i + u)) / frames, y: from.y }; await page.mouse.move(cur.x, cur.y) })
    }
    await go(-180, 5, 'carry-left')
    await go(360, 6, 'carry-right')
    for (let i = 0; i < 15; i += 1) await still('held-still', bot.slug)
    await page.mouse.up()
    for (let i = 0; i < 15; i += 1) await still('let-go', bot.slug)
  }
  out.errors = got.errors.slice(0, 3)
} catch (e) {
  out.threw = String(e?.stack ?? e).slice(0, 600)
  console.log('threw', out.threw)
} finally {
  await got.context.close().catch(() => {})
  await got.browser.close().catch(() => {})
}
writeFileSync(`${OUT}/lines.json`, JSON.stringify(out, null, 1))
console.log(out.threw ? 'FAIL hem-row threw' : `PASS hem-row ${mode} ${out.frames.length} stills`)
// M111: the row judges its own stills by D118 5's bars and ends non-zero over them (JUDGE=0 keeps the stills only).
if (!out.threw && process.env.JUDGE !== '0') {
  const here = new URL('.', import.meta.url).pathname
  for (const tool of ['hem-measure.py', 'feel-judge.py']) {
    const r = spawnSync('python3', [`${here}${tool}`, OUT], { encoding: 'utf8' })
    process.stdout.write(tool === 'feel-judge.py' ? r.stdout : '')
    if (r.status !== 0) {
      if (tool !== 'feel-judge.py') console.log(`FAIL ${tool} ended ${r.status}: ${(r.stderr || '').slice(-300)}`)
      process.exitCode = 1
      break
    }
  }
}
if (out.threw) process.exitCode = 1
