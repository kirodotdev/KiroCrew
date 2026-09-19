/**
 * Screenshots for the Schedule page's `Last Error` notice on a MESSAGE job.
 *
 * Drives capture/schedule-last-error.html, which mounts the REAL SchedulePage
 * with only the HTTP responses stubbed. The notice is not seeded: the page
 * decides to render it from `job.last_error` alone, and the dialog is opened by
 * clicking the real row, so a frame cannot document a state the shipped
 * condition would not produce.
 *
 * Every frame asserts its own content before writing -- that the notice exists,
 * that it carries the shipped reason text, and (for the member-shadow frame) that
 * it does NOT say "not found", which for that refusal would state the opposite of
 * the fact. A frame that rendered the wrong copy fails here instead of shipping
 * as evidence.
 *
 *   09-last-error-member-shadowed  a member-bound job refused because the bound
 *                                  directory declares its agent: names the
 *                                  shadowed Crew Member and both remedies
 *   10-last-error-agent-missing    the ordinary unresolved-agent skip, naming the
 *                                  directory to add the agent file to
 *
 * Numbered from 09 so the pair appends to the eight JobForm frames already in the
 * PR body rather than renumbering them.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort   # in another shell
 *   node scripts/capture-schedule-last-error.mjs http://127.0.0.1:6823 <outdir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/schedule-last-error'
mkdirSync(OUT, { recursive: true })

const JOB_NAME = 'Nightly release check'
/** Fragments of the shipped strings each frame must actually carry. */
const SHADOW_FRAGMENTS = [
  'would shadow',
  'Crew Member',
  "Rename the project's agent or unbind the member",
]
const MISSING_FRAGMENT = 'not found in project directory'

const browser = await chromium.launch()
const failures = []

async function scene(name, file, { assert, width = 1280, height = 1000 }) {
  const page = await browser.newPage({ viewport: { width, height } })
  page.on('pageerror', (e) => failures.push(`${file}: page error ${e.message}`))
  // The theme comes from localStorage (`mc-theme`), not a query param, so it has
  // to be seeded before the app's first render or the frames come out light while
  // the PR's other eight are dark.
  await page.addInitScript(() => localStorage.setItem('mc-theme', 'dark'))
  await page.goto(`${BASE}/capture/schedule-last-error.html?scene=${name}&theme=dark`)

  // Open the job's detail dialog through the real row, not by URL.
  await page.getByText(JOB_NAME).first().click()
  const notice = page.getByTestId('schedule-job-last-error')
  await notice.waitFor({ state: 'visible', timeout: 10_000 })
  // The notice sits at the FOOT of a long form inside a scrolling DialogBody, so
  // it starts below the fold. Without this, both frames were byte-identical
  // screenshots of the form's top with the notice nowhere in them -- an image
  // that passes every text assertion and shows none of the thing it documents.
  await notice.scrollIntoViewIfNeeded()
  // The dialog animates, and measuring mid-transition read the notice as 17px
  // tall -- one clipped line -- while its text was already complete.
  await page.waitForTimeout(700)
  const text = (await notice.innerText()).replace(/\s+/g, ' ')

  const dialog = page.getByRole('dialog').first()
  try {
    assert(text)
    // Asserting the TEXT is not enough to make a frame citable: an early run
    // passed every content check and wrote ~18px slivers whose words could not be
    // read. The floor sits between the two cases it must tell apart -- a
    // COLLAPSED notice clipped to one 17-18px line, and a legitimately SHORT one
    // (title plus a single message line, ~38px).
    const box = await notice.boundingBox()
    const dialogBox = await dialog.boundingBox()
    if (!box || !dialogBox) throw new Error('notice or dialog has no rendered box')
    if (box.height < 30) {
      throw new Error(`notice rendered ${Math.round(box.height)}px tall — collapsed, unreadable`)
    }
    // ... and being on screen is not the same as being INSIDE the frame that gets
    // written. The screenshot is of the dialog, so the notice has to lie within
    // the dialog's own rect for the image to contain it.
    const withinFrame =
      box.y >= dialogBox.y - 1 && box.y + box.height <= dialogBox.y + dialogBox.height + 1
    if (!withinFrame) {
      throw new Error(
        `notice lies outside the dialog frame being captured `
        + `(notice ${Math.round(box.y)}..${Math.round(box.y + box.height)}, `
        + `dialog ${Math.round(dialogBox.y)}..${Math.round(dialogBox.y + dialogBox.height)})`,
      )
    }
  } catch (e) {
    failures.push(`${file}: ${e.message}\n  notice read: ${text}`)
    await page.close()
    return
  }

  // The DIALOG, not the bare notice: the notice alone is a lone red bar with no
  // indication of which job or surface it belongs to, and UX asked for the job's
  // detail dialog showing it.
  await dialog.screenshot({ path: `${OUT}/${file}.png` })
  console.log(`${file}.png`)
  await page.close()
}

await scene('shadowed', '09-last-error-member-shadowed', {
  assert: (text) => {
    for (const f of SHADOW_FRAGMENTS) {
      if (!text.includes(f)) throw new Error(`missing shipped fragment ${JSON.stringify(f)}`)
    }
    // The defect this frame is evidence against: the unbranched wording said the
    // agent was "not found" in the very directory that declares it.
    if (text.includes('not found')) {
      throw new Error('the member-shadow refusal claimed the agent was not found')
    }
  },
})

await scene('missing', '10-last-error-agent-missing', {
  assert: (text) => {
    if (!text.includes(MISSING_FRAGMENT)) {
      throw new Error(`missing shipped fragment ${JSON.stringify(MISSING_FRAGMENT)}`)
    }
  },
})

await browser.close()

if (failures.length) {
  console.error('\nFAILED — no frame written for:')
  for (const f of failures) console.error('  ' + f)
  process.exit(1)
}
console.log('\nall frames captured and asserted')
