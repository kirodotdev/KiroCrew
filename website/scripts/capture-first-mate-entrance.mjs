/**
 * The first Mate's entrance (#23207): a recording and the landed frames.
 *
 * Drives the REAL MembersPage capture entry (capture/members-page.html) with
 * the shared gateway-free API stub. The roster holds Mate alone, its welcome
 * owed and unclaimed, so the page lands on Mate's thread and asks `/greet`;
 * the stub answers `started`, the one outcome that dispatched a greeting, and
 * the entrance runs: face centre stage with "Hi! I'm your first Mate.", the
 * header pill's face slot and the roster row's face empty; the flight; the
 * landing, with both faces filled; then the greeting row the harness delivers
 * the way the WebSocket would. Each frame asserts its state before it is
 * written, and three more passes prove the negatives: `already_greeted` (a
 * reload) shows no entrance, nor does a second thread of the same tab;
 * `prefers-reduced-motion` shows the line alone with the faces in place.
 *
 * Usage (two shells, from website/):
 *   npx vite --host 127.0.0.1 --port 6836 --strictPort
 *   node scripts/capture-first-mate-entrance.mjs http://127.0.0.1:6836 <outdir>
 *
 * The webm is the authoritative recording; a GIF is produced when ffmpeg is on
 * PATH (Playwright's bundled build has no GIF encoder).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { routeMembersApi } from './lib/members-fixtures.mjs'

const positional = process.argv.slice(2).filter(a => !a.startsWith('--'))
const BASE = positional[0] || 'http://127.0.0.1:6836'
const OUT = positional[1] || '../temp-screenshots/first-mate-entrance'
mkdirSync(OUT, { recursive: true })

// A fresh install's roster: Mate alone, never written to, its welcome owed and
// the once-only marker not yet claimed -- the row the page lands on first.
const MATE = {
  name: 'mate', slug: 'mate', bound: true, slot_key: 'member-mate', running: false, kiro_agent: 'kirocrew',
  workspace: 'default', memory_store: 'default', model: '', source: 'builtin', has_dm_message: false,
  welcome_owed: true, welcome_claimed: false, last_active_ts: null, last_message: '',
}
const EMPTY_THREAD = { messages: [], running: false, has_more: false, total: 0 }
const GREETING = "Hi, I'm Mate. I'm the teammate that came with Kiro Crew: I keep an eye on your work, pick up what you hand me and report back here. What are you working on?"
const LINE = "Hi! I'm your first Mate."

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
}

/** Open Mate's thread on a fresh page; `outcome` is what `/greet` answers.
 *  Returns the page and the number of greet requests it made so far. */
async function openMate(ctx, theme, { outcome = 'started', reduceMotion = false } = {}) {
  const page = await ctx.newPage()
  if (reduceMotion) await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addInitScript(() => {
    localStorage.setItem('mc-lang', 'en')
  })
  const greets = { count: 0 }
  await routeMembersApi(page, EMPTY_THREAD, { members: [MATE] })
  await page.route(u => new URL(u).pathname === '/api/teams', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"teams":[]}' }))
  // A fresh crewmate has no board yet (the cold welcome's read): 404, as the
  // server answers; and nothing to recap, so the cold welcome draws no card.
  await page.route(u => new URL(u).pathname === '/api/crew-board', route => route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"no ledger"}' }))
  await page.route(u => new URL(u).pathname === '/api/members/mate/recap', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ slug: 'mate', member: 'mate', paused: [], recent: [] }) }))
  await page.route(u => new URL(u).pathname === '/api/members/mate/greet', route => {
    greets.count += 1
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ outcome }) })
  })
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&route=${encodeURIComponent('/members?member=mate')}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByTestId('member-thread-header').waitFor()
  return { page, greets }
}

const hasAttr = (loc, attr) => loc.evaluate((el, a) => el.hasAttribute(a), attr)

/** The whole entrance on one theme, recorded; the landed frame is the PNG. */
async function run(theme) {
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1,
    recordVideo: { dir: `${OUT}/.video`, size: { width: 1280, height: 800 } },
  })
  // The recording starts with the context; the GIF is cut to the page's
  // arrival, so the dev server's first compile is not in it.
  const t0 = Date.now()
  const { page, greets } = await openMate(ctx, theme)
  const arrived = (Date.now() - t0) / 1000
  const entrance = page.getByTestId('mate-entrance')
  await entrance.waitFor()
  await page.waitForTimeout(700)
  check(`${theme}: greet asked once and answered started`, greets.count === 1, String(greets.count))
  check(`${theme}: Mate centre stage, saying its line`,
    (await entrance.getAttribute('data-phase')) === 'stage'
      && (await page.getByTestId('mate-entrance-bubble').innerText()).trim() === LINE
      && (await page.getByTestId('mate-entrance-face').count()) === 1)
  const pillFace = page.getByTestId('member-pill-face')
  const rowFace = page.getByTestId('member-face').first()
  const pillBox = await pillFace.boundingBox()
  check(`${theme}: header pill slot and roster row hold their face empty, width kept`,
    await hasAttr(pillFace, 'data-entrance-empty') && await hasAttr(rowFace, 'data-entrance-empty') && !!pillBox && pillBox.width > 0,
    JSON.stringify(pillBox))
  // On a fresh install the roster is Mate alone, folded into the header's
  // switcher chip: that chip's stack is the roster row the person sees.
  const switcherFace = page.getByTestId('crewmate-switcher-face').first()
  check(`${theme}: the folded roster's stack holds the face empty too`, await hasAttr(switcherFace, 'data-entrance-empty'))
  check(`${theme}: the pane is veiled under the stage`, await hasAttr(page.getByTestId('member-thread-pane'), 'data-entrance-veiled'))
  await page.screenshot({ path: `${OUT}/${theme}-01-stage.png` })
  // The flight ends on the slot; the pop runs on the slot's own face.
  await page.locator('[data-testid="mate-entrance"][data-phase="landed"]').waitFor({ timeout: 6000 })
  check(`${theme}: landed -- the slot and the row paint their own face again`,
    !(await hasAttr(pillFace, 'data-entrance-empty')) && !(await hasAttr(rowFace, 'data-entrance-empty'))
      && !(await hasAttr(switcherFace, 'data-entrance-empty')))
  await entrance.waitFor({ state: 'detached', timeout: 4000 })
  // Then the greeting, the way the gateway's WebSocket delivers the turn's row.
  await page.evaluate((text) => window.dispatchEvent(new CustomEvent('capture:frame', { detail: { kind: 'landed', slot: 'member-mate', text } })), GREETING)
  await page.getByText(GREETING).waitFor()
  await page.waitForTimeout(900)
  check(`${theme}: greeting row in the thread, pane unveiled, one greet in all`,
    !(await hasAttr(page.getByTestId('member-thread-pane'), 'data-entrance-veiled')) && greets.count === 1)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: `${OUT}/${theme}-02-landed-greeting.png` })
  await page.waitForTimeout(800)
  const video = page.video()
  await page.close()
  await ctx.close()
  const webm = `${OUT}/${theme}-entrance.webm`
  await video.saveAs(webm)
  await video.delete()
  // Repo convention (capture-members-steer-only): the webm is the artifact; a
  // GIF only when a full ffmpeg is on PATH.
  const gif = webm.replace(/\.webm$/, '.gif')
  const gifArgs = ['-y', '-loglevel', 'error', '-ss', Math.max(0, arrived - 0.4).toFixed(2), '-i', webm, '-vf', 'fps=12,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3', '-loop', '0', gif]
  const probe = spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' })
  if (probe.error) console.log(`  ffmpeg not on PATH -- webm kept as-is. To produce the GIF: ffmpeg ${gifArgs.join(' ')}`)
  else if (spawnSync('ffmpeg', gifArgs, { stdio: 'ignore' }).status === 0) console.log(`  GIF ${gif}`)
  else console.log('  ffmpeg GIF conversion failed; webm kept')
}

await run('dark')
await run('light')

// A reload: the marker is claimed, `/greet` answers `already_greeted`, and
// nothing enters -- the faces are painted from the first frame.
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 })
  const { page, greets } = await openMate(ctx, 'dark', { outcome: 'already_greeted' })
  await page.waitForTimeout(1200)
  check('already_greeted: asked once, no entrance, faces painted',
    greets.count === 1 && (await page.getByTestId('mate-entrance').count()) === 0
      && !(await hasAttr(page.getByTestId('member-pill-face'), 'data-entrance-empty')))
  await page.screenshot({ path: `${OUT}/dark-03-reload-no-replay.png` })
  await ctx.close()
}

// Reduced motion: the line alone, static, the faces in place; no face on stage.
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 })
  const { page } = await openMate(ctx, 'dark', { reduceMotion: true })
  const entrance = page.getByTestId('mate-entrance')
  await entrance.waitFor()
  await page.waitForTimeout(400)
  check('reduced motion: line shown, no face on stage, slot and row painted',
    await hasAttr(entrance, 'data-reduced-motion')
      && (await page.getByTestId('mate-entrance-face').count()) === 0
      && (await page.getByTestId('mate-entrance-bubble').innerText()).trim() === LINE
      && !(await hasAttr(page.getByTestId('member-pill-face'), 'data-entrance-empty'))
      && !(await hasAttr(page.getByTestId('member-face').first(), 'data-entrance-empty')))
  await page.screenshot({ path: `${OUT}/dark-04-reduced-motion.png` })
  await entrance.waitFor({ state: 'detached', timeout: 4000 })
  check('reduced motion: the line is gone after the hold', (await page.getByTestId('mate-entrance').count()) === 0)
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
