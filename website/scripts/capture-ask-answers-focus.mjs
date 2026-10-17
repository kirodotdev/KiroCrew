/**
 * Screenshot harness for the ask_question answers chip's keyboard focus cue.
 *
 * The chip under a blocking `ask_question` tool row ("You answered N
 * questions") wears the shared `focus-ring-accent` utility, so a keyboard user
 * sees the house cue on it: a thin `--text-strong` hairline plus the accent
 * band, the same ring every other accent control draws.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness
 * with every /api/** call answered from fixtures. Seeds one answered
 * ask_question row with two question/answer pairs, asserts the chip carries
 * the shared utility and not a bespoke ring, moves focus onto it with the
 * keyboard so `:focus-visible` applies, then shoots a tight clip in dark and
 * light.
 *
 * Usage: node scripts/capture-ask-answers-focus.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/ask-answers-focus'
const SLOT = 'chat-ask-answers-focus'
const PROJECT = '/home/user/workspace/kirocrew'
mkdirSync(OUT, { recursive: true })

// Mirrors ASK_ANSWERED_HEADER / ASK_PAIR_SEPARATOR in src/utils/askQuestionTool.ts.
const ANSWERED = [
  'User has answered your questions:',
  `${JSON.stringify('Which branch should the fix target?')} -> ${JSON.stringify('main')}`,
  `${JSON.stringify('Open the PR as a draft?')} -> ${JSON.stringify('No, ready for review')}`,
].join('\n')
const PURPOSE = 'Ask where the fix should land'

const t0 = Date.now() / 1000 - 600
const slots = [{
  key: SLOT, title: 'Fix the answers chip focus ring', running: false, last_message: 'Targeting main.', messages: 4,
  agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, modified: Math.floor(Date.now() / 1000),
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Fix the answers chip focus ring.' },
    { role: 'assistant', ts: t0 + 5, content: 'Two quick questions before I start.' },
    {
      role: 'tool', ts: t0 + 7, content: '🔧 ask_question',
      meta: {
        tool_call_id: 'tc_ask', kind: 'other', purpose: PURPOSE, tool_name: 'ask_question', mcp_server: 'kirocrew-core',
        input: JSON.stringify({ questions: [{ question: 'Which branch should the fix target?' }], __tool_use_purpose: PURPOSE }),
        output: ANSWERED,
      },
    },
    { role: 'assistant', ts: t0 + 60, content: 'Targeting main.' },
  ],
}

const h = await openTranscriptHarness({ slot: SLOT, slots, detail, project: PROJECT })

async function shoot(theme) {
  await h.load(theme, { selector: 'textarea', settle: 1200 })
  const group = h.page.getByText(/Worked through/, { exact: false }).first()
  if (await group.count()) await group.click()
  const chip = h.page.locator('[data-testid="ask-answers-chip"]').first()
  await chip.waitFor({ state: 'visible', timeout: 10000 })
  const cls = (await chip.getAttribute('class')) ?? ''
  if (!/(^|\s)focus-ring-accent(\s|$)/.test(cls)) throw new Error('chip does not wear focus-ring-accent')
  if (cls.includes('focus-visible:ring-accent/50')) throw new Error('chip still wears the bespoke ring')
  if (!/2 questions/.test((await chip.textContent()) ?? '')) throw new Error('chip does not count two answers')

  // Park focus on the chip, then step off and back with the keyboard, so the
  // final focus move is a Tab and `:focus-visible` matches as it does for a user.
  await chip.evaluate(el => el.focus())
  await h.page.keyboard.press('Shift+Tab')
  await h.page.keyboard.press('Tab')
  const state = await chip.evaluate(el => ({ active: document.activeElement === el, visible: el.matches(':focus-visible') }))
  if (!state.active) throw new Error('keyboard focus did not land on the chip')
  if (!state.visible) throw new Error('chip is focused but :focus-visible does not match')
  if ((await chip.getAttribute('aria-expanded')) !== 'false') throw new Error('chip opened while being focused')

  await h.page.waitForTimeout(250)
  const box = await chip.boundingBox()
  const pad = 24
  const x = Math.max(0, box.x - pad)
  const y = Math.max(0, box.y - pad)
  await h.page.screenshot({
    path: join(OUT, `ask-answers-focus-${theme}.png`),
    clip: { x, y, width: box.width + pad * 2, height: box.height + pad * 2 },
  })
  console.log('shot', theme)
}

for (const theme of ['dark', 'light']) await shoot(theme)
console.log('DONE', OUT)
await h.close()
