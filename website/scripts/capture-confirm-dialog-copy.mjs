/**
 * Screenshots for the confirm-dialog copy pass (#5243).
 *
 * Drives website/capture/confirm-dialog-copy.html. Two scenes, both mounting
 * the REAL useConfirm() dialog with the REAL i18n keys:
 *   - deploy-destroy: the deploy page's destroy confirm (title + body).
 *   - papyrus-discard: the Papyrus co-author-conflict discard guard.
 *
 * Each scene ASSERTS THE RENDERED TEXT before writing the file, so a frame
 * cannot silently photograph the wrong state. Default (after) expects the
 * consequence-first bodies and the unified "changes" verb; --before inverts
 * the assertions (run it against base catalogs, e.g. with the locale changes
 * stashed) and expects the question-restating bodies and "edits".
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-confirm-dialog-copy.mjs http://127.0.0.1:6824 ../temp-screenshots/confirm-dialog-copy [prefix] [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/confirm-dialog-copy'
const PREFIX = (process.argv[4] && !process.argv[4].startsWith('--')) ? process.argv[4] : ''
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const DESTROY_TITLE = 'Destroy site?'
const DESTROY_AFTER = "Permanently deletes bucket kc-site-blog-8f3a and distribution E2ABCDEF123 for 'blog'. Cannot be undone."
const DESTROY_BEFORE = "DESTROY 'blog'? Permanently deletes bucket kc-site-blog-8f3a and distribution E2ABCDEF123. Cannot be undone."
const DISCARD_TITLE_AFTER = 'Discard unsaved changes?'
const DISCARD_TITLE_BEFORE = 'Discard unsaved edits?'
const DISCARD_AFTER = 'Discards your unsaved changes to \u201cdraft.tex\u201d and loads the co-author\u2019s version. This cannot be undone.'
const DISCARD_BEFORE = 'Discard your unsaved edits to \u201cdraft.tex\u201d and load the co-author\u2019s version? This cannot be undone.'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 560, height: 400 }, deviceScaleFactor: 2 })
let failed = false

async function scene(name, title, body) {
  await page.goto(`${BASE}/capture/confirm-dialog-copy.html?scene=${name}`)
  const dialog = page.getByRole('dialog')
  try {
    await dialog.waitFor({ timeout: 10000 })
    await page.getByText(title, { exact: true }).waitFor({ timeout: 5000 })
    await page.getByText(body, { exact: true }).waitFor({ timeout: 5000 })
    console.log(`${name}: title=${JSON.stringify(title)} body OK`)
    await page.screenshot({ path: `${OUT}/${PREFIX}${name}.png` })
  } catch {
    const got = await dialog.innerText().catch(() => '(no dialog)')
    console.error(`${name}: expected title ${JSON.stringify(title)} + body ${JSON.stringify(body)}; dialog shows: ${JSON.stringify(got)}`)
    failed = true
  }
}

await scene('deploy-destroy', DESTROY_TITLE, BEFORE ? DESTROY_BEFORE : DESTROY_AFTER)
await scene('papyrus-discard',
  BEFORE ? DISCARD_TITLE_BEFORE : DISCARD_TITLE_AFTER,
  BEFORE ? DISCARD_BEFORE : DISCARD_AFTER)

await browser.close()
if (failed) {
  console.error('one or more scenes did not render the expected text — no misleading frame written')
  process.exit(1)
}
console.log(`wrote screenshots to ${OUT}`)
