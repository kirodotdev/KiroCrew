/**
 * Shared rig for the Prompts draft-guard capture harnesses.
 *
 * Both of them (`capture-prompt-draft-nav-guard.mjs` for the global sidebar,
 * `capture-prompt-draft-back-guard.mjs` for the browser Back button) need the
 * same setup: the REAL built SPA over serveDist with every /api call stubbed, two
 * prompt fixtures so the list pane looks like a real install rather than an empty
 * state, a record of every native dialog the run raised (the confirm cannot appear
 * in a screenshot, so it is ASSERTED instead), and a screenshot helper that parks
 * the scrollers first. Only the exit under test differs.
 *
 * The dialog recorder is the load-bearing part: a run where no dialog fires must
 * FAIL rather than quietly produce plausible frames, which is exactly what the bug
 * looked like.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './stub-dashboard-api.mjs'

/** Two prompts, so the list pane looks like a real install rather than an empty
 *  state (the empty state hides the list column entirely). Module-private: the
 *  harnesses consume them through the stubbed API, never by name. */
const PROMPTS = [
  {
    name: 'release-notes', fullName: 'release-notes',
    description: 'Draft release notes from a milestone',
    path: '~/.kiro/prompts/release-notes.md', package: '', source: 'global',
  },
  {
    name: 'triage', fullName: 'triage',
    description: 'Triage an inbound bug report',
    path: '~/.kiro/prompts/triage.md', package: '', source: 'global',
  },
]

const DETAIL = {
  content: '---\ndescription: Triage an inbound bug report\n---\n\nRead the report and classify it.\n',
  redacted: false,
  lossy: false,
  hash: 'a'.repeat(64),
}

/** The body text both harnesses type, and the confirm copy both expect. */
export const EDITED_BODY = 'Read the report, classify it, and name the one experiment that would settle the diagnosis.'
export const EXPECTED_CONFIRM = 'Discard unsaved changes?'
/** The editor's body field, addressed by its placeholder. */
export const BODY_FIELD = /markdown the agent receives/

/**
 * Boot the rig. Returns the page plus the pieces a scene needs:
 *
 *   dialogs    every native dialog raised so far, with the answer given
 *   answer     set to 'accept' or 'dismiss' BEFORE the click that raises one
 *   shot       screenshot into the run's out dir, scrollers parked
 *   fail       record a failed assertion (sets a non-zero exit code)
 *   pane       the side-panel pane locator (the shell has its own 'Edit' controls)
 *   done       final log line + teardown; call last
 */
export async function startPromptDraftHarness(out) {
  mkdirSync(out, { recursive: true })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  logPageProblems(page)

  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path.startsWith('/api/prompts/')) { await json(route, DETAIL); return true }
      if (path === '/api/prompts') { await json(route, PROMPTS); return true }
      return false
    },
  })

  const dialogs = []
  const state = { answer: 'dismiss' }
  page.on('dialog', async d => {
    dialogs.push({ message: d.message(), answered: state.answer })
    if (state.answer === 'accept') await d.accept()
    else await d.dismiss()
  })

  // Filling the body scrolls it into view, which clips the pane header out of the
  // frame. Park every scroller back at the top so each shot is the whole surface.
  const shot = async name => {
    await page.evaluate(() => {
      document.querySelectorAll('*').forEach(el => { el.scrollTop = 0 })
      window.scrollTo(0, 0)
    })
    await page.screenshot({ path: `${out}/${name}.png` })
  }

  return {
    base,
    page,
    dialogs,
    pane: () => page.getByTestId('side-panel-pane'),
    answer: value => { state.answer = value },
    shot,
    fail: msg => { console.error(`FAIL: ${msg}`); process.exitCode = 1 },
    done: async () => {
      console.log(`final url: ${page.url()}`)
      console.log(`dialogs raised: ${JSON.stringify(dialogs, null, 2)}`)
      console.log(process.exitCode ? 'capture FAILED' : `capture ok -> ${out}`)
      await browser.close()
      srv.close()
    },
  }
}
