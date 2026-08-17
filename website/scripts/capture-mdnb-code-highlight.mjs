/**
 * Screenshot harness for syntax colouring of fenced code in the Notes app.
 *
 * Three frames, one claim each:
 *   01 dark  - a labelled fence rendered with highlight.js colours
 *   02 light - the same note in a light theme, because the palette carries
 *              light-mode overrides and a fixed dark palette would be unreadable
 *              there; this frame is what proves it follows the theme
 *   03 plain - a fence that names NO language stays plain text, so a log or a
 *              directory tree is never painted with guessed keyword colours
 *
 * The colouring runs in the real Web Worker of the built bundle, so a frame is
 * only taken once `.hljs-*` spans actually exist in the note pane. That is the
 * assertion that separates "colours arrived" from "the worker never replied".
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-code-highlight.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import {
  MDNB_VAULT_ID,
  mdnbApiStub,
  mdnbNoteDoc,
  mdnbNotesList,
  notePaneClip,
} from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-code-highlight'
mkdirSync(OUT, { recursive: true })

const NOTE_PATH = 'runbooks/deploy-pipeline.md'
const NOTE_TITLE = 'Deploy pipeline'
const HL_SPAN = '.hljs-keyword, .hljs-string, .hljs-title, .hljs-built_in'

/** A labelled Python fence and a labelled shell fence, the two commonest in a runbook. */
const NOTE = `# ${NOTE_TITLE}

The rollout waits for the health probe before it shifts traffic:

\`\`\`python
import time
from deploy import health, traffic

def rollout(target: str, wait: int = 30) -> bool:
    """Shift traffic once the new revision answers its probe."""
    for attempt in range(wait):
        if health.probe(target) == "ok":
            traffic.shift(target, weight=100)
            return True
        time.sleep(1)
    raise TimeoutError(f"{target} never became healthy")
\`\`\`

Trigger it from the release host:

\`\`\`bash
export TARGET="api-green"
./deploy.sh --target "$TARGET" --wait 30 && echo "shifted"
\`\`\`
`

/** Same shape, but the fences name no language: pasted output, not code. */
const PLAIN_NOTE = `# ${NOTE_TITLE}

Last night's run, pasted as it came out:

\`\`\`
14:02:11  probe api-green      ok
14:02:11  shift  api-green     weight=100
14:02:12  probe  api-blue      draining
\`\`\`

And the layout it left behind:

\`\`\`
releases/
  2026-08-16/  api-blue
  2026-08-17/  api-green
\`\`\`
`

async function shoot(browser, base, doc, { file, theme, wantColour }) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    theme,
    extra: mdnbApiStub({ notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE), doc }),
  })
  logPageProblems(page)
  await page.addInitScript(id => localStorage.setItem('mdnb-active-vault', id), MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()
  await page.locator('pre').first().waitFor({ timeout: 20000 })

  if (wantColour) {
    // Colour comes from the worker, so wait for the spans themselves: a frame
    // taken before the reply would photograph the plain-text pending state and
    // pass as evidence of a feature that never ran.
    await page.locator(HL_SPAN).first().waitFor({ timeout: 20000 })
  } else {
    // The opposite claim needs the opposite assertion, and it needs a moment to
    // be meaningful: give the worker longer than a highlight round-trip, then
    // require that nothing was coloured.
    await page.waitForTimeout(2000)
    if ((await page.locator(HL_SPAN).count()) !== 0) throw new Error('unlabelled fence was coloured')
  }
  await page.waitForTimeout(400)

  const applied = await page.evaluate(() => ({
    theme: document.documentElement.dataset.theme || '',
    mode: document.documentElement.dataset.mode || '',
  }))
  const wantMode = theme === 'light' ? 'light' : 'dark'
  if (applied.mode !== wantMode) {
    throw new Error(`mode mismatch: wanted ${wantMode}, got ${applied.mode || '(none)'}`)
  }

  await page.screenshot({ path: `${OUT}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${OUT}/${file}`, `[${applied.theme || wantMode}]`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const coloured = mdnbNoteDoc(NOTE_PATH, NOTE)
  const plain = mdnbNoteDoc(NOTE_PATH, PLAIN_NOTE)
  try {
    await shoot(browser, base, coloured, { file: '01-highlighted-dark.png', theme: 'dark', wantColour: true })
    await shoot(browser, base, coloured, { file: '02-highlighted-light.png', theme: 'light', wantColour: true })
    await shoot(browser, base, plain, { file: '03-unlabelled-stays-plain.png', theme: 'dark', wantColour: false })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
