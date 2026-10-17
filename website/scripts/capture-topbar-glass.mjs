/**
 * Screenshot harness for the top bar's Liquid Glass pills: the search trigger,
 * the readout capsule and the Request a Feature pill (App.tsx,
 * components/FeedbackPill.tsx) are `components/Glass.tsx` panes now, the same
 * material as the composer dock. Against a REAL gateway, not fixtures.
 *
 * What the evidence has to show, per frame:
 *   - dark, light and kiro-light: the three pills read as glass (lit top and
 *     bottom bands, no border), on the blurred topbar strip, in every polarity
 *     -- kiro-light matters because the old bg-card pills needed an inset ring
 *     there and the glass has to carry its own edge instead;
 *   - hover on the search trigger: the `glass-hover` tint step;
 *   - the readout capsule's segments are the pane's children (the Glass layers
 *     come first), so the collapse rungs in index.css still keep the dot: the
 *     group is pinned to 180px and photographed with only the dot showing;
 *   - offline: the capsule turns `glass-danger` (the gateway is stopped while
 *     the page is open), still a glass, not a flat fill.
 *
 * The glass is opt-in (Settings -> Display -> View -> Translucent panels;
 * utils/liquidGlass.ts): GLASS=on writes the switch before the page boots, so
 * the frames show the material; without it they show the solid fallback the
 * same panes render for a fresh browser, which the PR also has to look right in.
 *
 * Usage:
 *   POD_INFO=<pod-info.json> GLASS=on node scripts/capture-topbar-glass.mjs <out-dir>
 *   OFFLINE=1 STOP_FILE=<path> ... captures only the offline frame; the caller
 *   stops the gateway once the harness has primed, then touches STOP_FILE.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, podInfo } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/topbar-glass'
mkdirSync(OUT, { recursive: true })
const { BASE, authed } = podInfo(readFileSync)
const HEADER = { x: 0, y: 0, width: 1400, height: 44 }
const report = []

async function prime(page, mode, color) {
  await page.goto(authed('/projects'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  await page.evaluate(async ([m, c]) => {
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: m, color: c, onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
  }, [mode, color])
  await page.goto(`${BASE}/projects`, { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const skip = page.getByRole('button', { name: /Skip this version|跳过此版本/ })
  if (await skip.waitFor({ state: 'visible', timeout: 2500 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.locator('[data-topbar-overlay] button.liquid-glass').waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(900)
  check(`${color}/${mode}: no dialog is open over the page`, (await page.getByRole('dialog').count()) === 0)
}

async function assertGlass(page, label) {
  const facts = await page.evaluate(() => {
    const q = (sel) => document.querySelector(sel)
    const search = q('[data-topbar-overlay] button.liquid-glass')
    const capsule = q('.tb-capsule')
    const feedback = q('[data-testid="feedback-pill"]')
    const layersFirst = (el) => {
      const kids = Array.from(el.children)
      const first = kids.findIndex((k) => !k.hasAttribute('data-liquid-glass-layer'))
      return first > 0 && kids.slice(0, first).every((k) => k.hasAttribute('data-liquid-glass-layer'))
    }
    const shown = (el) => Array.from(el.children).filter((k) => !k.hasAttribute('data-liquid-glass-layer') && getComputedStyle(k).display !== 'none').length
    return {
      searchGlass: !!search,
      searchBorder: search ? getComputedStyle(search).borderTopWidth : null,
      capsuleGlass: !!capsule && capsule.classList.contains('liquid-glass'),
      capsuleLayersFirst: capsule ? layersFirst(capsule) : null,
      capsuleShown: capsule ? shown(capsule) : null,
      feedbackGlass: !!feedback && feedback.classList.contains('liquid-glass'),
      dataTheme: document.documentElement.dataset.theme,
      reduceTransparency: document.documentElement.getAttribute('data-reduce-transparency'),
    }
  })
  check(`${label}: search trigger is a glass button`, facts.searchGlass)
  check(`${label}: search trigger has no border`, facts.searchBorder === '0px', String(facts.searchBorder))
  check(`${label}: readout capsule is a glass pane`, facts.capsuleGlass)
  check(`${label}: capsule glass layers precede the segments`, facts.capsuleLayersFirst === true)
  check(`${label}: feedback pill is a glass pane`, facts.feedbackGlass)
  check(`${label}: translucency switch is ${GLASS_ON ? 'on' : 'off (solid fallback)'}`, facts.reduceTransparency === (GLASS_ON ? null : 'on'), String(facts.reduceTransparency))
  return facts
}

const browser = await chromium.launch()
const GLASS_ON = process.env.GLASS === 'on'
const newPage = async (opts) => {
  const page = await browser.newPage(opts)
  if (GLASS_ON) await page.addInitScript(() => localStorage.setItem('mc-liquid-glass', 'on'))
  return page
}
try {
  if (process.env.OFFLINE) {
    // The gateway has just been stopped by the caller; a page opened before the
    // stop is not available to this fresh process, so instead the page loads
    // against the still-running dist server is not an option either -- the
    // caller passes a page that was primed while the gateway lived. Here we do
    // the simple thing: load while alive (caller ensures), then wait for the
    // socket to drop and the dot to go red.
    const page = await newPage({ viewport: { width: 1400, height: 900 } })
    await prime(page, 'dark', '')
    console.log('primed; waiting for the caller to stop the gateway (STOP_FILE)')
    const stopFile = process.env.STOP_FILE
    for (let i = 0; i < 120; i++) {
      await page.waitForTimeout(1000)
      try { readFileSync(stopFile); break } catch { /* not yet */ }
    }
    // The dot's live-region text flips to the offline label once the socket drops.
    await page.locator('.tb-capsule.glass-danger').waitFor({ state: 'visible', timeout: 60000 })
    await page.waitForTimeout(800)
    const facts = await assertGlass(page, 'offline')
    check('offline: capsule carries glass-danger, not a flat fill', await page.locator('.tb-capsule.glass-danger').count() === 1)
    await page.screenshot({ path: join(OUT, 'topbar-offline.png'), clip: HEADER })
    report.push({ frame: 'topbar-offline', ...facts })
  } else {
    for (const [name, mode, color] of [['dark', 'dark', ''], ['light', 'light', ''], ['kiro-light', 'light', 'kiro']]) {
      const page = await newPage({ viewport: { width: 1400, height: 900 } })
      await prime(page, mode, color)
      const facts = await assertGlass(page, name)
      check(`${name}: theme attribute applied`, name === 'kiro-light' ? facts.dataTheme === 'kiro-light' : facts.dataTheme?.endsWith(mode) === true, facts.dataTheme)
      await page.screenshot({ path: join(OUT, `topbar-${name}.png`), clip: HEADER })
      await page.screenshot({ path: join(OUT, `page-${name}.png`) })
      report.push({ frame: `topbar-${name}`, ...facts })
      if (name === 'dark') {
        await page.locator('[data-topbar-overlay] button.liquid-glass').hover()
        await page.waitForTimeout(400)
        await page.screenshot({ path: join(OUT, 'topbar-dark-hover.png'), clip: HEADER })
        await page.mouse.move(700, 600)
      }
      await page.close()
    }
    // The terminal rung: under 200px the right group keeps exactly the
    // capsule's first segment (the connection dot) and every glass layer -- the
    // state the layer-skipping selector in index.css exists to produce. Phone
    // pages drop the capsule entirely (App.mobileSingleTopbar), so the rung is
    // exercised by pinning the desktop group's width; the frame is the group's
    // own box, wherever the pinned width lands it in its track.
    const rung = await newPage({ viewport: { width: 1400, height: 900 } })
    await prime(rung, 'dark', '')
    const shown = await rung.evaluate(() => {
      const right = document.querySelector('.tb-right')
      right.style.width = '180px'
      right.style.flex = 'none'
      return new Promise((res) => requestAnimationFrame(() => requestAnimationFrame(() => {
        const c = document.querySelector('.tb-capsule')
        const kids = Array.from(c.children)
        const layers = kids.filter((k) => k.hasAttribute('data-liquid-glass-layer'))
        const segs = kids.filter((k) => !k.hasAttribute('data-liquid-glass-layer'))
        res({
          containerWidth: right.clientWidth,
          layers: layers.length,
          layersShown: layers.filter((k) => getComputedStyle(k).display !== 'none').length,
          segs: segs.length,
          segsShown: segs.filter((k) => getComputedStyle(k).display !== 'none').length,
          firstShownIsFirstSeg: segs.length > 0 && getComputedStyle(segs[0]).display !== 'none',
        })
      })))
    })
    console.log('rung', JSON.stringify(shown))
    check('rung: the group is under the 200px terminal rung', shown.containerWidth <= 200)
    check('rung: every glass layer still renders', shown.layers > 0 && shown.layersShown === shown.layers)
    check('rung: exactly the first segment (the dot) stays', shown.segsShown === 1 && shown.firstShownIsFirstSeg)
    await rung.waitForTimeout(300)
    const box = await rung.locator('.tb-right').boundingBox()
    check('rung: the group has a box to photograph', !!box && box.width > 0)
    await rung.screenshot({ path: join(OUT, 'topbar-rung-180.png'), clip: { x: Math.max(0, box.x - 40), y: 0, width: box.width + 80, height: 44 } })
    report.push({ frame: 'topbar-rung-180', ...shown })
    await rung.close()
  }
  writeFileSync(join(OUT, process.env.OFFLINE ? 'report-offline.json' : 'report.json'), JSON.stringify(report, null, 2))
  console.log('done', OUT)
} finally {
  await browser.close()
}
