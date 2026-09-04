// Spec Builder — PR screenshot capture.
//
// Drives a REAL instance of this branch (isolated gateway, own port, own
// KIROCREW_HOME — never the live plane) and captures the surfaces a reviewer
// needs, in both palettes (the app is theme-token driven, so light/dark is a
// meaningful variant).
//
// Usage:
//   node website/scripts/capture-spec-builder.mjs <baseUrl> <authValue> <outDir> [homeDir]
//
// Each state is ASSERTED before it is shot, so a blank page or an auth failure
// fails the run instead of silently producing an empty screenshot. (That is not
// theoretical: this assertion is what caught the rail unmounting on first run.)
//
// The first-run state is reached by moving the fixture index aside — the backend
// reads it per request, so no gateway restart is needed.
import { chromium } from 'playwright'
import { mkdir, rename } from 'node:fs/promises'
import { existsSync } from 'node:fs'

const [baseUrl, auth, outDir, homeDir] = process.argv.slice(2)
if (!baseUrl || !auth || !outDir) {
  console.error('usage: capture-spec-builder.mjs <baseUrl> <authValue> <outDir> [homeDir]')
  process.exit(2)
}

// `${home}/workspace/spec-builder/index.json` -- mirrors the backend's
// _state_dir(), which is config_dir()/'workspace'/APP_NAME. Without the
// `workspace` segment this path never existed, so the move-aside below
// silently no-opped and the first-run captures were taken against a
// populated index.
const INDEX = homeDir ? `${homeDir}/workspace/spec-builder/index.json` : ''
const INDEX_ASIDE = INDEX ? `${INDEX}.aside` : ''

await mkdir(outDir, { recursive: true })

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 2,
})
const page = await context.newPage()

// Pre-seed localStorage so first-run onboarding/theme modals never mount and
// overlay the app (the same suppression the pod-e2e runner performs).
await page.addInitScript(() => {
  try {
    localStorage.setItem('kc.onboarded', '1')
    localStorage.setItem('kc.changelogSeen', '9999')
  } catch { /* private mode */ }
})

const shots = []
async function shot(name) {
  await page.waitForTimeout(200)
  await page.screenshot({ path: `${outDir}/${name}` })
  shots.push(name)
  console.log(`  captured ${name}`)
}

async function setTheme(theme) {
  await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme)
  await page.waitForTimeout(350)
}

async function open() {
  await page.goto(`${baseUrl}/spec-builder?tk=${encodeURIComponent(auth)}`.replace('tk=', 'token='), {
    waitUntil: 'networkidle',
  })
  await page.keyboard.press('Escape').catch(() => {})
  const close = page.locator('[aria-label="Close"]')
  if (await close.count()) await close.first().click().catch(() => {})
}

// Builtin routes are gated on the app being enabled; a fresh home may not be.
const enable = await page.request.post(
  `${baseUrl}/api/apps/spec-builder/enable?${new URLSearchParams({ token: auth })}`,
)
console.log(`enable spec-builder -> ${enable.status()}`)

// ── 1. populated workspace: rail with specs, nothing selected ──────────────
console.log('populated workspace…')
await open()
await page.getByText('teams-notifications').first().waitFor({ timeout: 20_000 })
// Rail identity footer (part of the Issue Radar parity work).
await page.getByText(/^v\d+\.\d+\.\d+$/).first().waitFor({ timeout: 5_000 })
await setTheme('dark')
await shot('01-rail-populated-dark.png')

// ── 2. spec selected: chat | docs split with a real document ───────────────
console.log('spec detail…')
await page.getByRole('button', { name: /teams-notifications/ }).first().click()
await page.getByText('Requirements — Teams notifications').waitFor({ timeout: 15_000 })
await shot('02-spec-detail-dark.png')
await setTheme('light')
await shot('03-spec-detail-light.png')

// ── 3. new-spec view + project picker ──────────────────────────────────────
console.log('new-spec view…')
await setTheme('dark')
await page.getByRole('button', { name: 'New spec' }).first().click()
await page.getByText('Let’s plan something').waitFor({ timeout: 10_000 })
await page.getByText('Build something new').waitFor({ timeout: 5_000 })
await shot('04-new-spec-dark.png')

await page.getByRole('button', { name: /Choose a project folder/i }).click()
await page.waitForTimeout(1200)
await shot('05-project-picker-dark.png')

// ── 4. first-run empty state (index moved aside, read per request) ─────────
if (INDEX && existsSync(INDEX)) {
  console.log('first-run empty state…')
  await rename(INDEX, INDEX_ASIDE)
  try {
    await open()
    await page.getByText('Plan your next feature with a spec').waitFor({ timeout: 15_000 })
    // The rail MUST still be mounted here — it used to unmount, taking the
    // identity footer and Settings entry point with it.
    await page.getByText(/^v\d+\.\d+\.\d+$/).first().waitFor({ timeout: 5_000 })
    await setTheme('dark')
    await shot('06-first-run-dark.png')
    await setTheme('light')
    await shot('07-first-run-light.png')
  } finally {
    await rename(INDEX_ASIDE, INDEX)
  }
}

console.log(`\n${shots.length} screenshots -> ${outDir}`)
await browser.close()
