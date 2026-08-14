/**
 * Screenshot harness for the "Before I Forget" topbar scratchpad.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and
 * answers every /api/** call from fixtures via Playwright route interception —
 * gateway-free, no kiro-cli, no token.
 *
 * What it has to prove, and why one frame cannot:
 *   1. The trigger is a topbar icon that does not disturb the existing controls.
 *   2. The panel is a real surface with a header, a textarea and a footer.
 *   3. Content persists across a reload, and the dot on the CLOSED trigger is
 *      the only thing that says so — it cannot appear in an open-panel frame.
 *   4. The panel is themed, not hardcoded, so it is captured on both chromes.
 *
 * The frames are ASSERTED, not just taken. A capture harness that silently shot
 * a crashed shell or an empty crop would still exit 0 and still produce five
 * PNGs, so each shot checks the DOM claim the frame is evidence for and the
 * script exits non-zero if one does not hold. The closed-state crop is derived
 * from the trigger's real bounding box rather than hardcoded, because a topbar
 * that gains a control shifts it and a fixed crop would quietly stop containing
 * the thing being photographed.
 *
 * `deviceScaleFactor` is 1, not 2: at this viewport a 2x capture is 2800px wide,
 * and an image over 2000px on either edge is rejected by the model provider,
 * which wedges the conversation carrying it.
 *
 * Frames:
 *   closed-empty.png      topbar trigger, scratchpad empty — no dot
 *   closed-with-note.png  the same trigger carrying its content dot
 *   open-empty.png        panel open on the placeholder
 *   open-typed.png        panel open with content, char count and clear control
 *   open-dark.png         the same open panel on dark chrome
 *
 * Output lands in the repo-root `temp-screenshots/<feature>/`, the ephemeral
 * never-packaged dir that holds PR review evidence (see its README): it is
 * outside every path that ships in the wheel, sdist or desktop app, and it is
 * one of the trees `ux-review.yml` gates on. Running the command below therefore
 * reproduces exactly the frames committed alongside this PR.
 *
 * Usage: node scripts/capture-before-i-forget.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/before-i-forget-3619'
mkdirSync(OUT, { recursive: true })

const STORAGE_KEY = 'kirocrew:before-i-forget'

/** Resolve the labels from the catalog rather than hardcoding the English, so a
 *  rename fails the harness loudly instead of silently capturing nothing. */
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const TRIGGER_LABEL = manual.app.before_i_forget
const CLEAR_LABEL = manual.app.before_i_forget_clear
if (!TRIGGER_LABEL || !CLEAR_LABEL) throw new Error('app.before_i_forget* missing from en.manual.json — renamed?')

/** One settled slot: the shell renders the top bar against real slot state
 *  rather than the empty-list path, which is not what a user ever sees. */
const SLOT = 'before-i-forget'
const slots = [{
  key: SLOT,
  title: 'Reviewing the scratchpad',
  running: false,
  last_message: 'Looking at the topbar control.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '/home/user/workspace/notes',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const SAMPLE = [
  'ask about the retry budget in the sync path',
  'PR 3619 — screenshot gate needs both themes',
  'follow up: does the debounce cancel on clear?',
].join('\n')

let failures = 0
const check = (ok, msg) => {
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${msg}`)
  if (!ok) failures++
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/**
 * @param {object} o
 * @param {'light'|'dark'} o.mode
 * @param {string} o.file
 * @param {string} [o.seed]   content preloaded into localStorage
 * @param {boolean} [o.open]  open the panel before shooting
 * @param {string} [o.type]   text typed into the open panel
 */
async function shoot({ mode, file, seed = '', open = false, type }) {
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: mode,
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: mode,
    slots,
    extra: async (path, route) => {
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] })
        return true
      }
      return false
    },
  })
  await page.addInitScript(
    ([m, key, content, slot]) => {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-theme-mode', m)
      localStorage.setItem('mc-active-slot', slot)
      if (content) localStorage.setItem(key, content)
    },
    [mode, STORAGE_KEY, seed, SLOT],
  )

  await page.goto(base, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2200)

  console.log(`${file}  (${mode}, ${open ? 'open' : 'closed'})`)

  const trigger = page.getByRole('button', { name: TRIGGER_LABEL })
  await trigger.waitFor({ state: 'visible', timeout: 10_000 })

  if (!open) {
    // The dot is the whole point of the closed frames: it is the only signal
    // that the scratchpad is holding something.
    const dot = await trigger.locator('span.rounded-full').count()
    check(seed ? dot === 1 : dot === 0, `content dot ${seed ? 'present' : 'absent'} (found ${dot})`)

    // Crop from the trigger's real box so the dot is legible instead of being a
    // few pixels in a 1400px frame — and so a topbar reflow cannot leave the
    // crop pointing at empty chrome.
    const box = await trigger.boundingBox()
    check(!!box && box.width > 0, `trigger has a box: ${JSON.stringify(box)}`)
    const clip = {
      x: Math.max(0, Math.round(box.x - 420)),
      y: 0,
      width: Math.min(500, Math.round(box.x + box.width + 24)),
      height: Math.round(box.y + box.height + 12),
    }
    await page.screenshot({ path: `${OUT}/${file}`, clip })
    await context.close()
    return
  }

  await trigger.click()
  const panel = page.getByRole('dialog', { name: TRIGGER_LABEL })
  await panel.waitFor({ state: 'visible', timeout: 5_000 })
  const textarea = page.getByRole('textbox', { name: TRIGGER_LABEL })

  if (type) {
    await textarea.fill(type)
    // Past the 400ms debounce, so the footer shows "saved" rather than a
    // transient "saving…" that would read as a stuck state in a still frame.
    await page.waitForTimeout(700)
    check((await textarea.inputValue()) === type, 'textarea holds the typed content')
    check(
      (await panel.getByText(`${type.length} chars`).count()) === 1,
      `footer reports ${type.length} chars`,
    )
    check(
      (await panel.getByRole('button', { name: CLEAR_LABEL }).count()) === 1,
      'clear control is offered once there is content',
    )
    const persisted = await page.evaluate(k => localStorage.getItem(k), STORAGE_KEY)
    check(persisted === type, 'content reached localStorage after the debounce')
  } else {
    check((await textarea.inputValue()) === '', 'textarea starts empty')
    check(
      (await panel.getByRole('button', { name: CLEAR_LABEL }).count()) === 0,
      'no clear control while empty',
    )
  }

  const pbox = await panel.boundingBox()
  check(!!pbox && pbox.width > 200 && pbox.height > 200, `panel has a real box: ${JSON.stringify(pbox)}`)

  await page.waitForTimeout(300)
  await page.screenshot({ path: `${OUT}/${file}` })
  await context.close()
}

await shoot({ mode: 'light', file: 'closed-empty.png' })
await shoot({ mode: 'light', file: 'closed-with-note.png', seed: SAMPLE })
await shoot({ mode: 'light', file: 'open-empty.png', open: true })
await shoot({ mode: 'light', file: 'open-typed.png', open: true, type: SAMPLE })
await shoot({ mode: 'dark', file: 'open-dark.png', open: true, type: SAMPLE })

await browser.close()
srv.close()

if (failures) {
  console.error(`\n${failures} assertion(s) failed — the frames do not show what they claim.`)
  process.exit(1)
}
console.log('\ndone — all frames asserted')
