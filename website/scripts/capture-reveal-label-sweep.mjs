/**
 * Screenshots for the reveal-label sweep: every surface that hands a path to the
 * desktop now names the GATEWAY host's own file manager instead of always saying
 * Finder.
 *
 * Drives the ISOLATED capture entry (website/capture/path-chips.html), which
 * already mounts MarkdownRenderer, MarkdownPanel and FolderPanel against the real
 * stylesheet, and takes the platform as a query param that seeds the same query
 * the prerequisite gate owns in the app. Booting the full SPA is not needed for a
 * label, and a half-stubbed shell photographs its error boundary instead.
 *
 * The path-chip hint lives in a `title` attribute, which the OS draws and no
 * screenshot can show — so that surface is verified by ASSERTING the resolved
 * sentence, and the frames cover the two controls that are visible.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6807 --strictPort   # in another shell
 *   node scripts/capture-reveal-label-sweep.mjs http://127.0.0.1:6807 ../temp-screenshots/reveal-label-sweep
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6807'
const OUT = process.argv[3] || '../temp-screenshots/reveal-label-sweep'
mkdirSync(OUT, { recursive: true })

const FILE = '/Users/diwm/.kiro/crew/workspace/KiroCrew/README.md'
const DIR = '/Users/diwm/.kiro/crew/workspace/KiroCrew'

/** Platform → the wording every surface must land on for that host. */
const CASES = [
  {
    platform: 'darwin',
    name: 'macos',
    label: 'Open in Finder',
    fileHint: 'Click to open / Shift+click to reveal in Finder',
    dirHint: 'Click to browse / Shift+click to reveal in Finder',
  },
  {
    platform: 'win32',
    name: 'windows',
    label: 'Open in File Explorer',
    fileHint: 'Click to open / Shift+click to open in File Explorer',
    dirHint: 'Click to browse / Shift+click to open in File Explorer',
  },
  {
    platform: 'linux',
    name: 'linux',
    label: 'Show in file manager',
    fileHint: 'Click to open / Shift+click to show in file manager',
    dirHint: 'Click to browse / Shift+click to show in file manager',
  },
  {
    // No platform at all: the sentinel a non-owner dashboard user receives, and
    // what a probe that could not run leaves behind. Must read as generic.
    platform: '',
    name: 'unknown',
    label: 'Show in file manager',
    fileHint: 'Click to open / Shift+click to show in file manager',
    dirHint: 'Click to browse / Shift+click to show in file manager',
  },
]

const fail = (msg) => { console.error(msg); process.exitCode = 1 }

async function main() {
  const browser = await chromium.launch()
  for (const c of CASES) {
    const context = await browser.newContext({
      viewport: { width: 900, height: 600 },
      deviceScaleFactor: 2, // 11-13px label type renders soft at 1x
    })
    const page = await context.newPage()
    const q = (scene) => `${BASE}/capture/path-chips.html?scene=${scene}&theme=dark`
      + (c.platform ? `&platform=${c.platform}` : '')

    // 1. Folder panel — icon-only control, so the wording is only reachable
    //    through title/aria-label. Assert BOTH: a tooltip alone leaves a screen
    //    reader with nothing, an aria-label alone leaves a hovering pointer user
    //    with a mystery glyph.
    await page.goto(q('folder'))
    const reveal = page.getByRole('button', { name: c.label })
    await reveal.waitFor()
    const title = await reveal.getAttribute('title')
    if (title !== c.label) fail(`${c.name}: folder panel title is ${JSON.stringify(title)}, expected ${c.label}`)
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/01-folder-panel-${c.name}.png` })

    // 2. Markdown panel ⋯ menu — the text entry.
    await page.goto(q('reveal'))
    await page.getByTestId('markdown-panel-more-options').click()
    await page.getByText(c.label, { exact: true }).waitFor()
    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/02-panel-menu-${c.name}.png` })

    // 3. Path chips — the hint is a native `title`, undrawable in a screenshot,
    //    so read it off both a file chip and a directory chip.
    await page.goto(q('chips'))
    await page.locator(`code[data-path="${FILE}"]`).waitFor()
    const got = {
      file: await page.locator(`code[data-path="${FILE}"]`).first().getAttribute('title'),
      dir: await page.locator(`code[data-path="${DIR}"]`).first().getAttribute('title'),
    }
    if (got.file !== `${FILE}\n${c.fileHint}`) fail(`${c.name}: file chip hint is ${got.file}`)
    if (got.dir !== `${DIR}\n${c.dirHint}`) fail(`${c.name}: dir chip hint is ${got.dir}`)
    console.log(`${c.name.padEnd(8)} label=${c.label} · file chip=${JSON.stringify(got.file)}`)

    await context.close()
  }
  await browser.close()
  if (process.exitCode) console.error('wording assertions FAILED')
  else console.log(`wrote ${CASES.length * 2} screenshots to ${OUT}`)
}

main().catch((err) => { console.error(err); process.exit(1) })
