/**
 * Screenshot harness for the tool pill's hover bubble.
 *
 * The pill's hover is an InstantTip laying the command out one pipeline stage
 * per line, the joining operator in a gutter (a pipe in the accent, `&&` /
 * `||` / `;` muted), program / flags /
 * strings / redirections coloured apart. A native `title` would be the OS
 * tooltip instead: ~1s late, in the UI font, wrapping a long pipeline mid-word.
 *
 * Runs the REAL built SPA (website/dist) behind the shared transcript harness
 * with every /api/** call answered from fixtures. Hovers each purpose-labelled
 * pill -- shell commands including a classified search, one past the 12-line
 * cap, and a non-shell read --
 * asserts the bubble is the expected kind, that no native title is left, that
 * a cut command ends in the pointer to the opened step, and that the hovered
 * pill wears its open-bubble fill and outline, then shoots dark and light.
 *
 * Usage: node scripts/capture-tool-pill-hover.mjs [outDir]
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

// The harness renders English; read the hint from the catalog so a wording
// change there does not need a matching edit here.
const EN_MANUAL = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.manual.json', import.meta.url), 'utf8'))
const CUT_HINT = `… ${EN_MANUAL.pages.chat.toolCallLine.tip_cut_hint}`

const OUT = process.argv[2] || '../temp-screenshots/tool-pill-hover'
const SLOT = 'chat-tool-pill-hover'
const PROJECT = '/home/user/workspace/kirocrew'
mkdirSync(OUT, { recursive: true })

const CMD = '.venv/bin/python -m pytest -q -p no:cacheprovider --no-cov -o addopts="" test/test_remote_session_local_only_tools.py 2>&1 | grep -E "^(FAILED|E )|passed|failed|Error" | head -20'
const PURPOSE = 'Run the remote-session fence tests'
const CMD2 = 'cd website && npx tsc -p tsconfig.app.json --noEmit && npx vitest run src/test/ToolCallLine.test.tsx'
const PURPOSE2 = 'Typecheck and run the pill tests'
const CLASSIFIED_CMD = 'rg -n "InstantTip" website/src | head -20'
const CLASSIFIED_PURPOSE = 'Find tooltip call sites'

// Past TIP_LINE_CAP (12) stages, so the bubble ends in its muted cut marker.
const CMD3 = [
  'cd website', 'npm ci --no-audit', 'npx tsc -b', 'npx eslint src --max-warnings=0', 'npx vitest run --no-coverage',
  'npm run build', 'cd ..', 'python3 scripts/check_black_formatting.py', 'python3 scripts/check_subprocess_encoding.py',
  'isort --check src/kiro_crew test', 'flake8 src/kiro_crew test', 'mypy src/kiro_crew',
  'FEATURE_MAP_BASE_REF=origin/main python3 scripts/check_feature_map.py', 'python3 scripts/local-gate.py',
].join(' && ')
const PURPOSE3 = 'Run every local gate'
// A non-shell row: the pill shows the purpose, the bubble the tool's own prose title.
const READ_TITLE = 'Reading website/src/pages/chat/ToolCallLine.tsx (lines 860-1050)'
const PURPOSE4 = 'Read the pill host'

const t0 = Date.now() / 1000 - 600
const slots = [{
  key: SLOT, title: 'Fence remote-only tools', running: false, last_message: 'Tests pass.', messages: 8,
  agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT, modified: Math.floor(Date.now() / 1000),
  source_links: [], source_links_total: 0,
}]
const shellRow = (id, ts, purpose, command) => ({
  role: 'tool', ts, content: `🔧 ${purpose}`,
  meta: { tool_call_id: id, kind: 'execute', purpose, input: JSON.stringify({ command, __tool_use_purpose: purpose }), output: '12 passed in 3.41s' },
})
const readRow = (id, ts, purpose, title) => ({
  role: 'tool', ts, content: `🔧 ${title}`,
  meta: {
    tool_call_id: id, kind: 'read', purpose,
    input: JSON.stringify({ path: `${PROJECT}/website/src/pages/chat/ToolCallLine.tsx`, __tool_use_purpose: purpose }),
    output: 'export default function ToolCallLine…',
  },
})
const detail = {
  running: false, has_more: false, total: 8, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Run the new fence tests.' },
    { role: 'assistant', ts: t0 + 5, content: 'Running the fence tests, then the frontend checks.' },
    readRow('tc_r', t0 + 7, PURPOSE4, READ_TITLE),
    shellRow('tc_a', t0 + 9, PURPOSE, CMD),
    shellRow('tc_b', t0 + 30, PURPOSE2, CMD2),
    shellRow('tc_c', t0 + 40, PURPOSE3, CMD3),
    shellRow('tc_s', t0 + 45, CLASSIFIED_PURPOSE, CLASSIFIED_CMD),
    { role: 'assistant', ts: t0 + 50, content: 'Tests pass.' },
  ],
}

const h = await openTranscriptHarness({ slot: SLOT, slots, detail, project: PROJECT })

/** `kind`: 'shell' (the formatted command tip), 'capped' (the same, ending in
 *  the cut marker) or 'prose' (a non-shell row's plain title). */
async function shoot(theme, purpose, name, kind = 'shell') {
  await h.load(theme, { selector: 'textarea', settle: 1200 })
  const group = h.page.getByText(/Worked through/, { exact: false }).first()
  if (await group.count()) await group.click()
  const label = h.page.getByText(purpose, { exact: true }).first()
  await label.waitFor({ timeout: 10000 })
  const pill = label.locator('xpath=ancestor::button[1]')
  if (await pill.getAttribute('title')) throw new Error('native title still on the pill')
  if (await pill.getAttribute('data-tip-open')) throw new Error('pill tinted before its bubble opened')
  await pill.hover()
  const tip = h.page.getByRole('tooltip')
  await tip.waitFor({ timeout: 3000 })
  const commandTips = await tip.locator('[data-testid="tool-command-tip"]').count()
  if (kind === 'prose') {
    if (commandTips !== 0) throw new Error('prose title rendered as a command tip')
    if (!(await tip.textContent())?.includes(READ_TITLE)) throw new Error('bubble does not carry the tool title')
  } else if (commandTips !== 1) throw new Error('bubble is not the command tip')
  if (kind === 'capped') {
    const cut = tip.locator('[aria-hidden="true"] [data-testid="tool-command-tip-cut"]')
    if ((await cut.count()) !== 1) throw new Error('capped command shows no cut marker')
    if ((await cut.textContent()) !== CUT_HINT) throw new Error('cut marker does not point at the opened step')
  }
  if (name === 'classified') {
    const lines = await tip.locator('[aria-hidden="true"] > .contents').allTextContents()
    if (lines.length !== 2 || lines[0] !== '$rg -n "InstantTip" website/src' || lines[1] !== '|head -20') {
      throw new Error('classified search is not a two-stage shell grid')
    }
  }
  if ((await pill.getAttribute('data-tip-open')) !== 'true') throw new Error('hovered pill is not tinted while its bubble is open')
  if (!/\boutline-muted\b/.test((await pill.getAttribute('class')) ?? '')) throw new Error('hovered pill wears no open-bubble outline')
  await h.page.waitForTimeout(250)
  const a = await pill.boundingBox()
  const b = await tip.boundingBox()
  const x = Math.max(0, Math.min(a.x, b.x) - 40)
  const y = Math.max(0, Math.min(a.y, b.y) - 40)
  await h.page.screenshot({
    path: join(OUT, `${name}-${theme}.png`),
    clip: { x, y, width: Math.max(a.x + a.width, b.x + b.width) - x + 40, height: Math.max(a.y + a.height, b.y + b.height) - y + 40 },
  })
  console.log('shot', name, theme)
}

for (const theme of ['dark', 'light']) {
  await shoot(theme, PURPOSE, 'pipeline')
  await shoot(theme, PURPOSE2, 'chain')
  await shoot(theme, CLASSIFIED_PURPOSE, 'classified')
  await shoot(theme, PURPOSE3, 'capped', 'capped')
  await shoot(theme, PURPOSE4, 'prose', 'prose')
}
console.log('DONE', OUT)
await h.close()
