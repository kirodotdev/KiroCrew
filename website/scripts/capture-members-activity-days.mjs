/**
 * Capture the Crew Members side panel (Crew summary tab) on a REAL pod, for the
 * "Recent activity by day" change (before/after). Not a test — a screenshot harness.
 *
 * Usage:
 *   POD_INFO="$KIROCREW_SCRATCH/pod-info.json" \
 *     node scripts/capture-members-activity-days.mjs <outDir> <prefix> [lang]
 *
 * Writes <outDir>/<prefix>-light.png and <outDir>/<prefix>-dark.png (zh-CN),
 * plus <prefix>-{light,dark}-en.png (English).
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { check, podInfo } from './lib/crew-pod-harness.mjs'

const OUT = process.argv[2]
const PREFIX = process.argv[3]
const MEMBER = process.env.MEMBER || 'radar'
if (!OUT || !PREFIX) throw new Error('usage: <outDir> <prefix>')
mkdirSync(OUT, { recursive: true })

const { BASE, authed } = podInfo(readFileSync)

async function prime(page, theme, lang) {
  await page.goto(authed('/members'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const skip = page.getByRole('button', { name: /Skip this version|跳过此版本/ })
  if (await skip.waitFor({ state: 'visible', timeout: 3000 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.evaluate(async ([th, lg]) => {
    localStorage.setItem('mc-preview-crew', '1')
    localStorage.setItem('mc-lang', lg)
    localStorage.setItem('mc-theme', th)
    localStorage.setItem('mc-members-drawer-open', '1')
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: th, language: lg, onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
  }, [theme, lang])
}

async function openMember(page) {
  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  const row = page.locator('#main-content li button', { hasText: MEMBER }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  await page.getByTestId('member-title-row').waitFor({ state: 'visible', timeout: 10000 })
  const drawer = page.getByTestId('member-crew-summary')
  if (!(await drawer.isVisible().catch(() => false))) await page.getByTestId('member-panel-toggle').click()
  await drawer.waitFor({ state: 'visible', timeout: 10000 })
  // Activity, wake sources and stats must all have settled before the frame.
  await page.getByTestId('member-activity-loading').waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {})
  await page.getByTestId('member-wake-loading').waitFor({ state: 'hidden', timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(600)
  if (process.env.EXPECT_TESTID) {
    await page.getByTestId(process.env.EXPECT_TESTID).first().waitFor({ state: 'visible', timeout: 10000 })
  }
  return drawer
}

async function shoot(browser, theme, lang, suffix) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 2,
    timezoneId: 'UTC',
    locale: lang,
  })
  const page = await context.newPage()
  await prime(page, theme, lang)
  const drawer = await openMember(page)
  const stats = page.getByTestId('member-stats')
  check(`[${theme}/${lang}] stats rendered`, await stats.isVisible())
  const statText = await stats.innerText()
  check(`[${theme}/${lang}] stats are numbers, not dashes`, !/–/.test(statText), statText.replace(/\s+/g, ' '))
  // The whole DetailPanel column: header (avatar + name) down to the footer.
  const panel = page.getByTestId('member-side-panel')
  check(`[${theme}/${lang}] panel located`, await panel.count() === 1)
  const mode = await page.evaluate(() => document.documentElement.dataset.mode)
  check(`[${theme}/${lang}] theme applied`, mode === theme, `data-mode=${mode}`)
  const file = join(OUT, `${PREFIX}-${theme}${suffix}.png`)
  await panel.screenshot({ path: file })
  console.log('wrote', file)
  if (process.env.OPEN_DAY_INDEX) {
    // A day with a routed pick in it, so the frame shows the routed marker
    // (icon + accent) next to plain chat times.
    const row = page.getByTestId('member-activity-day').nth(Number(process.env.OPEN_DAY_INDEX))
    await row.click()
    const times = page.getByTestId('member-activity-times')
    await times.waitFor({ state: 'visible', timeout: 5000 })
    check(`[${theme}/${lang}] opened day shows a routed time`, (await times.locator('[data-routed]').count()) >= 1)
    await page.mouse.move(5, 5)
    await page.waitForTimeout(300)
    const openFile = join(OUT, `${PREFIX}-${theme}${suffix}-open.png`)
    await panel.screenshot({ path: openFile })
    console.log('wrote', openFile)
    // The unfolded list: every day row plus the "Show less" control.
    const before = await page.getByTestId('member-activity-day').count()
    await page.getByTestId('member-activity-more').click()
    await page.waitForTimeout(300)
    const after = await page.getByTestId('member-activity-day').count()
    check(`[${theme}/${lang}] unfolding shows more days`, after > before, `${before} -> ${after}`)
    await page.mouse.move(5, 5)
    const allFile = join(OUT, `${PREFIX}-${theme}${suffix}-unfolded.png`)
    await panel.screenshot({ path: allFile })
    console.log('wrote', allFile)
  }
  await context.close()
  return drawer
}

const browser = await chromium.launch()
try {
  for (const theme of ['light', 'dark']) {
    await shoot(browser, theme, 'zh-CN', '')
    await shoot(browser, theme, 'en', '-en')
  }
} finally {
  await browser.close()
}
