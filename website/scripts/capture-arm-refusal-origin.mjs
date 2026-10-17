/**
 * Screenshot + assertion for the refused-arm notice row (#19721).
 *
 * A loop a cron job, an app or a sub-agent result tried to start is refused, and
 * the gateway appends a `notice` row (`_surface_arm_refusal`). The row now speaks
 * to the person reading it. Drives the REAL built SPA (website/dist) with every
 * /api/** call answered from fixtures; exits non-zero unless the row renders.
 *
 * Usage: node scripts/capture-arm-refusal-origin.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/19721-arm-refusal-origin'
const SLOT = 'chat-arm-refusal'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })

// Verbatim from session_directive_apply.ARM_REFUSAL_NOTICE_PREFIX + _origin_arm_refusal.
const PREFIX = '⚠ Automation loop NOT armed: '
const tail = origin =>
  `this turn was started by ${origin}, so it cannot start or change a loop. ` +
  'To do it, send the request yourself in this chat.'
const ORIGINS = ['a cron job', 'a sub-agent result', 'an app']

const t0 = Date.now() / 1000 - 900
const slots = [{
  key: SLOT,
  title: 'Nightly PR patrol',
  running: false,
  last_message: 'Patrol cycle done.',
  messages: 5,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]
const detail = {
  running: false,
  has_more: false,
  total: 5,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Check the open PRs and tell me which ones are red.' },
    { role: 'assistant', ts: t0 + 30, content: 'Two PRs are red: #19721 and #19733.' },
    ...ORIGINS.map((origin, i) => ({
      role: 'notice',
      ts: t0 + 300 + i * 60,
      content: PREFIX + tail(origin),
      cls: 'msg msg-info',
      meta: { kind: 'arm_refusal' },
    })),
  ],
}

const { page, load, close } = await openTranscriptHarness({ slot: SLOT, project: PROJECT, slots, detail })
let failed = false
for (const theme of ['dark', 'light']) {
  await load(theme, { settle: 2500 })
  const toggle = page.getByRole('button', { name: /Worked through \d+ steps/ })
  if (await toggle.count()) {
    await toggle.first().evaluate(el => el.click())
    await page.waitForTimeout(500)
  }
  for (const origin of ORIGINS) {
    const row = page.getByText('Automation loop NOT armed: ' + tail(origin), { exact: false }).first()
    const ok = (await row.count()) > 0
    console.log(ok ? 'PASS' : 'FAIL', theme, origin)
    failed ||= !ok
  }
  await page.screenshot({ path: `${OUT}/arm-refusal-notice-${theme}.png` })
  console.log('wrote', `${OUT}/arm-refusal-notice-${theme}.png`)
}
await close()
process.exit(failed ? 1 : 0)
