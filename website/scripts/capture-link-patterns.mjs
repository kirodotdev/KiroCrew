/**
 * Screenshot harness for operator link patterns (dashboard.link_patterns).
 *
 * Proves, from the live DOM before any pixel is taken:
 *  1. A prose ticket id rewrites to a real anchor at the configured template.
 *  2. An inline-code span whose WHOLE text matches renders as a link chip
 *     (anchor wrapping the code element) instead of the copy-only chip.
 *  3. A match inside a fenced code block is NOT rewritten.
 *  4. The Settings -> Chat row editor renders the configured rules.
 *
 * The rules arrive through the same network shape production uses — a
 * /api/dashboard/config override registered AFTER the harness's catch-all
 * route (Playwright matches newest-first) — not hand-set component state.
 *
 * Usage: node scripts/capture-link-patterns.mjs [outDir]
 */
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const { openTranscriptHarness } = await import('./lib/transcript-harness.mjs')
const { json } = await import('./lib/boot-api.mjs')

const OUT = process.argv[2] || '../temp-screenshots/link-patterns'
const SLOT = 'chat-linkpatterns'
// Derived from this script's own location (scripts/ -> website/ -> repo root),
// never hardcoded: this path RENDERS into the captured screenshot, so a personal
// absolute path both leaks a home directory and misrepresents any other checkout.
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

mkdirSync(OUT, { recursive: true })

const RULES = [
  { pattern: '\\b(?:PROJ|OPS)-\\d+\\b', url: 'https://tracker.example.com/browse/{match}' },
]

const now = Date.now() / 1000
const slots = [{
  key: SLOT, title: 'Link patterns', running: false,
  last_message: 'triage summary', messages: 4, agent: 'kirocrew',
  memory_mode: 'persistent', project: PROJECT, modified: Math.floor(now),
  source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 4, queue: [], project: PROJECT,
  messages: [
    { role: 'user', ts: now - 900, content: 'What is blocking the rollout?' },
    {
      role: 'assistant', ts: now - 850, content: [
        'PROJ-4312 blocks the rollout, and `OPS-771` tracks the fix on the ops side.',
        '',
        'The deploy log names the same id:',
        '```',
        'gate: waiting on PROJ-4312',
        '```',
        'Full context in [the runbook](https://wiki.example.com/runbook).',
      ].join('\n'),
    },
    { role: 'user', ts: now - 500, content: 'Who owns OPS-771?' },
    { role: 'assistant', ts: now - 30, content: 'The ops on-call owns `OPS-771`; PROJ-4312 stays with us.' },
  ],
}

async function main() {
  const h = await openTranscriptHarness({
    slot: SLOT, project: PROJECT, slots, detail,
    viewport: { width: 1400, height: 950 },
  })

  // Newest route wins: serve the config WITH rules over the fixture default.
  await h.page.route('**/api/dashboard/config', route => json(route, {
    restore_sessions: false, restore_window_minutes: 30,
    merge_queued_messages: false, widget_density: 'more',
    link_patterns: RULES,
  }))

  let failures = 0
  const assert = (label, ok) => {
    console.log(`${ok ? 'PASS' : 'FAIL'}: ${label}`)
    if (!ok) failures += 1
  }
  const shot = async name => {
    await h.page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  for (const theme of ['dark', 'light']) {
    await h.load(theme, { selector: 'textarea[data-composer-input]', settle: 900 })

    const links = await h.page.$$eval(
      'a[href^="https://tracker.example.com/browse/"]',
      as => as.map(a => ({ href: a.getAttribute('href'), hasCode: !!a.querySelector('code'), text: a.textContent?.trim() })),
    )
    // Prose PROJ-4312 + chip OPS-771 (msg 2), prose OPS-771 in the USER turn
    // (msg 3 — user messages render through the same pipeline), chip OPS-771 +
    // prose PROJ-4312 (msg 4) = 5 anchors; the fenced PROJ-4312 must add none.
    assert(`${theme}: 5 tracker anchors rendered (got ${links.length})`, links.length === 5)
    assert(`${theme}: prose match links to resolved template`,
      links.some(l => l.href === 'https://tracker.example.com/browse/PROJ-4312' && !l.hasCode))
    assert(`${theme}: whole-match inline code renders as a link CHIP (anchor wrapping <code>)`,
      links.some(l => l.href === 'https://tracker.example.com/browse/OPS-771' && l.hasCode))
    const fenced = await h.page.$$eval('pre a', as => as.length)
    assert(`${theme}: fenced code block contains no injected anchor`, fenced === 0)

    await shot(`transcript-${theme}`)
  }

  // Settings -> Chat: the row editor, clipped to its own field frame.
  await h.page.goto(new URL('/settings', h.base).href, { waitUntil: 'domcontentloaded' })
  const chatTab = await h.page.$('text=Chat')
  if (chatTab) await chatTab.click()
  const field = await h.page.waitForSelector('[data-setting-key="dashboard.link_patterns"]', { timeout: 10_000 }).catch(() => null)
  if (field) {
    await field.scrollIntoViewIfNeeded()
    await h.page.waitForTimeout(400)
    const patternValue = await field.$eval('input', el => el.value).catch(() => '')
    assert('settings: editor shows the configured pattern', patternValue === RULES[0].pattern)
    await field.screenshot({ path: `${OUT}/settings-editor.png` })
    console.log('wrote', `${OUT}/settings-editor.png`)
  } else {
    assert('settings: link-patterns field mounted', false)
  }

  await h.close()
  if (failures > 0) {
    console.error(`${failures} assertion(s) failed`)
    process.exit(1)
  }
}

await main()
