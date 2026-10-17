/**
 * Screenshots for PR #14970: Settings → Security → the registry-trust section,
 * after the copy split (rail keeps the short "Registry trust", the pane's
 * heading is now the descriptive "Registries trusted with your Git
 * credentials"). The PR's original three frames showed an older copy revision,
 * so this re-captures the SHIPPED UI as evidence.
 *
 * Boots Vite in-process to serve capture/trusted-registries-settings.html,
 * which mounts the REAL SecurityPanel against the real stylesheet with the
 * gateway reads seeded into the query cache (see that entry file). Scene + theme
 * come from the query string:
 *   rows    — one trusted + one untrusted operator registry
 *   confirm — the grant-confirm dialog open (shows "Registry address: <url>")
 *   empty   — no hand-added registries (shows the "Add one under Apps." pointer)
 *   change-failed — a grant rejected with a long backend detail wrapping in the card
 *   corrupt — the corrupt-keystone notice
 *   unavailable — the read-failure notice with Retry
 *
 * Each frame is self-checking: it waits for the text a reviewer needs to see in
 * that scene and fails loudly if the surface rendered blank or wrong, so a
 * broken frame is never published as evidence.
 *
 * Usage:
 *   node scripts/capture-trusted-registries.mjs [outDir]
 * Default outDir: $KIROCREW_SCRATCH/shots-14970
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2]
  || join(process.env.KIROCREW_SCRATCH || '/tmp', 'shots-14970')
mkdirSync(OUT, { recursive: true })

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const vite = await createServer({
  root: ROOT,
  configFile: join(ROOT, 'vite.config.ts'),
  server: { host: '127.0.0.1', port: 0, strictPort: false },
  logLevel: 'warn',
})
await vite.listen()
const { port } = vite.httpServer.address()
const base = `http://127.0.0.1:${port}`

// The one string each scene must render before the shot — the readiness signal
// AND the proof the scene drew what it claims.
const CONFIRM_REPO = 'https://git.example.test/community/index.git'

/** name → { scene, theme, expect: [visible strings], extra?: async fn } */
const FRAMES = [
  {
    name: 'rows-dark',
    scene: 'rows',
    theme: 'dark',
    // Rail short label AND the descriptive pane heading in one frame, plus a row.
    expect: ['Registry trust', 'Registries trusted with your Git credentials', 'team-apps'],
  },
  {
    name: 'rows-light',
    scene: 'rows',
    theme: 'light',
    expect: ['Registry trust', 'Registries trusted with your Git credentials', 'team-apps'],
  },
  {
    name: 'confirm-dark',
    scene: 'confirm',
    theme: 'dark',
    // The monospace "Registry address: <url>" line above the confirm body.
    expect: [`Registry address: ${CONFIRM_REPO}`],
  },
  {
    name: 'empty-dark',
    scene: 'empty',
    theme: 'dark',
    // The empty state with "Apps" as a link.
    expect: ['No registries added by hand.'],
  },
  {
    name: 'not-served-dark',
    scene: 'not-served',
    theme: 'dark',
    // A row a pinned registry outranks shows its "Not used — …" note and no
    // grant/revoke control. 1280x800 per the brief.
    viewport: { width: 1280, height: 800 },
    expect: [
      'Not used — a registry that comes with',
      'Not listed — this registry is not shown',
      'Not configured — this registry is not in your config',
    ],
  },
  {
    name: 'unavailable-dark',
    scene: 'unavailable',
    theme: 'dark',
    // A failed read: the notice and its Retry button, no stale rows.
    viewport: { width: 1280, height: 800 },
    expect: ['Could not read which registries are trusted'],
  },
  {
    name: 'change-failed-dark',
    scene: 'change-failed',
    theme: 'dark',
    // A grant refused with code unknown_registry: the change_failed notice shows
    // the card's own short copy, not the backend detail. 1280x800 per the brief.
    viewport: { width: 1280, height: 800 },
    expect: ['That change did not take effect', 'This registry was removed from Apps'],
  },
  {
    name: 'corrupt-dark',
    scene: 'corrupt',
    theme: 'dark',
    // The corrupt-keystone notice, above the rows.
    viewport: { width: 1280, height: 800 },
    expect: ['The trust file is damaged'],
  },
]

const browser = await chromium.launch({ executablePath: chromiumExecutable() })
try {
  for (const frame of FRAMES) {
    const context = await browser.newContext({ viewport: frame.viewport || { width: 1080, height: 1000 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    const pageErrors = []
    page.on('pageerror', e => pageErrors.push(e.message))

    await page.goto(
      `${base}/capture/trusted-registries-settings.html?scene=${frame.scene}&theme=${frame.theme}`,
      { waitUntil: 'networkidle' },
    )

    for (const text of frame.expect) {
      await page.getByText(text, { exact: false }).first().waitFor({ state: 'visible', timeout: 25_000 })
    }

    // The confirm scene renders a modal dialog: crop to it so the shot centres
    // on the "Registry address" line and the confirm body. Every other scene
    // crops to the panel so the rail label and the pane heading are both in view.
    const target = frame.scene === 'confirm'
      ? page.getByRole('dialog')
      : page.locator('[data-capture-root]')
    await target.first().waitFor({ state: 'visible', timeout: 25_000 })
    await page.waitForTimeout(300)
    await target.first().screenshot({ path: join(OUT, `${frame.name}.png`) })
    console.log(`captured ${frame.name}.png`)

    if (pageErrors.length) throw new Error(`${frame.name}: uncaught page errors: ${pageErrors.join(' | ')}`)
    await context.close()
  }
} finally {
  await browser.close()
  await vite.close()
}
console.log(`done → ${OUT}`)
