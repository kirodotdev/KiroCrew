import { test, expect } from '@playwright/test'

/**
 * An app's link-preview card runs in a frame the dashboard creates
 * (`linkPreviewFrames.ts`). In a real browser, that frame must not reach the
 * dashboard: not its document, not its location, not its cookies or storage.
 *
 * The app list, the mint and the frame document are routed here so no app has to
 * be installed; the frame element, its sandbox and its placement are the
 * dashboard's own. The frame document carries the same `sandbox allow-scripts`
 * response policy the gateway sends (`apps/app_frame.py`, pinned by
 * test/test_app_frame.py).
 */

const APP = 'probe-app'
const FRAME = `/app-frame/${APP}/linkPreviews/cards/ffffffffffffffff/t0k3n`

const PROBE = `<!doctype html><html><body><script>
const out = { origin: self.origin };
const tryIt = (k, f) => { try { out[k] = String(f()); } catch (e) { out[k] = e.name; } };
tryIt('parentDocument', () => window.parent.document.title);
tryIt('parentLocation', () => window.parent.location.href);
tryIt('topDocument', () => window.top.document.body.innerHTML.length);
tryIt('cookie', () => document.cookie);
tryIt('localStorage', () => localStorage.length);
fetch('/api/status', { credentials: 'include' })
  .then((r) => { out.apiFetch = 'status ' + r.status; })
  .catch((e) => { out.apiFetch = e.name; })
  .finally(() => parent.postMessage({ lpProbe: out }, '*'));
</script></body></html>`

test('the link-preview frame cannot read the dashboard', async ({ page }) => {
  await page.addInitScript(() => {
    window.addEventListener('message', (e) => {
      const d = e.data as { lpProbe?: unknown } | null
      if (d && typeof d === 'object' && d.lpProbe) (window as unknown as { __lpProbe: unknown }).__lpProbe = d.lpProbe
    })
  })
  await page.route('**/api/apps', (route) => route.fulfill({
    json: [{
      name: APP,
      enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/lp.mjs', hosts: ['probe.example.com'] }] } },
    }],
  }))
  await page.route(`**/api/apps/${APP}/frames/linkPreviews/*`, (route) => route.fulfill({
    json: { frame: FRAME, frameId: 'ffffffffffffffff', capability: 'ffffffffffffffff~cap', ttl: 900 },
  }))
  await page.route(`**${FRAME}`, (route) => route.fulfill({
    status: 200,
    contentType: 'text/html',
    headers: { 'Content-Security-Policy': "sandbox allow-scripts; frame-ancestors 'self'" },
    body: PROBE,
  }))

  await page.goto('/')
  await page.evaluate(() => {
    // A chat message's root carries this scope; links elsewhere reach no app.
    const scope = document.createElement('div')
    scope.setAttribute('data-link-preview-scope', '')
    document.body.append(scope)
    const a = document.createElement('a')
    a.href = 'https://probe.example.com/item/1'
    a.textContent = 'probe link'
    a.id = 'lp-probe-link'
    Object.assign(a.style, { position: 'fixed', left: '200px', top: '200px', zIndex: '2147483646' })
    scope.append(a)
  })
  await page.locator('#lp-probe-link').hover()

  const frame = page.locator(`iframe[data-link-preview-frame="${APP}:cards"]`)
  await expect(frame).toHaveCount(1, { timeout: 10_000 })
  await expect(frame).toHaveAttribute('sandbox', 'allow-scripts')

  await expect.poll(() => page.evaluate(() => (window as unknown as { __lpProbe?: unknown }).__lpProbe ?? null), { timeout: 10_000 })
    .not.toBeNull()
  const probe = await page.evaluate(() => (window as unknown as { __lpProbe: Record<string, string> }).__lpProbe)
  expect(probe.origin).toBe('null')
  expect(probe.parentDocument).toBe('SecurityError')
  expect(probe.parentLocation).toBe('SecurityError')
  expect(probe.topDocument).toBe('SecurityError')
  expect(probe.cookie).toBe('SecurityError')
  expect(probe.localStorage).toBe('SecurityError')
  // An opaque origin gets no dashboard response: CORS refuses it, cookie or not.
  // (/api/status, not the routed /api/apps: a routed reply skips the real gateway.)
  expect(probe.apiFetch).toBe('TypeError')
})
