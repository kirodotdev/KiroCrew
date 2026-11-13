import { test, expect, type Page, type Request } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * A link-preview card behind an authenticating reverse proxy in front of the
 * dashboard: the proxy refuses every request that carries no session cookie, and a
 * sandboxed frame's own requests never carry one.
 *
 * The proxy is emulated with routes: a request the card frame makes itself is
 * answered `403` with an HTML login page, as such a proxy answers it; a request this
 * page makes goes through. The frame document is the gateway's own srcdoc document
 * (`apps/app_frame.py`, generated here by the installed package), so the bootstrap
 * under test is the real one.
 */

const HERE = dirname(fileURLToPath(import.meta.url))
const PROBE_LINK = 'https://probe.example.com/item/1'
const PYTHON = process.env.KIROCREW_PYTHON || 'python3'

test.use({ serviceWorkers: 'block' })

/** The frame document the mint hands the dashboard, from the real gateway code. */
function srcdocDocument(app: string, entry: string): string {
  const program = [
    'import sys',
    'from kiro_crew.apps import app_frame as lp',
    'p = lp._POINTS["linkPreviews"]',
    'sys.stdout.write(lp._frame_html(sys.argv[1], sys.argv[2], "Nprobe0123456789", p.script, srcdoc=True))',
  ].join('\n')
  return execFileSync(PYTHON, ['-c', program, app, entry], { encoding: 'utf8' })
}

/** True for a request the card frame made itself (no cookie reaches the proxy). */
function fromCardFrame(page: Page, request: Request): boolean {
  try {
    return request.frame() !== page.mainFrame()
  } catch {
    return false
  }
}

const LOGIN_PAGE = { status: 403, contentType: 'text/html; charset=UTF-8', body: '<!doctype html><title>Access Required</title>Access Required' }

async function setUp(page: Page, opts: { app: string; fixture: string; proxy: boolean }) {
  const { app, fixture, proxy } = opts
  const moduleSource = readFileSync(join(HERE, 'fixtures', fixture, 'ui', 'lp.mjs'), 'utf8')
  const document = srcdocDocument(app, 'lp.mjs')
  const frame = `/app-frame/${app}/linkPreviews/cards/ffffffffffffffff/t0k3n`
  await page.route('**/api/apps', (route) => route.fulfill({
    json: [{
      name: app,
      enabled: true,
      manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'lp.mjs', hosts: ['probe.example.com'] }] } },
    }],
  }))
  await page.route(`**/api/apps/${app}/frames/linkPreviews/*`, (route) => route.fulfill({
    json: { frame, frameId: 'ffffffffffffffff', capability: 'ffffffffffffffff~linkPreviews~cards~cap', ttl: 900, document },
  }))
  // The frame document by URL: only a frame that navigates itself ever asks for it.
  await page.route(`**${frame}`, (route) => {
    if (proxy && fromCardFrame(page, route.request())) return route.fulfill(LOGIN_PAGE)
    return route.fulfill({ status: 404, body: '' })
  })
  await page.route(`**/apps/${app}/ui/lp.mjs`, (route) => {
    if (proxy && fromCardFrame(page, route.request())) return route.fulfill(LOGIN_PAGE)
    return route.fulfill({
      status: 200,
      contentType: 'application/javascript',
      headers: { 'Access-Control-Allow-Origin': 'null', Vary: 'Origin' },
      body: moduleSource,
    })
  })
}

async function hoverProbeLink(page: Page) {
  await page.goto('/')
  await page.evaluate((href) => {
    const scope = document.createElement('div')
    scope.setAttribute('data-link-preview-scope', '')
    document.body.append(scope)
    const a = document.createElement('a')
    a.href = href
    a.textContent = 'probe link'
    a.id = 'lp-probe-link'
    Object.assign(a.style, { position: 'fixed', left: '200px', top: '200px', zIndex: '2147483646' })
    scope.append(a)
  }, PROBE_LINK)
  await page.locator('#lp-probe-link').hover()
}

test('a card that registers renders behind a proxy that refuses the frame its own requests', async ({ page }) => {
  const app = 'lp-register-probe'
  await setUp(page, { app, fixture: 'lp-register-app', proxy: true })
  await hoverProbeLink(page)

  const frame = page.locator(`iframe[data-link-preview-frame="${app}:cards"]`)
  await expect(frame).toHaveCount(1, { timeout: 10_000 })
  // Same boundary as ever: scripts only, never the dashboard's origin.
  await expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  await expect(frame).toHaveAttribute('credentialless', '')
  await expect(frame).toHaveCSS('visibility', 'visible', { timeout: 12_000 })
  const card = page.frameLocator(`iframe[data-link-preview-frame="${app}:cards"]`).locator('#probe-card')
  await expect(card).toHaveText(`Probe card: ${PROBE_LINK}`)
  // Still an opaque origin.
  const origin = await page.frames().find((f) => f !== page.mainFrame())?.evaluate(() => self.origin)
  expect(origin).toBe('null')
  await expect(page.getByText("Couldn't load this preview")).toHaveCount(0)
})

test('a card that only exports activate still renders on the direct path', async ({ page }) => {
  const app = 'lp-activate-probe'
  await setUp(page, { app, fixture: 'lp-activate-app', proxy: false })
  await hoverProbeLink(page)
  const frame = page.locator(`iframe[data-link-preview-frame="${app}:cards"]`)
  await expect(frame).toHaveCSS('visibility', 'visible', { timeout: 12_000 })
  const card = page.frameLocator(`iframe[data-link-preview-frame="${app}:cards"]`).locator('#probe-card')
  await expect(card).toHaveText(`Activate-only card: ${PROBE_LINK}`)
})
