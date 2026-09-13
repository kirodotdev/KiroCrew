/**
 * Screenshots for the clear-context busy refusal on a channel.
 *
 * Drives the isolated capture entry (website/capture/clear-context-busy-refusal.html),
 * which mounts the REAL `Btn` and the REAL `ErrorNotice` the page renders, with the copy
 * resolved through the REAL `clearContextBusyMessage` exported from ChannelPage.
 *
 * The shipped refusal surface is the IN-PAGE banner, so each frame asserts the banner's
 * own text before writing: it must name every refusing role, the cause, and the retry.
 * A frame is not written unless those assertions hold, so an empty or mis-copied banner
 * fails the run rather than being committed as evidence. Dialogs are expected PER SCENE:
 * absent everywhere except the confirm scene, whose copy is read off the dialog event.
 *
 *   01-clear-all-two-roles-busy   partial clear-all, two of three roles mid-turn
 *   02-per-agent-role-busy        the per-agent control, its addressed role refusing
 *   03-clean-acknowledged         contrast: nothing refused, the clear is acknowledged
 *   07-confirm-dialog-copy        the native confirm bodies, read off the dialogs
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-clear-context-busy-refusal.mjs http://127.0.0.1:6841 ../temp-screenshots/clear-context-busy-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/clear-context-busy-refusal'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

/** Scenes, with what the banner must carry for the frame to be honest. The copy reuses
 * the page's own busy word ("working") rather than introducing separate vocabulary, and
 * never makes the role LIST the subject of a verb, so two roles read as well as one. */
const SCENES = [
  {
    file: '01-clear-all-two-roles-busy',
    scope: 'all',
    banner: true,
    mustCarry: ['Researcher', 'Analyst', 'kept for', 'still working', 'Try again when they finish', 'Cleared for Scribe', 'Context partially cleared'],
    // The hand-off would unmount the page and destroy an unsent composer draft. The bold
    // FAILURE lead is wrong here too: this scene did clear a role.
    mustNotCarry: ['Ask the agent', 'Failed to clear context'],
  },
  {
    file: '02-per-agent-role-busy',
    scope: 'agent',
    banner: true,
    // `scope=agent` touches only the addressed member, so a refusal there is `busy && !cleared`
    // -- it takes the total-refusal lead, never the partial one. Both leads are non-failure
    // and both render amber: the clear was withheld, so red stays for a real error.
    mustCarry: ['Researcher', 'kept for', 'still working', 'Try again when they finish', 'Context not cleared'],
    mustNotCarry: ['Ask the agent', 'Context partially cleared', 'Cleared for Scribe', 'Failed to clear context'],
  },
  {
    // A clean clear is acknowledged, so this frame evidences the acknowledgment rather
    // than the blank surface the page stopped rendering.
    file: '03-clean-acknowledged',
    scope: 'clean',
    banner: false,
    mustCarry: ['Context cleared', 'deleted too'],
    mustNotCarry: ['Try again', 'Context not cleared', 'still working'],
  },
  {
    // A TOTAL refusal is a 409 and arrives as a throw. It must render the SAME localized
    // refusal as the partial case -- not the backend's English prose, which would read as
    // doubled phrasing and land untranslated on a localized page.
    file: '04-total-refusal-409',
    scope: 'total',
    banner: true,
    mustCarry: ['Researcher', 'Analyst', 'kept for', 'still working', 'Try again when they finish'],
    mustNotCarry: ['Nothing was cleared', 'turn in flight', 'Ask the agent'],
  },
  {
    // The generic failure path, which this change moved off `alert()`.
    file: '05-generic-failure-inline',
    scope: 'failure',
    banner: true,
    mustCarry: ['channel store unavailable'],
    mustNotCarry: ['Context not cleared', 'Context partially cleared'],
  },
  {
    // The clear-context confirm bodies. They are NATIVE dialogs, so their chrome
    // cannot be screenshotted; the harness instead reads each message off the dialog event
    // and pins the copy, and the frame carries the text that was verified.
    file: '07-confirm-dialog-copy',
    scope: 'confirms',
    banner: false,
    mustCarry: [
      'deletes the channel',
      'Configs are preserved',
      "Researcher's context",
    ],
    mustNotCarry: ['Context not cleared', 'Context partially cleared'],
    expectDialogs: [
      "This clears context for all agents and deletes the channel's messages. Configs are preserved. If any agent is still working, only the agents that are idle are cleared; a working agent's context and the channel's messages are kept.",
    ],
  },
  {
    // The three in-row states, which no other scene mounts: the agents panel is where the
    // user's pointer is, and the page-top banner is outside it.
    file: '08-row-marks',
    scope: 'rowmarks',
    banner: false,
    // The marks are icon-only, so their labels live in `title`/`aria-label` rather than in
    // text; they are asserted from the DOM below instead of through `mustCarry`.
    mustCarry: ['Researcher'],
    mustNotCarry: ['Context not cleared', 'Context partially cleared'],
    noClick: true,
    rowMarks: true,
    viewport: { width: 760, height: 320 },
  },
]

for (const theme of ['dark', 'light']) {
  for (const scene of SCENES) {
    const page = await browser.newPage({ viewport: scene.viewport || { width: 760, height: 250 }, deviceScaleFactor: 2 })

    // The shipped path raises no dialog; assert that rather than assume it.
    const dialogs = []
    page.on('dialog', async d => {
      dialogs.push(d.message())
      await d.dismiss()
    })

    await page.goto(`${BASE}/capture/clear-context-busy-refusal.html?theme=${theme}&scope=${scene.scope}`)
    await page.waitForSelector('[data-capture-root]')
    if (!scene.noClick) await page.click('[data-capture-clear]')
    // `attached`, not visible: the wrapper is empty on the clean scene, where the whole
    // point is that no banner renders.
    await page.waitForSelector('[data-capture-notice]', { state: 'attached' })

    const notice = page.getByTestId('clear-context-error')
    if (scene.banner) await notice.waitFor({ timeout: 5000 }).catch(() => {})
    const shown = (await notice.count()) === 1
    // Read the whole notice surface, not the banner alone: a clean clear renders an
    // acknowledgment and no banner, so banner-only text cannot see it and a frame that
    // showed nothing would assert clean.
    const text = (await page.locator('[data-capture-notice]').textContent()) || ''
    const carried = scene.mustCarry.every(s => text.includes(s))
    // The 409 frame's whole point is that the backend's English did NOT leak through.
    const leaked = (scene.mustNotCarry || []).filter(s => text.includes(s))
    const bannerOk = shown === scene.banner
    // Read from the DOM, not from the props this fixture passed: a mark the component failed
    // to render would otherwise be "evidenced" by a frame that cannot show its absence.
    let marksOk = true
    if (scene.rowMarks) {
      // Clicked HERE so the pending row is genuinely mid-request: its handler never resolves,
      // which is the state the frame has to show and no other scene reaches.
      await page.click('[data-capture-row="pending"] button[title="Clear context"]')
      const pendingBtn = page.locator('[data-capture-row="pending"] button[aria-busy="true"]')
      await pendingBtn.waitFor({ timeout: 5000 }).catch(() => {})
      marksOk =
        (await pendingBtn.count()) === 1 &&
        (await pendingBtn.isDisabled()) &&
        (await page.locator('[data-capture-row="cleared"] [data-testid="agent-clear-done"]').count()) === 1 &&
        (await page.locator('[data-capture-row="kept"] [data-testid="agent-clear-kept"]').count()) === 1 &&
        (await page.locator('[data-capture-row="cleared"] [data-testid="agent-clear-kept"]').count()) === 0
    }
    // Per scene, because the confirm scene's whole purpose is to RAISE dialogs: every
    // expected message must have been seen, and no scene may raise an unexpected one.
    const expected = scene.expectDialogs || []
    const dialogsOk = expected.length
      ? expected.every(m => dialogs.includes(m)) && dialogs.length >= expected.length
      : dialogs.length === 0
    const name = `${scene.file}-${theme}`
    if (
      check(
        name,
        bannerOk && carried && leaked.length === 0 && dialogsOk && marksOk,
        `banner=${shown}/${scene.banner} carried=${carried} leaked=${JSON.stringify(leaked)} marks=${marksOk} dialogs=${JSON.stringify(dialogs)} text=${JSON.stringify(text.slice(0, 140))}`,
      )
    ) {
      await page.screenshot({ path: `${OUT}/${name}.png` })
    }
    await page.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
