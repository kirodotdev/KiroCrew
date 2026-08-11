/**
 * Screenshot harness for find-bar close focus hand-back (issue #2612).
 *
 * Builds on scripts/lib/transcript-harness.mjs (real built SPA, stubbed
 * network). What this scene must prove, from the live DOM not the pixels:
 *
 *  1. Ctrl/Cmd+F opens the find bar and focuses its input.
 *  2. Escape closes the bar AND focus lands on the composer — before this
 *     fix, `close()` cleared the bar's state and left focus nowhere, so the
 *     user's next keystrokes went to no element until they clicked back in.
 *  3. The bar's own close button hands focus back identically: the fix lives
 *     in the close path, not the Escape handler.
 *
 * Usage: node scripts/capture-findbar-escape-focus.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/findbar-escape-focus'
const SLOT = 'chat-findbarfocus'
const PROJECT = '/Users/diwm/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Find bar focus', running: false,
  last_message: 'deployment finished', messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 900, content: 'Kick off the deployment to staging please.' },
    { role: 'assistant', ts: now - 850, content: 'Starting the staging deployment now — build first, then rollout.' },
    { role: 'user', ts: now - 500, content: 'How is the deployment going?' },
    { role: 'assistant', ts: now - 30, content: 'The deployment finished cleanly: 12 tasks rolled, health checks green.' },
  ],
}

/** aria-label of the element that currently holds focus (null when none). */
const activeLabel = page => page.evaluate(() =>
  document.activeElement && document.activeElement !== document.body
    ? document.activeElement.getAttribute('aria-label') || document.activeElement.tagName
    : null)

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1400, height: 950 },
  })

  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  const shot = async name => {
    await h.page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  // 1. Dark theme: open the bar, Escape, prove the composer holds focus.
  await h.load('dark', { selector: 'textarea[aria-label="Message input"]', settle: 800 })
  await h.page.keyboard.press('Control+f')
  await h.page.waitForTimeout(300)
  await shot('01-findbar-open-dark')
  const openFocus = await activeLabel(h.page)
  assert(`Ctrl+F focuses the find input (active: ${openFocus})`, openFocus !== 'Message input' && openFocus !== null)

  await h.page.keyboard.press('Escape')
  await h.page.waitForTimeout(300)
  await shot('02-escape-composer-focused-dark')
  const escFocus = await activeLabel(h.page)
  assert(`Escape hands focus to the composer (active: ${escFocus})`, escFocus === 'Message input')
  await h.page.keyboard.type('typing lands here immediately')
  const typed = await h.page.inputValue('textarea[aria-label="Message input"]')
  assert('keystrokes after Escape land in the composer', typed.includes('typing lands here'))

  // 2. The pane's own close control routes through the same close path
  //    (the docked find bar renders inside a DetailPanel with
  //    onClose={search.close}).
  await h.load('dark', { selector: 'textarea[aria-label="Message input"]', settle: 800 })
  await h.page.keyboard.press('Control+f')
  await h.page.waitForTimeout(300)
  await h.page.getByRole('button', { name: 'Close panel' }).click()
  await h.page.waitForTimeout(300)
  const btnFocus = await activeLabel(h.page)
  assert(`close button hands focus to the composer (active: ${btnFocus})`, btnFocus === 'Message input')
  await shot('03-close-button-composer-focused-dark')

  // 3. Light theme evidence.
  await h.load('light', { selector: 'textarea[aria-label="Message input"]', settle: 800 })
  await h.page.keyboard.press('Control+f')
  await h.page.waitForTimeout(300)
  await shot('04-findbar-open-light')
  await h.page.keyboard.press('Escape')
  await h.page.waitForTimeout(300)
  const lightFocus = await activeLabel(h.page)
  assert(`light theme: Escape hands focus to the composer (active: ${lightFocus})`, lightFocus === 'Message input')
  await shot('05-escape-composer-focused-light')

  await h.close()
  if (failures) {
    console.error(`${failures} assertion(s) failed`)
    process.exit(1)
  }
  console.log('all assertions passed')
}

main().catch(err => { console.error(err); process.exit(1) })
