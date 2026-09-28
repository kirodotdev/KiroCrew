/** Capture real App Store components with fixture API reads, never the live gateway. */
import { chromium, expect } from '@playwright/test'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:6812'
const out = process.argv[3] || '../temp-screenshots/appstore-sources'
mkdirSync(out, { recursive: true })
const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Capture has no gateway' }),
  }))
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  for (const theme of ['dark', 'light']) {
    await page.goto(`${base}/capture/appstore-sources.html?theme=${theme}`)
    const team = page.getByRole('button', { name: /Team Apps Registry/ })
    await team.waitFor()
    await expect(page.locator('html')).toHaveAttribute('data-mode', theme)
    await expect(page.getByRole('button', { name: 'All sources' })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByRole('heading', { name: 'Team toolkit' })).toBeVisible()
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-featured.png` })
    await team.scrollIntoViewIfNeeded()
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-all.png` })
    await team.click()
    await expect(team).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByRole('status')).toHaveText('2 apps')
    await expect(page.getByRole('button', { name: 'All apps 2', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: /^View details for/ })).toHaveCount(2)
    await expect(page.getByRole('button', { name: 'View details for Build Tools' })).toContainText('Source Team Apps Registry')
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-filtered.png` })
    await page.getByRole('button', { name: 'View details for Build Tools' }).click()
    await expect(page.getByRole('status')).toHaveCount(0)
    await expect(page.getByText('Team Apps Registry', { exact: true })).toBeVisible()
    await page.screenshot({ animations: 'disabled', path: `${out}/${theme}-detail.png` })
  }
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`${base}/capture/appstore-sources.html?theme=dark`)
    await page.getByRole('button', { name: /Community Apps Registry/ }).click()
    await expect(page.getByRole('status')).toHaveText('1 app')
    await page.getByRole('button', { name: 'View details for Community Calendar' }).scrollIntoViewIfNeeded()
    await page.screenshot({ animations: 'disabled', path: `${out}/narrow-${width}-filtered.png` })
    await page.getByRole('button', { name: 'View details for Community Calendar' }).click()
    await expect(page.getByRole('status')).toHaveCount(0)
    await expect(page.getByText('Community Apps Registry', { exact: true })).toBeVisible()
    await expect(page.getByText('Not vetted', { exact: true })).toBeVisible()
    await page.screenshot({ animations: 'disabled', path: `${out}/narrow-${width}-detail.png` })
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
    if (overflow) throw new Error(`Detail page overflows at ${width}px`)
  }
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(`${base}/capture/appstore-sources.html?theme=dark`)
  await page.getByRole('button', { name: /Empty Registry/ }).click()
  await expect(page.getByText('No matching apps', { exact: true })).toBeVisible()
  await expect(page.getByRole('status')).toHaveText('0 apps')
  await expect(page.getByRole('button', { name: 'All apps 0', exact: true })).toBeVisible()
  await page.screenshot({ animations: 'disabled', path: `${out}/empty.png` })
  for (const [name, label] of [['local-draft', 'Local install'], ['unlisted-app', 'Unknown']]) {
    await page.goto(`${base}/capture/appstore-sources.html?theme=dark&detail=${name}`)
    await expect(page.getByText(label, { exact: true })).toBeVisible()
    await expect(page.getByText('Not vetted', { exact: true })).toHaveCount(0)
    await page.screenshot({ animations: 'disabled', path: `${out}/${name}-detail.png` })
  }
  await page.goto(`${base}/capture/appstore-sources.html?theme=dark&detail=build-tools&sourceError=1`)
  await expect(page.getByText('Source metadata unavailable', { exact: true })).toBeVisible()
  await expect(page.getByText('team', { exact: true })).toBeVisible()
  await expect(page.getByText('Team reviewed', { exact: true })).toHaveCount(0)
  await page.screenshot({ animations: 'disabled', path: `${out}/source-error-detail.png` })
  if (errors.length) throw new Error(errors.join('\n'))
  console.log(`Source selection, detail navigation, narrow layouts, and empty state passed. Frames: ${out}`)
} finally {
  await browser.close()
}
