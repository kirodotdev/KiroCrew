/**
 * Capture the Kiro sign-in chooser (the embedded card Settings → Agent Harness
 * renders, and the full-screen gate) for the organization (external IdP)
 * sign-in: the fifth choice, its inline form, the IAM Identity Center form
 * beside it, and the browser-wait screen without the "Use a code instead" path.
 *
 * Usage: node scripts/capture-24639-organization-sign-in.mjs [base-url] [out-dir] [shot-name]
 * Needs a Vite dev server serving website/ on a loopback address (the choice is
 * offered only when the dashboard itself is served from loopback), e.g.
 * `npx vite --host 127.0.0.1 --port 5199`. Every /api/ call is answered from
 * the fixtures below; no gateway is involved.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const out = process.argv[3] || '../.github/screenshots/pr-24639'
mkdirSync(out, { recursive: true })

const json = (route, body, code = 200) => route.fulfill({
  status: code, contentType: 'application/json', body: JSON.stringify(body),
})

const signedOut = {
  authenticated: false, provider: '', identity: '', transport: 'loopback',
  expires_at: null, expired: false, has_refresh_token: false, refresh_rejected: false, usable: false,
}

const shots = [
  { name: 'card-chooser-light', theme: 'light', chrome: 'card' },
  { name: 'card-chooser-dark', theme: 'dark', chrome: 'card' },
  { name: 'card-sso-form-light', theme: 'light', chrome: 'card', sso: true },
  { name: 'card-sso-form-dark', theme: 'dark', chrome: 'card', sso: true },
  { name: 'card-organization-form-light', theme: 'light', chrome: 'card', open: true },
  { name: 'card-organization-form-dark', theme: 'dark', chrome: 'card', open: true },
  { name: 'card-organization-waiting-light', theme: 'light', chrome: 'card', open: true, submit: true },
  { name: 'card-organization-waiting-dark', theme: 'dark', chrome: 'card', open: true, submit: true },
  { name: 'gate-chooser-light', theme: 'light', chrome: 'gate' },
  { name: 'gate-chooser-dark', theme: 'dark', chrome: 'gate' },
  { name: 'account-modal-light', theme: 'light', chrome: 'account', ready: 'Signed in with' },
  { name: 'account-modal-dark', theme: 'dark', chrome: 'account', ready: 'Signed in with' },
]

const browser = await chromium.launch()
try {
  for (const s of shots) {
    if (process.argv[4] && process.argv[4] !== s.name) continue
    const page = await browser.newPage({ viewport: { width: 1360, height: 1000 }, deviceScaleFactor: 1.4 })
    await page.addInitScript(() => {
      window.open = () => null
    })
    const errors = []
    page.on('pageerror', error => errors.push(String(error)))
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 300)) })
    await page.route(url => new URL(url).pathname.startsWith('/api/'), route => {
      const p = new URL(route.request().url()).pathname
      if (p === '/api/kas-login') return json(route, signedOut)
      if (p === '/api/kas-login/loopback') {
        return json(route, {
          login_id: 'lb-1', user_code: '', expires_at: '2099-01-01T00:00:00Z',
          verification_uri_complete: 'https://app.kiro.dev/signin?state=s1',
          auth_url: 'https://app.kiro.dev/signin?state=s1', port: 3128,
        })
      }
      if (p === '/api/kas-login/poll') return json(route, { status: 'pending' })
      return json(route, {})
    })
    await page.goto(`${base}/capture/kas-login-organization.html?theme=${s.theme}&chrome=${s.chrome}`, { timeout: 240000, waitUntil: 'domcontentloaded' })
    try {
      const ready = s.ready
        ? page.getByText(s.ready, { exact: false }).first()
        : page.getByRole('button', { name: 'Continue with Google' })
      await ready.waitFor({ timeout: 120000 })
    } catch (e) {
      await page.screenshot({ path: `${out}/debug-${s.name}.png` })
      console.log('page errors:', errors.join(' | '))
      throw e
    }
    const org = page.getByRole('button', { name: 'Continue with Okta, Entra ID or another identity provider' })
    if (s.sso) {
      await page.getByRole('button', { name: 'Continue with AWS IAM Identity Center' }).click()
      await page.getByTestId('kas-login-sso-form').waitFor()
    }
    if (s.open) {
      await org.click()
      await page.getByTestId('kas-login-org-form').waitFor()
      await page.getByLabel('Work email domain or Kiro start URL (optional)').fill('example.com')
    }
    if (s.submit) {
      await page.getByTestId('kas-login-org-form').getByRole('button', { name: 'Continue' }).click()
      await page.getByRole('heading', { name: 'Waiting for your browser' }).waitFor()
    }
    await page.waitForTimeout(800)
    await page.screenshot({ path: `${out}/${s.name}.png`, animations: 'disabled' })
    console.log(`${s.name}: captured${errors.length ? ` (page errors: ${errors.join(' | ')})` : ''}`)
    await page.close()
  }
} finally {
  await browser.close()
}
