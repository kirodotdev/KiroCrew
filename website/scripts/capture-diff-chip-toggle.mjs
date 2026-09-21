/**
 * Screenshot + video harness for the tool-call diff chip as the card's TOGGLE.
 *
 * The chip that opens a tool-derived diff card stays mounted while the card is
 * open and closes it on the next click. Three frames per theme — folded, open,
 * folded again — each taken after a REAL pointer click (`locator.click()`, not a
 * dispatched event), so a control that renders but cannot be hit fails here the
 * way it failed for the reader: the earlier header chevron sat under Pierre's
 * `z-index: 2` file header and swallowed every click. The counts printed for
 * each frame are the assertions; a frame is not written when one is off.
 *
 * A short recording of the same three-step flow proves the chip is one element
 * throughout (it changes look, never place).
 *
 * The second surface is the prose ```diff fence (FoldableDiffBlock), which keeps
 * DiffBlock's header chevron as its close control. Two frames: the fence open
 * (chevron visible at the header's left edge, filename clear of it), then
 * folded again by a REAL click on that chevron — the click Pierre's header used
 * to swallow. Runs the REAL built SPA (website/dist) behind the shared
 * transcript harness — no gateway, no token.
 *
 * Usage: node scripts/capture-diff-chip-toggle.mjs [outDir]
 */
import { mkdirSync, renameSync } from 'node:fs'

import { openTranscriptHarness } from './lib/transcript-harness.mjs'

const OUT = process.argv[2] || '../temp-screenshots/diff-chip-toggle'
const SLOT = 'chat-diff-chip-toggle'
const PROJECT = '/home/user/workspace/KiroCrew'
const CHIP = '[data-testid="tool-diff-chip"]'

mkdirSync(OUT, { recursive: true })

const CARD_DIFF = [
  '--- /dev/null',
  '+++ b/scripts/find_defs.py',
  '@@ -0,0 +1,6 @@',
  '+import ast',
  '+import sys',
  '+',
  '+def main() -> None:',
  '+    tree = ast.parse(open(sys.argv[1]).read())',
  '+    print(ast.dump(tree, indent=2))',
].join('\n')

/** The assistant's own retelling of the change, as a ```diff fence in prose. */
const PROSE = [
  'Wrote find_defs.py. The one change worth calling out:',
  '',
  '```diff',
  '--- a/scripts/find_defs.py',
  '+++ b/scripts/find_defs.py',
  '@@ -4,2 +4,2 @@',
  '-    print(ast.dump(tree))',
  '+    print(ast.dump(tree, indent=2))',
  '```',
].join('\n')

const t0 = Date.now() / 1000 - 900
const slots = [{
  key: SLOT, title: 'Diff chip toggles its card', running: false, last_message: 'Wrote find_defs.py.',
  messages: 3, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 3, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: t0, content: 'Write a script that dumps the AST of a file.' },
    { role: 'tool', ts: t0 + 14, content: '🔧 fs_write', cls: '', meta: { tool_call_id: 'tc_card', kind: 'edit', input: CARD_DIFF, purpose: 'Write File' } },
    { role: 'assistant', ts: t0 + 40, content: PROSE },
  ],
}

function fail(msg) {
  console.error('ASSERTION FAILED:', msg)
  process.exit(1)
}

async function run(theme, record) {
  const { page, load, close } = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1000, height: 700 },
    ...(record ? { recordVideo: { dir: OUT, size: { width: 1000, height: 700 } } } : {}),
  })
  await load(theme, { selector: CHIP, settle: 900 })
  // Pin English; the harness init script clears localStorage on every navigation,
  // so register after the first load and reload.
  await page.addInitScript(() => localStorage.setItem('mc-lang', 'en'))
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector(CHIP, { timeout: 20000 })
  await page.waitForTimeout(900)

  const chip = page.locator(CHIP).first()
  const state = async () => ({
    card: await page.locator('.diff-block').count(),
    chip: await chip.count(),
    expanded: await chip.getAttribute('aria-expanded'),
  })
  const shot = async (name) => {
    await page.mouse.move(0, 0)
    await page.waitForTimeout(250)
    if (!record) {
      await page.screenshot({ path: `${OUT}/${theme}-${name}.png` })
      console.log('wrote', `${OUT}/${theme}-${name}.png`)
    }
  }

  let s = await state()
  if (s.card !== 0 || s.chip !== 1 || s.expanded !== 'false') fail(`folded start: ${JSON.stringify(s)}`)
  await shot('01-folded')

  await chip.click()
  await page.waitForTimeout(900)
  s = await state()
  if (s.card !== 1 || s.chip !== 1 || s.expanded !== 'true') fail(`after open click: ${JSON.stringify(s)}`)
  await shot('02-open-chip-stays')

  await chip.click()
  await page.waitForTimeout(900)
  s = await state()
  if (s.card !== 0 || s.chip !== 1 || s.expanded !== 'false') fail(`after close click: ${JSON.stringify(s)}`)
  await shot('03-folded-again')

  // Prose fence: chip opens the fence, the header chevron (the fixed control)
  // closes it. Both are real pointer clicks.
  const prose = page.locator('[data-testid="prose-diff-chip"]').first()
  if (await prose.count() !== 1) fail('prose fence chip missing')
  await prose.click()
  await page.waitForTimeout(900)
  const fold = page.locator('.diff-block [data-diff-toggle]')
  if (await page.locator('.diff-block').count() !== 1 || await fold.count() !== 1) fail('prose fence did not open with a fold handle')
  await shot('04-prose-fence-open')
  await fold.click()
  await page.waitForTimeout(900)
  if (await page.locator('.diff-block').count() !== 0 || await prose.count() !== 1) fail('chevron click did not fold the prose fence')
  await shot('05-prose-fence-folded-by-chevron')

  const video = await close()
  if (video) {
    const dest = `${OUT}/${theme}-toggle.webm`
    renameSync(video, dest)
    console.log('wrote', dest)
  }
}

for (const theme of ['light', 'dark']) await run(theme, false)
await run('light', true)
