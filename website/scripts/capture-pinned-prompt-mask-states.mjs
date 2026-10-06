/**
 * Still evidence for the pinned-prompt MASK fix (#16842 / supersedes #11539),
 * capturing exactly the states UX Review asked for and the first evidence set
 * missed:
 *
 *   01  collapsed card MID-PUSH (pushUp > 0) — the next prompt is pushing the
 *       banner out; the opaque fill + lower fade must cover the transcript the
 *       card slides over, with no text leaking around the narrower card, and the
 *       fade must still be mounted (not clipped away on the first push frame).
 *   02  expanded card MID-PUSH (pushUp > 0) — the self-review case: the band's
 *       top stays pinned at the fold (never painting over the chat header), while
 *       the card and its opaque fill ride up together so no empty opaque strip is
 *       left over the transcript below the moved card.
 *   03  expanded card AT REST — the lower EdgeFade is visible against the
 *       transcript reply text underneath (from-bg -> transparent).
 *
 * Each state is shot in CLEAN light and CLEAN dark (one `data-theme` on the
 * root, never mixed). The surface is the REAL app: the capture page mounts the
 * real `usePinnedPrompt` hook, the real `PinnedPrompt` card and the real
 * `UserMessage` bubble over a fixture transcript (see
 * capture/pinned-prompt-handoff.tsx). Nothing about the banner is reimplemented.
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort
 *   node scripts/capture-pinned-prompt-mask-states.mjs http://127.0.0.1:6821 \
 *     ../temp-screenshots/pinned-prompt-mask-states
 *
 * ASSERTS as well as photographs: exits nonzero unless each shot reached the
 * state it claims (a push with pushUp > 0, an expanded card, a mounted fade),
 * and — the fix's own guarantees — unless (a) the band's top never rises above
 * the opaque title row's bottom (the header is never painted over), and (b) the
 * opaque band's bottom tracks the card's moved bottom while pushed (no leftover
 * strip).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/pinned-prompt-mask-states'
mkdirSync(OUT, { recursive: true })

const BANNER = '[data-testid="pinned-prompt"]'

function fail(msg) {
  console.error(`FAIL: ${msg}`)
  process.exitCode = 1
}

/** Read the band (the positioned wrapper whose top is the fold), the card, the
 *  opaque fill, the fade, the title row, and the live pushUp + expanded flags
 *  straight off the DOM — all relative to the viewport. */
async function readState(page) {
  return page.evaluate(() => {
    const card = document.querySelector('[data-testid="pinned-prompt"]')
    if (!card) return { hasCard: false }
    // card -> in-flow clip wrapper -> band (positioned; the band sizes to the card)
    const clip = card.parentElement
    const band = clip ? clip.parentElement : null
    // The opaque fill and the lower fade live on the band's ABSOLUTE z-0 mask
    // layer, which shrinks from the bottom with the push.
    const mask = band ? band.querySelector(':scope > .z-0') : null
    const fill = mask ? mask.querySelector('.bg-bg') : null
    const fade = mask ? mask.querySelector('.bg-gradient-to-b.from-bg.to-transparent') : null
    // The opaque title row of the capture overlay (ChatPage's header stand-in):
    // the band must never rise above its bottom edge.
    const titleRow = document.querySelector('[data-capture-root] .border-b')
    const cardBox = card.querySelector('.user-bubble') || card
    const cr = card.getBoundingClientRect()
    const cardBoxR = cardBox.getBoundingClientRect()
    const br = band ? band.getBoundingClientRect() : null
    const maskR = mask ? mask.getBoundingClientRect() : null
    const fillR = fill ? fill.getBoundingClientRect() : null
    const fr = fade ? fade.getBoundingClientRect() : null
    const tr = titleRow ? titleRow.getBoundingClientRect() : null
    const chevron = card.querySelector('button[aria-expanded]')
    const expanded = chevron ? chevron.getAttribute('aria-expanded') === 'true' : false
    // The push rides the CARD; the band and the mask layer never translate (the
    // mask shrinks via its height instead). Read the magnitude from each.
    const ty = (el) => {
      if (!el) return 0
      const t = getComputedStyle(el).transform
      if (!t || t === 'none') return 0
      const m = t.match(/matrix\(([^)]+)\)/)
      if (!m) return 0
      const parts = m[1].split(',').map(Number)
      return Math.abs(parts[5] || 0)
    }
    return {
      hasCard: true,
      expanded,
      cardPush: ty(card),
      bandPush: ty(band),
      cardTop: cr.top,
      cardBottom: cardBoxR.bottom,
      bandTop: br ? br.top : null,
      bandBottom: br ? br.bottom : null,
      maskBottom: maskR ? maskR.bottom : null,
      fillBottom: fillR ? fillR.bottom : null,
      titleBottom: tr ? tr.bottom : null,
      fadeMounted: !!fade,
      fadeTop: fr ? fr.top : null,
      fadeBottom: fr ? fr.bottom : null,
    }
  })
}

/** Scroll the capture scroller to the first offset where a pin is pushed. */
async function scrollToPush(page, { want = 'any' } = {}) {
  for (let top = 0; top <= 4000; top += 6) {
    await page.evaluate(t => { document.querySelector('[data-capture-scroller]').scrollTop = t }, top)
    await page.waitForTimeout(16)
    const s = await readState(page)
    if (!s.hasCard) continue
    const push = Math.max(s.cardPush, s.bandPush)
    if (want === 'expanded' && !s.expanded) continue
    if (push > 2) return { top, state: s }
  }
  return { top: null, state: await readState(page) }
}

async function expandCard(page) {
  const chevron = page.locator(`${BANNER} button[aria-expanded]`)
  if (await chevron.count()) {
    await chevron.first().click()
    await page.waitForTimeout(300)
    return true
  }
  return false
}

async function shoot(page, name) {
  await page.screenshot({ path: join(OUT, `${name}.png`) })
}

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
try {
  for (const theme of ['light', 'dark']) {
    const colorScheme = theme === 'light' ? 'light' : 'dark'
    // --- collapsed mid-push (3-line prompts, default scenario) ---
    {
      const ctx = await browser.newContext({ viewport: { width: 1000, height: 700 }, colorScheme, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(String(e)))
      await page.goto(`${BASE}/capture/pinned-prompt-handoff.html?theme=${theme}`, { waitUntil: 'networkidle' })
      await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
      await page.waitForSelector('[data-display-index="0"]', { timeout: 20000 })
      await page.waitForTimeout(500)

      const { top, state } = await scrollToPush(page, { want: 'any' })
      if (top == null) fail(`[${theme}] collapsed: never reached a pushed pin`)
      else {
        if (state.expanded) fail(`[${theme}] collapsed shot is expanded`)
        if (!state.fadeMounted) fail(`[${theme}] collapsed: lower fade not mounted (clipped away?)`)
        if (state.bandPush > 1) fail(`[${theme}] collapsed: band translated (${state.bandPush}) — it must stay pinned at the fold`)
        if (state.bandTop != null && state.titleBottom != null && state.bandTop < state.titleBottom - 1) {
          fail(`[${theme}] collapsed: band top ${state.bandTop.toFixed(1)} rose above title row bottom ${state.titleBottom.toFixed(1)} — painting over the header`)
        }
        await shoot(page, `01-collapsed-mid-push-${theme}`)
        console.log(`[${theme}] 01 collapsed mid-push at scrollTop ${top}, cardPush=${state.cardPush.toFixed(1)}, bandPush=${state.bandPush.toFixed(1)}`)
      }
      if (errors.length) fail(`[${theme}] collapsed page errors: ${errors.slice(0, 3).join(' | ')}`)
      await ctx.close()
    }

    // --- expanded: at rest (fade visible) + mid-push (band carries the push) ---
    {
      const ctx = await browser.newContext({ viewport: { width: 1000, height: 700 }, colorScheme, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(String(e)))
      // tall=1 gives a 30-line prompt, which earns a chevron so it can expand.
      await page.goto(`${BASE}/capture/pinned-prompt-handoff.html?theme=${theme}&tall=1`, { waitUntil: 'networkidle' })
      await page.waitForSelector('[data-capture-root]', { timeout: 20000 })
      await page.waitForSelector('[data-display-index="0"]', { timeout: 20000 })
      await page.waitForTimeout(500)

      // Land the tall prompt pinned AT REST (pushUp == 0): scroll until it pins,
      // then stop at the last resting offset before a push begins.
      let restTop = null
      for (let t = 0; t <= 4000; t += 6) {
        await page.evaluate(x => { document.querySelector('[data-capture-scroller]').scrollTop = x }, t)
        await page.waitForTimeout(14)
        const s = await readState(page)
        if (s.hasCard && Math.max(s.cardPush, s.bandPush) <= 1) { restTop = t }
        if (s.hasCard && Math.max(s.cardPush, s.bandPush) > 1 && restTop != null) break
      }
      if (restTop == null) fail(`[${theme}] expanded: never found a resting pin`)
      else {
        await page.evaluate(x => { document.querySelector('[data-capture-scroller]').scrollTop = x }, restTop)
        await page.waitForTimeout(150)
        const expanded = await expandCard(page)
        if (!expanded) fail(`[${theme}] expanded: no chevron to expand the tall prompt`)
        await page.waitForTimeout(250)
        const rest = await readState(page)
        if (!rest.expanded) fail(`[${theme}] expanded-at-rest: card did not expand`)
        if (!rest.fadeMounted) fail(`[${theme}] expanded-at-rest: lower fade not mounted`)
        if (rest.maskBottom != null && rest.fadeTop != null && Math.abs(rest.fadeTop - rest.maskBottom) > 2) {
          fail(`[${theme}] expanded-at-rest: fade not anchored to mask bottom (mask ${rest.maskBottom?.toFixed(1)} vs fade ${rest.fadeTop?.toFixed(1)})`)
        }
        await shoot(page, `03-expanded-at-rest-${theme}`)
        console.log(`[${theme}] 03 expanded at rest, fade at ${rest.fadeTop?.toFixed(1)}..${rest.fadeBottom?.toFixed(1)}`)

        // Now push the expanded card out and shoot mid-push.
        const { top, state } = await scrollToPush(page, { want: 'expanded' })
        if (top == null) fail(`[${theme}] expanded mid-push: never reached a pushed expanded pin`)
        else {
          const strip = state.fillBottom != null && state.cardBottom != null
            ? state.fillBottom - state.cardBottom
            : null
          // The mask keeps ROW_PAD_Y (4px) of opaque fill below the card by design
          // (room for --shadow-sm), at rest and while pushed alike. The self-review
          // bug was two-fold: (a) an earlier revision translated the whole BAND up,
          // sliding its opaque fill over the title row and divider; (b) before that,
          // only the card translated while the fill kept full height, leaving an
          // opaque strip that grew with pushUp. Post-fix: the band NEVER translates
          // (header is safe) and the absolute mask layer's height shrinks from the
          // bottom, so the fill's bottom tracks the card and the strip stays at the
          // resting ROW_PAD_Y.
          const ROW_PAD_Y = 4
          if (state.bandPush > 1) fail(`[${theme}] expanded mid-push: band translated (${state.bandPush}) — it must stay pinned at the fold, not ride the push`)
          if (state.cardPush <= 1) fail(`[${theme}] expanded mid-push: card is not carrying the push (cardPush=${state.cardPush})`)
          if (state.bandTop != null && state.titleBottom != null && state.bandTop < state.titleBottom - 1) {
            fail(`[${theme}] expanded mid-push: band top ${state.bandTop.toFixed(1)} rose above title row bottom ${state.titleBottom.toFixed(1)} — painting over the header`)
          }
          if (!state.fadeMounted) fail(`[${theme}] expanded mid-push: lower fade not mounted`)
          if (strip != null && strip > ROW_PAD_Y + 2) fail(`[${theme}] expanded mid-push: opaque strip ${strip.toFixed(1)}px below card bottom (> ROW_PAD_Y ${ROW_PAD_Y}) — fill did not follow the card`)
          await shoot(page, `02-expanded-mid-push-${theme}`)
          console.log(`[${theme}] 02 expanded mid-push at scrollTop ${top}, cardPush=${state.cardPush.toFixed(1)}, bandPush=${state.bandPush.toFixed(1)}, strip=${strip == null ? 'n/a' : strip.toFixed(1)}px (resting pad ${ROW_PAD_Y})`)
        }
      }
      if (errors.length) fail(`[${theme}] expanded page errors: ${errors.slice(0, 3).join(' | ')}`)
      await ctx.close()
    }
  }
  console.log(`wrote ${OUT}`)
} finally {
  await browser.close()
}
