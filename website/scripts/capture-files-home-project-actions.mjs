/**
 * Real-browser evidence for the Files tab's per-project terminal action (#1142).
 *
 * Drives the isolated capture entry (website/capture/files-home-project-actions.html),
 * which mounts the REAL `FilesHomePanel` with only its transport stubbed.
 *
 * Frames, per theme:
 *   01-header-closed          the Files header: Reveal, then `Project actions`
 *   02-project-actions-open   the menu: the terminal row and what it explains
 *
 * Assertions, so a frame cannot silently photograph the wrong thing:
 *   - the header carries exactly two controls, in order (the row cap the action
 *     was collapsed into one trigger to respect);
 *   - the open menu carries the terminal row, label AND its one-line note;
 *   - the note states what the action IS rather than what it avoids — the
 *     reassurance wording an earlier round tried is pinned OUT, because a reader
 *     took it for a warning;
 *   - the applied palette matches the filename's theme.
 *
 * Every frame is taken with animations disabled: Radix fades its menu in over
 * ~150ms, and a shot inside that window caught a menu with no painted background
 * whose items overlapped the content beneath — a reader could not tell what a
 * click would hit, in a frame that is the PR's own evidence for that control.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort    # in another shell (website/)
 *   node scripts/capture-files-home-project-actions.mjs http://127.0.0.1:6841 ../temp-screenshots/files-home-project-actions-1142
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/files-home-project-actions-1142'
mkdirSync(OUT, { recursive: true })

/** The panel's own dock width, so the capture IS the panel. */
const VIEWPORT = { width: 460, height: 720 }
const HEADER_BUTTONS = ['Show in file manager', 'Project actions']
/** Rendered text: the row's label followed by its one-line note. */
const MENU_ITEMS = [
  'Open terminal in the project directoryA command line that starts in this project’s folder.',
]
/** The framing that failed, kept as a negative assertion. */
const REASSURANCE = 'Nothing runs until you type'
const SHOT = { animations: 'disabled' }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const fail = (label, message) => { console.error(`[${label}] ${message}`); failures++ }

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  page.on('pageerror', e => fail(theme, `pageerror: ${e.message}`))
  await page.goto(`${BASE}/capture/files-home-project-actions.html?theme=${theme}`, { waitUntil: 'networkidle' })

  // Wait for the header's settled shape rather than a fixed sleep: Reveal
  // appears only once the branding read has answered `direct_local`.
  const trigger = page.getByRole('button', { name: 'Project actions' })
  await trigger.waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: 'Show in file manager' }).waitFor({ timeout: 15000 })

  // The palette is asserted, not assumed: an unset preference resolves to the
  // HOST's mode, so a frame can otherwise claim a theme it does not carry.
  const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  if (applied !== (theme === 'light' ? 'kiro-light' : 'kiro-dark')) {
    fail(theme, `data-theme=${applied} does not match the frame name`)
  }

  // The header ROW is the parent of the tab's own `Files` label. Reached that way
  // rather than by a class chain, so this driver encodes none of the component's
  // internals — and rather than by an ancestor `hasText` filter, which matches
  // the whole panel and would count the rail's controls too.
  const header = page.getByText('Files', { exact: true }).locator('xpath=..')
  const names = await header.getByRole('button').evaluateAll(
    els => els.map(el => el.getAttribute('aria-label')),
  )
  if (JSON.stringify(names) !== JSON.stringify(HEADER_BUTTONS)) {
    fail(theme, `header buttons ${JSON.stringify(names)} != ${JSON.stringify(HEADER_BUTTONS)}`)
  }
  await page.screenshot({ path: `${OUT}/${theme}-01-header-closed.png`, ...SHOT })

  await trigger.click()
  const items = page.getByRole('menuitem')
  await items.first().waitFor({ timeout: 15000 })
  const itemNames = await items.evaluateAll(els => els.map(el => el.textContent?.trim() || ''))
  if (JSON.stringify(itemNames) !== JSON.stringify(MENU_ITEMS)) {
    fail(theme, `menu items ${JSON.stringify(itemNames)} != ${JSON.stringify(MENU_ITEMS)}`)
  }
  if (itemNames.some(n => n.includes(REASSURANCE))) {
    fail(theme, 'the row went back to reassuring instead of saying what it is')
  }
  await page.screenshot({ path: `${OUT}/${theme}-02-project-actions-open.png`, ...SHOT })
  console.log(`[${theme}] header=${JSON.stringify(names)} menu=${JSON.stringify(itemNames)}`)
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)
