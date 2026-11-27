import { test, expect, type Page } from '@playwright/test'

/**
 * A failed load of the app registry chunk (`shared-modules-*.js`) must not
 * reload the dashboard when nobody asked to open an app: a hover prefetch, or a
 * background startup mount. A reload there drops whatever the user was typing.
 *
 * This runs against the BUILT dashboard on purpose. Vite's build wraps every
 * dynamic `import()` in its preload helper, which fires `vite:preloadError`;
 * the order of that event against the loader's own handlers is only real in a
 * build, so a unit test cannot catch a regression here (#43471).
 *
 * The installed-app list, the probe apps' records and their bundles are stubbed,
 * so the spec needs no installed app and no agent.
 */

const REGISTRY = /\/assets\/shared-modules-[^/]+\.js$/
const ROW = '[data-onboarding-nav="app-probe-a"]'
const TEXT = 'unsaved draft 123'

const PROBE = (app: string) => `import { jsx } from 'react/jsx-runtime'
export default function Probe() { return jsx('div', { 'data-probe': '${app}', children: 'probe ${app}' }) }`

function record(name: string, startup = false) {
  return {
    name, version: '1.0.0', displayName: name, enabled: true, origin: 'local',
    manifest: { name, version: '1.0.0', displayName: name, permissions: {}, ui: { entry: 'index.js', pages: [{ route: `/${name}`, label: name }] } },
    ...(startup ? { startup: { state: 'admitted', entry: 'startup.js', digest: 'd1' } } : {}),
  }
}

async function stubApps(page: Page, apps: ReturnType<typeof record>[]) {
  await page.route(/\/api\/apps$/, route => route.fulfill({ json: apps }))
  await page.route(/\/api\/apps\/probe-[a-z]+$/, route => {
    const name = new URL(route.request().url()).pathname.split('/').pop()!
    const app = apps.find(a => a.name === name)
    return app ? route.fulfill({ json: app }) : route.fallback()
  })
  await page.route(/\/apps\/probe-[a-z]+\/ui\/.*\.js$/, route => {
    const name = new URL(route.request().url()).pathname.split('/')[2]
    return route.fulfill({ contentType: 'text/javascript', body: PROBE(name) })
  })
}

/**
 * Note each `vite:preloadError` in sessionStorage, where it survives a reload.
 * main.tsx's handler bumps `vite-preload-reload-count` there, synchronously,
 * just before it reloads, so the two together say whether it chose to reload.
 */
async function notePreloadErrors(page: Page) {
  await page.addInitScript(() => {
    addEventListener('vite:preloadError', () => { sessionStorage.setItem('e2e-preload-error', '1') })
  })
}

/** Set a marker a reload would wipe, and count page loads from here on. */
async function watchReloads(page: Page) {
  let loads = 0
  page.on('load', () => { loads++ })
  await page.evaluate(() => { (window as unknown as { __noReload: number }).__noReload = 1 })
  const store = (key: string) => page.evaluate(k => sessionStorage.getItem(k), key)
  return {
    loads: () => loads,
    marker: () => page.evaluate(() => (window as unknown as { __noReload?: number }).__noReload ?? null),
    /** Resolves once the chunk failure reached the page's handler. */
    failureHandled: () => expect.poll(() => store('e2e-preload-error')).toBe('1'),
    reloadCount: () => store('vite-preload-reload-count'),
  }
}

// A service worker would answer the chunk itself, out of reach of page.route.
test.use({ serviceWorkers: 'block' })

test.describe('app registry chunk failure (built dashboard)', () => {
  test('a failed hover prefetch does not reload the page', async ({ page }) => {
    await stubApps(page, [record('probe-a')])
    await notePreloadErrors(page)
    await page.route(REGISTRY, route => route.abort('failed'))
    await page.goto('/', { waitUntil: 'load' })
    await page.locator(ROW).waitFor()
    const watch = await watchReloads(page)
    await page.hover(ROW)
    await watch.failureHandled()
    expect(await watch.reloadCount()).toBeNull()
    expect(await watch.marker()).toBe(1)
    expect(watch.loads()).toBe(0)
  })

  test('a failed registry load on a startup mount does not reload the page', async ({ page }) => {
    await notePreloadErrors(page)
    await stubApps(page, [record('probe-a'), record('probe-s', true)])
    let release: () => void = () => {}
    const held = new Promise<void>(resolve => { release = resolve })
    let failed = 0
    await page.route(REGISTRY, async route => {
      await held
      failed++
      await route.fulfill({ status: 404, contentType: 'text/plain', body: 'gone' })
    })
    await page.goto('/', { waitUntil: 'load' })
    await page.locator(ROW).waitFor()
    const watch = await watchReloads(page)
    // Something the user typed, which a reload would lose.
    const input = page.getByPlaceholder(/search/i).first()
    await input.fill(TEXT)
    release()
    await expect.poll(() => failed).toBeGreaterThan(0)
    await watch.failureHandled()
    expect(await watch.reloadCount()).toBeNull()
    // The startup app reports its own failure in this page instead.
    await expect(page.locator('[data-testid^="startup-module-error-"]').first()).toBeVisible()
    expect(watch.loads()).toBe(0)
    expect(await watch.marker()).toBe(1)
    await expect(input).toHaveValue(TEXT)
  })
})
