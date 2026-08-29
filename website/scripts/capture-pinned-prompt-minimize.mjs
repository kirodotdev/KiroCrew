/**
 * Screenshot + measurement harness for the pinned-prompt banner's MINIMIZED form,
 * at phone width.
 *
 * Runs the REAL built SPA (website/dist) against a static file server with every
 * /api/** call answered from fixtures — no gateway, no token, no agent. Only the
 * network is stubbed, so the banner, the chip, the config persistence and the
 * scroll-driven pin geometry are the unmodified production path.
 *
 * Viewport is 390x740 deliberately: the card costs a fixed ~55px whatever the
 * screen, which is a rounding error on a desktop transcript and a visible chunk of
 * a phone's. Capturing this at 1280px would photograph a complaint nobody has.
 *
 * It also asserts the one invariant that could DELETE a message. ChatPage hides the
 * pinned prompt's transcript row while the card stands in for it; the chip does not
 * stand in for anything, so minimizing MUST un-hide that row. The unit test covers
 * the predicate, and this covers the wiring — it reads the row's computed
 * visibility in the real app, in both states.
 *
 * Usage: node scripts/capture-pinned-prompt-minimize.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { installApiFixtures, logPageFailures, json } from './lib/api-fixtures.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/pinned-prompt-minimize'
const SLOT = 'chat-pinned'
const PROJECT = '/home/user/workspace/KiroCrew'
const VIEW = { width: 390, height: 740 }
/** The card's own `px-4`. The control may sit over this, never over the text. */
const CARD_PAD_X = 16

mkdirSync(OUT, { recursive: true })

/** Short enough to clamp at 390px but not at 1280px — the phone-only case. */
const PROMPT = 'Clean up the leftover worktrees whose PRs already merged, and leave anything holding uncommitted work.'

const slots = [{
  key: SLOT,
  title: 'Clean up leftover local infrastructure',
  running: false,
  last_message: 'Stopping here, nothing was removed.',
  messages: 8,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const paraOnce = (n) => [
  `Paragraph ${n}. Filler with enough length to give the transcript runway, so the`,
  'incoming prompt can reach the fold and the hand-off can actually complete. The',
  'banner is recomputed from getBoundingClientRect on an animation frame, so the',
  'state it lands in is a pure function of scrollTop — which is what makes this',
  'harness deterministic rather than dependent on gesture momentum.',
].join(' ')
const para = (n) => [paraOnce(n), paraOnce(n + 100), paraOnce(n + 200)].join('\n\n')

const t0 = Date.now() / 1000 - 900
const PIN_TS = t0 + 10
const detail = {
  running: false,
  messages: [
    { role: 'assistant', ts: t0, content: para(1) },
    { role: 'user', ts: PIN_TS, content: PROMPT },
    { role: 'assistant', ts: t0 + 20, content: 'Stopping here, nothing was removed.' },
    { role: 'assistant', ts: t0 + 21, content: para(2) },
    { role: 'assistant', ts: t0 + 22, content: para(3) },
    { role: 'assistant', ts: t0 + 23, content: para(4) },
    { role: 'assistant', ts: t0 + 24, content: para(5) },
  ],
}

// Every assertion below reports through this: a harness whose failure path exits 0
// cannot detect the regression it exists to catch.
const fail = (...parts) => { process.exitCode = 1; console.log(...parts) }

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
  })
  const page = await context.newPage()

  // Shared boot table (prerequisite gate, status, theme, branding …) rather than a
  // private copy: `npm run jscpd` runs as `pretest` at a 0% duplication threshold,
  // so a second copy of that table fails the test command outright.
  await installApiFixtures(page, {
    '/api/chat/slots': slots,
    '/api/chat/nav/resolve-links': { summaries: [] },
    '/api/models': { models: [], default: 'auto' },
    '/api/recent-projects': { dirs: [PROJECT] },
  })
  // Registered AFTER the shared installer, deliberately: Playwright resolves the
  // most RECENTLY registered matching route first, and the shared table matches on
  // an exact pathname so it cannot answer the slot-detail path's id segment.
  await page.route('**/api/chat/slots/*', route => json(route, detail))

  logPageFailures(page)

  // Seeded ONCE, not per navigation. addInitScript runs before every page load, so
  // an unguarded clear() wipes the config on the reload below — which would make the
  // persistence check pass or fail for a reason that has nothing to do with the
  // feature. The sentinel is what makes that check mean something.
  await page.addInitScript(() => {
    if (!localStorage.getItem('mc-harness-seeded')) {
      localStorage.clear()
      localStorage.setItem('mc-harness-seeded', '1')
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot', 'chat-pinned')
    }
  })
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)

  async function scrollTo(top) {
    await page.evaluate(async (t) => {
      const sc = document.querySelector('.chat-container')
      if (sc) sc.scrollTop = t
      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))
    }, top)
    await page.waitForTimeout(350)
  }

  const maxTop = () => page.evaluate(() => {
    const sc = document.querySelector('.chat-container')
    return sc ? sc.scrollHeight - sc.clientHeight : 0
  })

  /**
   * Banner state plus the measurement that matters: whether the PINNED PROMPT'S OWN
   * transcript row is hidden. Located by its rendered text rather than by index, so
   * a virtualizer reflow cannot silently point it at a different row.
   */
  const state = () => page.evaluate((prompt) => {
    const card = document.querySelector('[data-testid="pinned-prompt"]')
    const chip = document.querySelector('[data-testid="pinned-prompt-pill"]')
    let rowHidden = null
    let bubbleW = 0
    for (const row of document.querySelectorAll('[data-display-index]')) {
      if (row.textContent && row.textContent.includes(prompt.slice(0, 40))) {
        rowHidden = getComputedStyle(row).visibility === 'hidden'
        // visibility:hidden preserves layout, so the stood-in-for bubble is
        // still measurable while hidden — the two widths are comparable for
        // the SAME text in the same frame.
        const bub = row.querySelector('.message-bubble')
        bubbleW = bub ? +bub.getBoundingClientRect().width.toFixed(2) : 0
        break
      }
    }
    const minBtn = document.querySelector('[data-testid="pinned-prompt-minimize"]')
    const minRect = minBtn ? minBtn.getBoundingClientRect() : null
    /*
     * The chip and the restored row's action toolbar share the band's vertical
     * space, and the toolbar is right-aligned and permanently visible on touch.
     */
    const chipRect = chip ? chip.getBoundingClientRect() : null
    return {
      card: !!card,
      cardH: card ? +card.getBoundingClientRect().height.toFixed(2) : 0,
      cardW: card ? +card.getBoundingClientRect().width.toFixed(2) : 0,
      chip: !!chip,
      chipH: chip ? +chip.getBoundingClientRect().height.toFixed(2) : 0,
      chipLeft: chipRect ? +chipRect.left.toFixed(2) : null,
      chipRight: chipRect ? +chipRect.right.toFixed(2) : null,
      chipText: chip ? (chip.textContent || '').trim() : '',
      rowHidden,
      bubbleW,
      minLeft: minRect ? +minRect.left.toFixed(2) : null,
      minRight: minRect ? +minRect.right.toFixed(2) : null,
      cardLeft: card ? +card.getBoundingClientRect().left.toFixed(2) : null,
      cardRight: card ? +card.getBoundingClientRect().right.toFixed(2) : null,
      minimizeBtn: !!minBtn,
    }
  }, PROMPT)

  /** Sweep down until the banner satisfies `want`. */
  async function sweepUntil(want, label) {
    let top = 0
    for (let i = 0; i < 90; i++) {
      const max = await maxTop()
      if (top > max) break
      await scrollTo(top)
      const s = await state()
      if (want(s)) { console.log(label, 'at scrollTop', top, JSON.stringify(s)); return s }
      top += 80
    }
    return null
  }

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // 1. BEFORE — the card, at phone width. This is the complaint.
  const carded = await sweepUntil(s => s.card, 'card')
  if (!carded) { fail('FAIL: never reached a pinned card'); await shot('00-no-card'); return finish() }
  await shot('01-card-390px')

  if (carded.rowHidden !== true) {
    fail('FAIL: the pinned row should be HIDDEN while the card stands in for it, got', carded.rowHidden)
  }

  /*
   * WIDTH parity, at the clamped width the card exists to stand in for. The card
   * replaces the bubble pixel-for-pixel, so a width mismatch re-wraps the text on
   * every hand-off. Height parity alone cannot see this.
   */
  console.log(`MEASURED width @${VIEW.width}px: card ${carded.cardW}px vs bubble ${carded.bubbleW}px, delta ${(carded.cardW - carded.bubbleW).toFixed(2)}px`)
  if (!carded.bubbleW) {
    fail('FAIL: could not measure the stood-in-for bubble — the parity check is vacuous')
  } else if (Math.abs(carded.cardW - carded.bubbleW) > 1) {
    fail(`FAIL: card/bubble WIDTH parity broken at ${VIEW.width}px — card ${carded.cardW}px vs bubble ${carded.bubbleW}px; the hand-off will visibly re-wrap`)
  } else {
    console.log('OK: card matches the bubble it stands in for on width')
  }

  /*
   * Taking the control out of flow buys that parity, so the cost it could
   * introduce is its own: running off-screen, or landing over the card's text.
   */
  console.log(`MEASURED control: left ${carded.minLeft}px, right ${carded.minRight}px, cardLeft ${carded.cardLeft}px`)
  if (carded.minLeft === null) {
    fail('FAIL: no minimize control to measure')
  } else if (carded.minLeft < 0) {
    fail(`FAIL: minimize control runs ${(-carded.minLeft).toFixed(2)}px off the left edge at ${VIEW.width}px — unreachable`)
  } else if (carded.minRight > carded.cardLeft + CARD_PAD_X + 0.5) {
    fail(`FAIL: minimize control overlaps the card's text — right ${carded.minRight}px is past the ${CARD_PAD_X}px padding at cardLeft ${carded.cardLeft}px`)
  } else {
    console.log('OK: control is on screen and clear of the card text')
  }

  // 2. Minimize. Clicked through the real control, so the config write and the
  //    mc-config-changed round trip are exercised, not simulated.
  const minimizeBtn = page.locator('[data-testid="pinned-prompt-minimize"]')
  if (!await minimizeBtn.count()) { fail('FAIL: no minimize control on the card'); return finish() }
  await minimizeBtn.first().click()
  await page.waitForTimeout(500)
  const chipped = await state()
  console.log('minimized:', JSON.stringify(chipped))
  await shot('02-chip-390px')

  if (!chipped.chip) fail('FAIL: minimize left no chip — the state is a dead end')
  if (chipped.card) fail('FAIL: the card is still mounted after minimizing')
  // THE invariant. The chip stands in for nothing, so the row must be readable.
  if (chipped.rowHidden !== false) {
    fail('FAIL: minimized row is still hidden — the message is now invisible in BOTH places, got', chipped.rowHidden)
  } else {
    console.log('OK: minimizing un-hid the transcript row')
  }

  const reclaimed = (carded.cardH - chipped.chipH).toFixed(2)
  console.log(`MEASURED: card ${carded.cardH}px -> chip ${chipped.chipH}px, ${reclaimed}px of a ${VIEW.height}px viewport reclaimed`)

  /*
   * The chip is the feature's only trace once minimized persists, and `title`
   * never fires on touch, so the label has to be rendered text.
   */
  console.log(`MEASURED chip text: ${JSON.stringify(chipped.chipText)}`)
  if (!chipped.chipText) {
    fail('FAIL: the chip renders no visible text — a tooltip cannot label it on touch')
  } else {
    console.log('OK: chip carries a visible label')
  }
  /*
   * Left placement is a decision, not a default, so pin it: the chip covers a
   * VISIBLE row, unlike the card, which covers one ChatPage has hidden.
   */
  const bandLeft = 16
  console.log(`MEASURED chip: text ${JSON.stringify(chipped.chipText)}, x ${chipped.chipLeft}-${chipped.chipRight}; card right was ${carded.cardRight}`)
  if (chipped.chipLeft === null) {
    fail('FAIL: no chip rect — the placement check is vacuous')
  } else if (Math.abs(chipped.chipLeft - bandLeft) > 1) {
    fail(`FAIL: chip starts at ${chipped.chipLeft}px, not the band's left edge (${bandLeft}px)`)
  } else {
    console.log(`OK: chip is left-placed at the band edge (card's right edge was ${carded.cardRight}px)`)
  }

  // 3. Persistence: the flag is global chat config, so it must survive a reload.
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const persisted = await sweepUntil(s => s.chip || s.card, 'after-reload')
  if (persisted && persisted.chip && !persisted.card) console.log('OK: minimized state survived a reload')
  else fail('FAIL: minimized state did not persist across a reload:', JSON.stringify(persisted))

  // 4. Restore, through the chip.
  const restoreBtn = page.locator('[data-testid="pinned-prompt-pill"]')
  if (await restoreBtn.count()) {
    await restoreBtn.first().click()
    await page.waitForTimeout(500)
    const restored = await state()
    console.log('restored:', JSON.stringify(restored))
    await shot('03-restored-390px')
    if (!restored.card) fail('FAIL: the chip did not restore the card')
    if (restored.rowHidden !== true) fail('FAIL: restoring should hide the row again, got', restored.rowHidden)
    else console.log('OK: restoring re-hid the transcript row')
  } else {
    fail('FAIL: no chip to restore from after reload')
  }

  return finish()

  async function finish() {
    await browser.close()
    srv.close()
  }
}

main()
