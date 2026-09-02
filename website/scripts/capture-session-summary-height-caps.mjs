/**
 * Screenshot + geometry harness for the two height caps in the session-summary
 * panel: the expanded open-items block, and the expanded project-notes footer.
 *
 * Both are bounded at 33vh and scroll inside themselves. A still cannot show
 * that a cap BINDS — a short list and a capped list look identical in a frame —
 * so this asserts the geometry it photographs:
 *
 *   - a tall list's scroll container is shorter than its content (the cap binds)
 *     and is no taller than 33vh,
 *   - the toggle stays in the viewport after the inner list is scrolled to its
 *     end (it lives OUTSIDE the scroll container, which is the whole point),
 *   - a SHORT list's container is NOT scrollable (natural height, no scrollbar),
 *     which is the case a cap could silently break by pinning every list to 33vh.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures, so no gateway, no
 * kiro-cli and no dashboard token are involved.
 *
 * Usage: npm run build && node scripts/capture-session-summary-height-caps.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-summary-height-caps'
const SLOT = 'session-summary-height-caps'
const VIEWPORT = { width: 1400, height: 900 }
/** The cap under test, resolved against the viewport this run uses. */
const CAP_PX = Math.round(VIEWPORT.height * 0.33)
/** The two capped surfaces, matched by a class pair each carries uniquely. The
 *  cap now governs the CARD, so the scroller is a child of it rather than the
 *  capped element itself — measuring the capped node alone would no longer see
 *  whether anything actually scrolls. */
const ITEMS_CARD = 'div[class*="max-h-[33vh]"][class*="bg-bg-accent"]'
const NOTES_CARD = 'div[class*="max-h-[33vh]"][class*="bg-card"]'

mkdirSync(OUT, { recursive: true })

const slots = [
  {
    key: SLOT,
    title: 'Session summary — height caps',
    running: false,
    messages: 60,
    agent: 'kirocrew',
    modified: Math.floor(Date.now() / 1000),
    last_ts: '2026-08-20T20:00:00Z',
    folder_id: '',
  },
]

/** A long session: enough open items and notes that BOTH caps bind. Every
 *  intent is needs-you so every one of them contributes to the block. */
const TALL_TITLES = [
  'Raise the storage rails to 50',
  'Bound the open-items block at 33vh',
  'Bound the project-notes footer',
  'Stop done intents feeding the chip',
  'Reconcile the config descriptions',
  'Update the Configuration table',
  'Decide the truncation flag shape',
  'Re-pin the screenshot SHAs',
  'Prune the merged local branches',
  'Answer the Design Review concerns',
  'Confirm the parse-path defaults',
  'Re-run the frontend gates',
  'Check the dead-keys ratchet',
  'Verify the caps on a short window',
]

function tallSummary() {
  return {
    enabled: true,
    stale: false,
    generated_at: Date.now() / 1000 - 240,
    user_turns: 60,
    last_activity: '2026-08-20T20:00:00Z',
    constraints: Array.from(
      { length: 18 },
      (_, i) => `Operational note ${i + 1} — a recurring fact about how this project is run.`,
    ),
    intents: TALL_TITLES.map((title, i) => ({
      title,
      initial_intent: null,
      progress: ['Where this stands right now.'],
      next_steps: [
        { what: `${title} — first open step`, why: 'It blocks the next one.', expect: 'A green gate.' },
        { what: `${title} — second open step`, why: 'It is still open.', expect: 'One less thing.' },
      ],
      ranges: [[i * 4 + 1, i * 4 + 3]],
      status: 'completed',
      verified: false,
      state: 'needs-you',
      last_touched_turn: 60 - i,
      origin_turn: null,
    })),
  }
}

/** A short session: enough open items that the block still caps at 3 and offers
 *  an overflow row (so it CAN be expanded), but few enough that the expanded
 *  list stays well under 33vh. This is the case rule 2 protects — a cap that
 *  pinned every expanded list to 33vh would pass the tall checks and fail here. */
function shortSummary() {
  const s = tallSummary()
  s.intents = s.intents
    .slice(0, 5)
    .map(intent => ({ ...intent, next_steps: [intent.next_steps[0]] }))
  s.constraints = s.constraints.slice(0, 2)
  s.user_turns = 12
  return s
}

/** The localStorage seeds that put the summary panel on screen for this slot.
 *
 *  These go through `stubDashboardApi`'s `localStorageEntries`, NOT a separate
 *  `addInitScript`. The stub's own initializer calls `localStorage.clear()`, and
 *  init scripts run in registration order — so a caller that seeds through its
 *  own initializer is relying on having registered after the stub. Get that order
 *  wrong and the clear wipes the seeds, the panel never opens, and the capture
 *  fails as a role-lookup timeout that says nothing about the cause.
 */
const PANEL_SEEDS = {
  'mc-active-slot': SLOT,
  ['mc-activity-open:' + SLOT]: 'true',
  'mc-privacy-notice-v1': '1',
  ['mc-panel-tabs:' + SLOT]: JSON.stringify({
    tabs: [{ id: 'summary', kind: 'summary', title: 'Summary' }],
    activeId: 'summary',
  }),
}

async function openPanel(page, base) {
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
}

async function shoot(page, name) {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Geometry for one capped surface: the card's own height, and the client vs
 *  scroll height of the scroller inside it. Returns null when the card is absent
 *  (collapsed, or not capped because the section is short). */
async function metrics(page, cardSel, scrollerSel) {
  return page.evaluate(
    ([card, scroller]) => {
      const el = document.querySelector(card)
      if (!el) return null
      const box = el.querySelector(scroller)
      const area = el.closest('div[class*="flex-1"][class*="overflow-y-auto"]')
      return {
        card: Math.round(el.getBoundingClientRect().height),
        client: box ? box.clientHeight : null,
        scroll: box ? box.scrollHeight : null,
        panel: area ? area.clientHeight : null,
        win: window.innerHeight,
      }
    },
    [cardSel, scrollerSel],
  )
}

/** Whether the bottom overflow cue is currently rendered, plus enough about how
 *  it resolved to catch a cue that is present but invisible.
 *
 *  It is gated on measurement, so its presence is itself an assertable claim:
 *  shown only while something is genuinely below the fold. The colours matter
 *  separately — these gradients are built on `withAlpha()` theme colours, and a
 *  stop that resolves to a fully transparent or mismatched colour renders as
 *  nothing, or as a grey band that reads as a rendering bug rather than a hint. */
async function fadeProbe(page, testid, surfaceSel) {
  return page.evaluate(
    ([id, surface]) => {
      const el = document.querySelector(`[data-testid="${id}"]`)
      if (!el) return { present: false }
      const box = document.querySelector(surface)
      return {
        present: true,
        h: Math.round(el.getBoundingClientRect().height),
        gradient: getComputedStyle(el).backgroundImage,
        surfaceBg: box ? getComputedStyle(box).backgroundColor : null,
      }
    },
    [testid, surfaceSel],
  )
}

/** Chrome reports a `withAlpha()` theme colour as `color(srgb 0.11 0.1 0.13)` but
 *  a resolved gradient stop as `rgb(28, 25, 34)`, so the two have to be brought
 *  into the same units before they can be compared — matching on the strings
 *  compares spelling, not colour. Returns 0-255 triples. */
function rgbTriples(css) {
  const out = []
  for (const m of css.matchAll(/(?:color\(srgb|rgba?\()\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/g)) {
    const n = [m[1], m[2], m[3]].map(Number)
    // Fractional components are srgb 0-1; integers are already 0-255.
    out.push(n.map(v => (n.every(c => c <= 1) ? Math.round(v * 255) : Math.round(v))))
  }
  return out
}

/** The opaque end of the gradient has to be the surface the content sits on, or
 *  the "fade" is a coloured band over it rather than content dissolving away. */
function fadeDissolvesInto(probe) {
  if (!probe.present || !probe.gradient || !probe.surfaceBg) return false
  const [surface] = rgbTriples(probe.surfaceBg)
  if (!surface) return false
  return rgbTriples(probe.gradient).some(stop =>
    stop.every((c, i) => Math.abs(c - surface[i]) <= 1),
  )
}

const failures = []
function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures.push(label)
}

async function run(page, base, theme, summary, kind) {
  await stubDashboardApi(page, {
    slots,
    theme,
    localStorageEntries: PANEL_SEEDS,
    extra: (path, route) => {
      if (!path.includes('/summary')) return false
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(summary),
      })
      return true
    },
  })
  await openPanel(page, base)
  await shoot(page, `${kind}-01-collapsed-${theme}`)

  // Expand the block. The label carries the withheld count, so match loosely.
  await page.getByRole('button', { name: /\+\d+ more/i }).click()
  await page.waitForTimeout(400)
  await shoot(page, `${kind}-02-open-items-expanded-${theme}`)

  const m = await metrics(page, ITEMS_CARD, 'div[class*="overflow-y-auto"]')
  if (kind === 'tall') {
    check('tall: open-items card is capped once expanded', m !== null)
    if (m) {
      // Two-sided deliberately. `card <= cap` alone is satisfied by a card that
      // collapsed to its heading, which is how a layout regression reads as a
      // pass: taking the scroller out of flow leaves nothing to measure, the card
      // shrinks to 86px, and every one-sided ceiling check still says ok.
      check(
        `tall: the CARD sits AT the 33vh cap (${CAP_PX}px), not merely under it`,
        m.card <= CAP_PX + 1 && m.card >= CAP_PX - 2,
        `card=${m.card}`,
      )
      check(
        'tall: the list inside it has real height and scrolls',
        m.client > 0 && m.scroll > m.client,
        `client=${m.client} scroll=${m.scroll}`,
      )
      // The cap is stated in `vh`, which measures the WINDOW, while the card
      // competes for the PANEL's scroll area — shorter by the panel header, the
      // notes footer and the freshness footer. Both shares are printed because
      // the difference between them is what makes a correct cap look wrong.
      if (m.panel) {
        console.log(
          `note  card ${m.card}px = ${Math.round((m.card / m.win) * 100)}% of the ${m.win}px ` +
            `window, ${Math.round((m.card / m.panel) * 100)}% of the ${m.panel}px panel area; ` +
            `list inside it ${m.client}px`,
        )
      }
    }
    const fade = await fadeProbe(page, 'summary-open-items-fade', ITEMS_CARD)
    check('tall: the bottom fade marks the list as continuing', fade.present, `h=${fade.h}px`)
    check(
      'tall: the fade dissolves into the card, rather than banding over it',
      fadeDissolvesInto(fade),
      `surface=${fade.surfaceBg} gradient=${fade.gradient}`,
    )
    // Scroll the INNER list to its end, then prove two things at once: the
    // toggle is still on screen because it lives outside the scroller, and the
    // fade has cleared. A fade that persists at the end is a smaller version of
    // the failure it exists to fix — a cue for content that is not there.
    await page.evaluate(sel => {
      const box = document.querySelector(sel)?.querySelector('div[class*="overflow-y-auto"]')
      if (box) box.scrollTop = box.scrollHeight
    }, ITEMS_CARD)
    await page.waitForTimeout(300)
    const lessVisible = await page.getByRole('button', { name: /Show less/i }).isVisible()
    check('tall: "Show less" survives scrolling the list to its end', lessVisible)
    check(
      'tall: the fade clears once the list is scrolled to its end',
      !(await fadeProbe(page, 'summary-open-items-fade', ITEMS_CARD)).present,
    )
    await shoot(page, `${kind}-03-open-items-scrolled-${theme}`)
  } else {
    // Rule 2: a short list keeps its natural height. The cap class is present
    // whenever the block is expanded, so the property to assert is not its
    // ABSENCE but that it does not bind — the card sits below the ceiling and
    // nothing inside it scrolls.
    check('short: the expanded card exists', m !== null)
    if (m) {
      check(
        `short: the card sits BELOW the cap, at natural height (< ${CAP_PX}px)`,
        m.card < CAP_PX,
        `card=${m.card}`,
      )
      check(
        'short: the list inside it does not scroll',
        m.scroll <= m.client + 1,
        `client=${m.client} scroll=${m.scroll}`,
      )
    }
    // The other half of the cue's contract. A fade applied whenever the block is
    // expanded would pass every tall check above and be wrong exactly here,
    // dimming the last row of a complete list to imply more below it.
    check(
      'short: no fade over a list that fits',
      !(await fadeProbe(page, 'summary-open-items-fade', ITEMS_CARD)).present,
    )
  }

  // The notes footer, which carries the same cap for the same reason.
  await page.getByRole('button', { name: /How this project works/i }).click()
  await page.waitForTimeout(400)
  await shoot(page, `${kind}-04-notes-expanded-${theme}`)
  // The scroller is the named region wrapping the list, not the `<ul>` — the list
  // keeps its implicit role, so measuring it would report natural content height
  // and read as a cap that is not binding.
  const n = await metrics(page, NOTES_CARD, 'div[role="region"]')
  if (kind === 'tall') {
    check('tall: notes footer is capped once expanded', n !== null)
    if (n) {
      check(
        `tall: the notes FOOTER sits AT the 33vh cap (${CAP_PX}px)`,
        n.card <= CAP_PX + 1 && n.card >= CAP_PX - 2,
        `card=${n.card}`,
      )
      check(
        'tall: the notes list has real height and scrolls',
        n.client > 0 && n.scroll > n.client,
        `client=${n.client} scroll=${n.scroll}`,
      )
      check(
        'tall: the notes header is not squeezed out by the list',
        n.card - n.client >= 20,
        `card=${n.card} list=${n.client}`,
      )
      const nFade = await fadeProbe(page, 'summary-notes-fade', NOTES_CARD)
      check('tall: the notes list carries the bottom fade too', nFade.present, `h=${nFade.h}px`)
      check(
        'tall: the notes fade dissolves into the footer',
        fadeDissolvesInto(nFade),
        `surface=${nFade.surfaceBg} gradient=${nFade.gradient}`,
      )

      // Bounding this list made it a scroll region whose items are plain text, so
      // without a tab stop everything past the fold is pointer-only.
      //
      // What this can and cannot prove: Chromium puts a scroll container with no
      // focusable children into the tab order BY ITSELF (keyboard-focusable
      // scrollers), so Tab reaches this list here even with `tabIndex` removed —
      // measured, not assumed. Safari and Firefox do not, which is why the
      // attribute stays; the jsdom test pins it, since jsdom implements no such
      // behaviour. So what is asserted here is the part Chromium does NOT supply:
      // the region carries an accessible NAME, and focus is VISIBLE once it lands.
      await page.getByRole('button', { name: /How this project works/i }).focus()
      await page.keyboard.press('Tab')
      await page.waitForTimeout(150)
      const landed = await page.evaluate(() => {
        const el = document.activeElement
        if (!el) return { tag: 'none', role: '', name: '', ringWidth: '', listRole: '' }
        const cs = getComputedStyle(el)
        // The list must remain a list: an explicit role replaces an element's
        // implicit one, so putting `role="region"` on the `<ul>` would cost its
        // items their `listitem` exposure.
        const ul = el.querySelector('ul')
        return {
          tag: el.tagName,
          role: el.getAttribute('role') || '',
          name: el.getAttribute('aria-label') || '',
          // The ring is drawn as a box-shadow by Tailwind's ring utilities; an
          // empty/none value means the reader cannot see where focus went.
          ringWidth: cs.boxShadow === 'none' ? '' : cs.boxShadow,
          listRole: ul ? ul.getAttribute('role') || 'implicit-list' : 'no list',
        }
      })
      check(
        'tall: focus lands on the notes region and it is NAMED',
        landed.tag === 'DIV' && landed.role === 'region' && landed.name.length > 0,
        `active=${landed.tag} role=${landed.role || '-'} name="${landed.name}"`,
      )
      check(
        'tall: the notes list inside it is still exposed as a list',
        landed.listRole === 'implicit-list',
        `list=${landed.listRole}`,
      )
      check(
        'tall: the focused region shows a visible focus indicator',
        landed.ringWidth !== '',
        `boxShadow=${landed.ringWidth || 'none'}`,
      )
      await shoot(page, `${kind}-05-notes-focused-${theme}`)
    }
  } else {
    check(
      'short: the notes footer is NOT capped (natural height)',
      n === null || n.scroll <= n.client + 1,
      n ? `card=${n.card} client=${n.client} scroll=${n.scroll}` : 'no capped footer',
    )
    check(
      'short: no fade over a notes list that fits',
      !(await fadeProbe(page, 'summary-notes-fade', NOTES_CARD)).present,
    )
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })

  for (const [kind, summary] of [
    ['tall', tallSummary()],
    ['short', shortSummary()],
  ]) {
    for (const theme of ['dark', 'light']) {
      const page = await context.newPage()
      logPageProblems(page)
      await run(page, base, theme, summary, kind)
      await page.close()
    }
  }

  await browser.close()
  srv.close()

  if (failures.length) {
    console.error(`\n${failures.length} geometry assertion(s) failed:`)
    for (const f of failures) console.error(`  - ${f}`)
    process.exit(1)
  }
  console.log('\nall geometry assertions passed')
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
