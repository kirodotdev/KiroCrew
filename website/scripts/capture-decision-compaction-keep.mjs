/**
 * Screenshot harness for the two surfaces `compaction.keep` adds.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures. No gateway, no agent and no Jev call: only the network is stubbed, so
 * `readCompactionKeepRecord`, `CompactionCard`'s three shapes, the transcript
 * virtualizer and the settings card render exactly as in production.
 *
 * Two surfaces, because the feature is a measurement and the consent that allows it:
 *
 *  - the LINE. One line under the compaction notice, saying what an oracle WOULD
 *    have kept. SIX frames per theme, one per shape the record outlives: the
 *    threshold success notice, the recycle notice, the ⚠-led failure that renders
 *    through the shared error surface, the conditional `(+K not scored)` copy, and
 *    the `completed` summary card both COLLAPSED and EXPANDED — that last one is the
 *    only shape with a disclosure, and the line sits outside the fold, so both states
 *    are claims. A line placed inside one branch would be absent from the others, and
 *    only a picture of each shows it is not.
 *
 *    ONE compaction card per frame, and that is the point rather than tidiness. A
 *    transcript carrying all of them photographs the same picture every time,
 *    differing only by scroll offset — so the files' hashes differ while the evidence
 *    does not, and a reader cannot tell which row a frame is about. Each scene serves
 *    its own slot detail (the harness reads `detail` at request time) with its own
 *    record, so the numbers on the line differ frame to frame too.
 *  - the SWITCH. The third consent row on the Decisions (Jev) card, plus the
 *    point row it unlocks. The `withheld` pass serves the SAME fixtures with the
 *    scope revoked and ASSERTS both are absent, because "the row appears" is only
 *    half the claim.
 *
 * The transcript frames are FULL-FRAME rather than clipped to the card. A wrapper-tight
 * crop cuts the notice's own sentence out, and "the line sits under the notice" is the
 * claim — so the frame has to contain both. Every frame also asserts the THEME it is
 * named for: the theme is a stored preference rather than a media query, so a page
 * navigated without a cold load renders in the previous one, and that failure is
 * otherwise silent (two files, one mislabelled, nothing in the run saying so).
 *
 * WHAT THIS HARNESS ASSERTS, and why it asserts anything at all: a capture script
 * that only writes PNGs fails toward a false pass -- a fixture typo, a clipped
 * card or a state that never arrived all still produce a tidy image a PR can cite.
 * So every frame also checks the words that make it that state, and the two
 * absence claims are checked rather than described.
 *
 * Usage: node scripts/capture-decision-compaction-keep.mjs [outDir]
 */
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync } from 'node:fs'

import { json } from './lib/boot-api.mjs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/decision-compaction-keep'
const SLOT = 'chat-compaction-keep'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const LINE = '[data-testid="compaction-keep-line"]'


/** Consent is on, pointed at the address it was given for, and scoped. */
const CONSENT = {
  enabled: true,
  endpoint: 'https://api.typesafe.ai/v1/systemone',
  configured_endpoint: 'https://api.typesafe.ai/v1/systemone',
  permits: true,
  tool_args: true,
  compaction: true,
}

/**
 * The record the gateway stamps, as § 12 of the decisions spec pins it.
 *
 * 14 kept whole + 9 kept without their result + 38 dropped = 61, because the
 * reader refuses a record whose tallies do not sum to the total.
 */
const RECORD = {
  turn_id: 'cmp-7ab419',
  point: 'compaction.keep',
  total_calls: 61,
  pinned_calls: 7,
  kept_both: 14,
  kept_call: 9,
  dropped: 38,
  chars_all: 1482300,
  // Both arms are named `_eligible` because both are upper bounds: the gateway counts
  // conversation rows by ROLE, and the replay applies row quotas and a character budget
  // on top. The reader keys on these exact names, so a fixture using the old ones makes
  // the share render as absent — which is what caught this rename.
  chars_today_eligible: 74110,
  chars_jev_eligible: 607743,
  requests: 3,
  fitting_stage: 'inputs_200',
  calls_truncated: 0,
}

/** A session Jev would keep almost nothing of: a different line, not a re-crop. */
const RECYCLE_RECORD = {
  ...RECORD, turn_id: 'cmp-91d004',
  kept_both: 3, kept_call: 4, dropped: 54, chars_jev_eligible: 190400,
}
/** And one it would keep most of. */
const FAILED_RECORD = {
  ...RECORD, turn_id: 'cmp-2f8c55',
  kept_both: 20, kept_call: 11, dropped: 30, chars_jev_eligible: 1102110,
}
/**
 * A session whose walk overflowed: the line states what it did not score.
 *
 * Its own counts and its own notice, not the plain scene's plus a phrase. A frame whose
 * only difference is one short run of text could not be verified here -- the DOM
 * assertions told the two scenes apart in both directions and four capture strategies
 * still wrote byte-identical files -- so the scene is made its own picture instead. The
 * phrase itself is pinned where a pixel cannot hide it: the harness asserts it in the
 * DOM, and `CompactionKeepLine.test.tsx` asserts it renders and that it precedes the
 * character share.
 */
const TRUNCATED_RECORD = {
  ...RECORD, turn_id: 'cmp-6b0d72', calls_truncated: 140,
  total_calls: 148, pinned_calls: 7, kept_both: 52, kept_call: 26, dropped: 70,
  chars_all: 3180400, chars_today_eligible: 121900, chars_jev_eligible: 1908240,
}
const NOTICE_TRUNCATED = '\u{1F504} Auto-compacted at 93%.'
/** The summary card's own, on a longer session. */
const SUMMARY_RECORD = {
  ...RECORD, turn_id: 'cmp-33fe81',
  total_calls: 92, pinned_calls: 7, kept_both: 31, kept_call: 17, dropped: 44,
  chars_all: 2214870, chars_today_eligible: 98240, chars_jev_eligible: 1241300,
}

/** The backend's own context digest, which is what the `completed` shape folds. */
const SUMMARY = [
  '\u2705 Conversation compacted: **Goal** \u2014 rewrite the config parser to accept TOML.',
  '',
  '**Status** \u2014 the TOML reader is in; the schema check is next.',
  '',
  '**Technical** \u2014 `config/read.py` replaces `parser.py`; `tomllib` on 3.11+.',
  '',
  '**Decisions** \u2014 keep the INI path for one release, behind a deprecation warning.',
].join('\n')

const NOTICE_COMPACTED = '\u{1F504} Auto-compacted at 85%.'
const NOTICE_RECYCLED =
  '\u267B\uFE0F Compaction didn\u2019t succeed at 91%, so the session was restarted '
  + 'instead. The conversation above is still here; the agent no longer remembers it.'
const NOTICE_FAILED =
  '\u26A0 Auto-compact failed at 88% \u2014 will retry after cooldown. '
  + 'You can run `/compact` manually.'

const t0 = Date.now() / 1000 - 1800

/**
 * A compaction row, with the conversation that led to it above.
 *
 * The compaction row is LAST, and that is load-bearing rather than tidy. `TurnBlock`
 * wraps a turn's INTERIM rows in a `CollapsibleSection` rendered at
 * `height: 0; opacity: 0` until expanded, so an extra message after the compaction put
 * the card inside a closed fold: it contributed no pixels, and scenes whose DOM
 * provably differed photographed byte-identically however the frame was clipped or
 * which capture path was used. Last row, no fold -- which is also what a reader sees
 * the moment a compaction lands.
 */
const around = (content, record) => [
  { role: 'user', ts: t0, content: 'Rewrite the config parser to accept TOML.' },
  { role: 'assistant', ts: t0 + 12, content: 'Editing parser.py now \u2014 the TOML reader is in.' },
  { role: 'assistant', ts: t0 + 300, content, meta: { kind: 'compaction', decisions_strip: record } },
]

/**
 * ONE compaction card per scene, which is the whole point of the rewrite.
 *
 * Each frame has to show the row it is named for and nothing else: a transcript
 * carrying all four shapes photographs the same picture every time, differing only by
 * scroll offset — so the files' hashes differ while the evidence does not.
 */
const SCENES = {
  '01-compacted': around(NOTICE_COMPACTED, RECORD),
  '02-recycled': around(NOTICE_RECYCLED, RECYCLE_RECORD),
  '03-failed': around(NOTICE_FAILED, FAILED_RECORD),
  '04-truncated': around(NOTICE_TRUNCATED, TRUNCATED_RECORD),
  '05-summary': around(SUMMARY, SUMMARY_RECORD),
}

/**
 * A phrase from each scene's own NOTICE, checked inside the captured element.
 *
 * This is the "unclipped" claim, and it has to be scoped to the frame: a page-scoped
 * `getByText` passes on a capture that cut the sentence off, because the sentence is
 * still in the document. The frame is the card host, so the question is whether the
 * host's own text carries the notice as well as the keep line.
 */
const NOTICE_IN_FRAME = {
  '01-compacted': 'Auto-compacted at 85%',
  '02-recycled': 'the session was restarted',
  '03-failed': 'Auto-compact failed at 88%',
  '04-truncated': 'Auto-compacted at 93%',
  // The digest's own prefix is stripped by `COMPLETED_RE`; the card's title replaces it.
  '05-summary': 'Context compacted',
}

const slots = [
  {
    key: SLOT,
    title: 'Rewrite the parser',
    running: false,
    last_message: 'Auto-compacted.',
    messages: 3,
    agent: 'kirocrew',
    memory_mode: 'persistent',
    project: PROJECT,
    modified: Math.floor(Date.now() / 1000),
    source_links: [],
    source_links_total: 0,
  },
]

// MUTABLE, and that is the mechanism: the harness's `/api/**` route reads this object
// at request time, so swapping `messages` and cold-loading serves the next scene.
const detail = {
  running: false,
  has_more: false,
  total: 4,
  queue: [],
  project: PROJECT,
  messages: [],
}

const failures = []
const check = (ok, msg) => {
  if (!ok) failures.push(msg)
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${msg}`)
}

/**
 * What each frame must say to BE that frame.
 *
 * Every entry asserts its own session's numbers, not just that a line exists: two frames
 * agreeing on their text are two frames one of which is not evidence.
 */
const EXPECT = {
  '01-compacted': async (page, said, theme, check) => {
    check(
      /would keep/.test(said),
      `01 ${theme}: the line says WOULD, so it cannot read as what the compaction did`,
    )
    check(
      /23/.test(said) && /61/.test(said),
      `01 ${theme}: the kept count is derived from the log's own tallies (${said.trim().slice(0, 80)})`,
    )
    check(
      /up to/.test(said) && /%/.test(said),
      `01 ${theme}: the character share is stated as the BOUND it is`,
    )
    check(
      !/not scored/.test(said),
      `01 ${theme}: no overflow qualifier on a session that did not overflow`,
    )
  },
  '02-recycled': async (page, said, theme, check) => {
    check(/would keep/.test(said), `02 ${theme}: the recycle notice carries the line`)
    check(
      /\b7\b/.test(said),
      `02 ${theme}: its own numbers, not another scene's (${said.trim().slice(0, 80)})`,
    )
    check(
      await page.getByText(/was restarted|no longer remembers/).count() > 0,
      `02 ${theme}: the recycle notice's own sentence is in frame`,
    )
  },
  '03-failed': async (page, said, theme, check) => {
    check(/would keep/.test(said), `03 ${theme}: the failure shape carries it too`)
    check(
      await page.getByTestId('compaction-card-error').count() > 0,
      `03 ${theme}: it renders through the shared error surface`,
    )
    check(/\b31\b/.test(said), `03 ${theme}: its own numbers (${said.trim().slice(0, 80)})`)
  },
  '04-truncated': async (page, said, theme, check) => {
    check(
      /not scored/.test(said) && /140/.test(said),
      `04 ${theme}: a truncated walk states what it did not score (${said.trim().slice(0, 90)})`,
    )
    check(
      said.indexOf('not scored') < said.indexOf('%'),
      `04 ${theme}: the overflow qualifies the COUNT, so it precedes the share`,
    )
    check(
      /78/.test(said) && /148/.test(said),
      `04 ${theme}: its own session's numbers (${said.trim().slice(0, 90)})`,
    )
  },
  '05-summary': async (page, said, theme, check) => {
    check(/48/.test(said) && /92/.test(said), `05 ${theme}: its own numbers`)
  },
}

const shot = []
/** Every frame's hash, so no two can be the same picture. */
const frames = []

/**
 * Run *body* against a harness serving exactly *messages*, then tear it down.
 *
 * A harness PER SCENE, and that is the fix rather than tidiness: mutating one harness's
 * slot detail between scenes leaves the previous scene's frame composited for the next
 * one, so two scenes with different notices, different counts and different records
 * still wrote byte-identical files. Waiting on the DOM cannot help -- the DOM is already
 * correct; it is the paint that lags. A fresh browser per scene has nothing to carry
 * over.
 */
async function withScene(messages, body) {
  const detail = {
    running: false,
    has_more: false,
    total: messages.length,
    queue: [],
    project: PROJECT,
    messages,
  }
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT,
    project: PROJECT,
    slots,
    detail,
  })
  // Registered AFTER the harness's catch-all so they win: Playwright matches route
  // handlers in reverse order.
  await page.route('**/api/decisions/consent', route => json(route, CONSENT))
  await page.route('**/api/dashboard/config', route =>
    json(route, {
      restore_sessions: false,
      restore_window_minutes: 30,
      merge_queued_messages: false,
      widget_density: 'more',
      decisions_enabled: true,
    }),
  )
  // The locale has to be pinned before the load that is photographed: the harness's own
  // init script clears localStorage on every navigation, so the key is written by a
  // later init script and a reload is what makes it stick.
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  try {
    await body(page, load)
  } finally {
    await close()
  }
}

/**
 * Is the card actually ON SCREEN? Asked of the browser, because every DOM assertion in
 * this file passed while the card was inside a closed fold.
 *
 * Two questions a `textContent` check cannot answer: does any ancestor render it at zero
 * height or zero opacity, and does `elementFromPoint` at the card's own centre land
 * inside the card. The first names the fold; the second catches anything painted over it.
 */
const invisibility = page =>
  page.locator(LINE).first().evaluate(el => {
    const dim = []
    let node = el
    while (node) {
      const cs = getComputedStyle(node)
      const rect = node.getBoundingClientRect()
      if (Number(cs.opacity) === 0 || rect.height === 0 || cs.visibility === 'hidden') {
        dim.push(`${node.tagName} h=${Math.round(rect.height)} op=${cs.opacity} vis=${cs.visibility}`)
      }
      node = node.parentElement
    }
    const host = el.closest('[data-testid="compaction-card-host"]')
    const rect = host.getBoundingClientRect()
    const top = document.elementFromPoint(
      Math.round(rect.x + rect.width / 2),
      Math.round(rect.y + rect.height / 2),
    )
    return { dim, hitInside: top ? host.contains(top) : false }
  })

/** The document's own theme, so a frame cannot silently be the previous one. */
const themeOf = page =>
  page.evaluate(() => (document.documentElement.dataset.mode === 'light' ? 'light' : 'dark'))

async function main() {
  // ── the transcript frames: one scene, one harness, one card ──
  for (const [name, messages] of Object.entries(SCENES)) {
    await withScene(messages, async (page, load) => {
      for (const theme of ['light', 'dark']) {
        await load(theme, { selector: LINE })
        await page.waitForTimeout(800)
        check(
          await themeOf(page) === theme,
          `${name} ${theme}: the frame renders in the theme it is named for`,
        )
        // EXACTLY one, so the frame cannot be a picture of several cards with one of
        // them circled in the reader's imagination.
        check(
          await page.locator(LINE).count() === 1,
          `${name} ${theme}: the scene carries exactly one compaction card`,
        )
        const line = page.locator(LINE).first()
        await line.evaluate(el => el.scrollIntoView({ block: 'center' }))
        await page.waitForTimeout(400)
        const said = (await line.textContent()) ?? ''

        /**
         * Photograph the compaction card itself -- the element that wraps the notice AND
         * the keep line (`compaction-card-host`), so one frame carries both halves of
         * the claim "the receipt sits under the notice".
         *
         * Not a viewport clip and not a guessed container. `main` does not contain the
         * virtualised transcript, so capturing it produced pictures of the page AROUND
         * the card: frames then differed only where their notice's HEIGHT differed, and
         * two single-line notices came out byte-identical however much their content
         * differed. The scrolling ancestor is not screenshottable. The card is.
         *
         * The containment check is the part that keeps this honest, and it is what the
         * harness was missing: a capture that does not contain its subject fails here
         * rather than producing a tidy file nobody can read the subject out of.
         */
        const shoot = async (suffix = '') => {
          const host = line.locator(
            'xpath=ancestor::div[@data-testid="compaction-card-host"][1]',
          )
          check(
            await host.count() === 1,
            `${name}${suffix} ${theme}: the card host is in the tree`,
          )
          check(
            await host.evaluate((node, selector) => !!node.querySelector(selector), LINE),
            `${name}${suffix} ${theme}: the captured element contains the keep line`,
          )
          // Both halves in ONE frame, every frame -- including the folded summary,
          // whose header is in frame whether or not its digest is.
          const hostText = (await host.textContent()) ?? ''
          check(
            hostText.includes(NOTICE_IN_FRAME[name]),
            `${name}${suffix} ${theme}: the notice's own words are inside the frame, `
            + `not merely on the page`,
          )
          const seen = await invisibility(page)
          check(
            seen.dim.length === 0,
            `${name}${suffix} ${theme}: nothing renders the card at zero height or opacity`
            + `${seen.dim.length ? ' — ' + seen.dim.join('; ') : ''}`,
          )
          check(
            seen.hitInside,
            `${name}${suffix} ${theme}: the card is what sits at its own centre point`,
          )
          const stem = `${name}${suffix}-${theme}.png`
          const file = `${OUT}/${stem}`
          await host.screenshot({ path: file })
          const bytes = readFileSync(file)
          const digest = createHash('sha256').update(bytes).digest('hex').slice(0, 12)
          const twin = frames.find(f => f.digest === digest)
          // A frame identical to another frame is not evidence of the state it is named
          // for, whatever its assertions said. Found by hand once; a failure now.
          check(
            !twin,
            `${stem}: is its own picture (byte-identical to ${twin ? twin.stem : '-'})`,
          )
          frames.push({ stem, digest, bytes: bytes.length })
          shot.push(stem)
          console.log('wrote', file, `${bytes.length}B sha=${digest}`)
        }

        await EXPECT[name](page, said, theme, check)
        if (name === '05-summary') {
          // The one card shape with a disclosure: the digest folds and the line sits
          // outside the fold, so BOTH states are claims rather than one.
          const toggle = page.getByTestId('compaction-card-toggle').first()
          check(
            await toggle.getAttribute('aria-expanded') === 'false',
            `${name} ${theme}: the summary starts folded`,
          )
          await shoot('-collapsed')
          await toggle.click()
          await page.waitForSelector('[data-testid="compaction-card-body"]', { timeout: 15000 })
          await page.waitForTimeout(500)
          check(
            await toggle.getAttribute('aria-expanded') === 'true',
            `${name} ${theme}: the chevron reports the expanded state`,
          )
          check(
            (await page.getByTestId('compaction-card-body').textContent() ?? '').includes('Decisions'),
            `${name} ${theme}: the expanded body renders the digest as markdown`,
          )
          check(
            /would keep/.test((await page.locator(LINE).first().textContent()) ?? ''),
            `${name} ${theme}: the keep line survives the expansion`,
          )
          await shoot('-expanded')
        } else {
          await shoot()
        }
      }
    })
  }

  // ── the consent card, and the absence claim ──
  await withScene(SCENES['01-compacted'], async (page, load) => {
    for (const theme of ['light', 'dark']) {
      // Cold-load first: the theme is a stored preference applied at boot, not a media
      // query, so a page navigated without one renders in the previous theme.
      await load(theme, { selector: LINE })
      await page.goto(`${new URL(page.url()).origin}/settings/developer`, {
        waitUntil: 'domcontentloaded',
      })
      await page.waitForTimeout(1400)
      const row = page.getByRole('switch', {
        name: 'Also send the conversation and tool-call inputs so Jev can score compaction',
      })
      await row.waitFor({ timeout: 20000 })
      await row.evaluate(el => el.scrollIntoView({ block: 'center' }))
      await page.waitForTimeout(400)
      check(
        await themeOf(page) === theme,
        `consent ${theme}: the frame renders in the theme it is named for`,
      )
      check(
        await row.getAttribute('aria-checked') === 'true',
        `consent ${theme}: the third switch draws the recorded scope`,
      )
      check(
        await page.getByTitle('compaction.keep').count() > 0,
        `consent ${theme}: the point is named while its scope is granted`,
      )
      const card = row.locator('xpath=ancestor::*[contains(@class,"rounded")][1]')
      const file = `${OUT}/07-consent-${theme}.png`
      await card.screenshot({ path: file })
      shot.push(`07-consent-${theme}.png`)
      console.log('wrote', file)
    }
  })

  // The SAME fixtures with the scope revoked: the switch stays, unchecked, and the point
  // row is gone. Asserted rather than described, because "it appears when granted" says
  // nothing about what an ungranted install sees.
  await withScene(SCENES['01-compacted'], async (page, load) => {
    await page.route('**/api/decisions/consent', route =>
      json(route, { ...CONSENT, compaction: false }),
    )
    await load('light', { selector: LINE })
    await page.goto(`${new URL(page.url()).origin}/settings/developer`, {
      waitUntil: 'domcontentloaded',
    })
    await page.waitForTimeout(1400)
    const row = page.getByRole('switch', {
      name: 'Also send the conversation and tool-call inputs so Jev can score compaction',
    })
    await row.waitFor({ timeout: 20000 })
    check(
      await row.getAttribute('aria-checked') === 'false',
      'withheld: the scope draws OFF for a keystone that never recorded it',
    )
    check(
      await page.getByTitle('compaction.keep').count() === 0,
      'withheld: the point is NOT named while its scope is ungranted',
    )
  })

  if (failures.length) {
    console.error(`\n${failures.length} check(s) failed:`)
    for (const line of failures) console.error(`  - ${line}`)
    process.exit(1)
  }
  console.log(`\nwrote ${shot.length} shot(s) to ${OUT}:`)
  for (const f of frames) console.log(`  ${f.stem.padEnd(34)} ${String(f.bytes).padStart(8)}B  ${f.digest}`)
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
