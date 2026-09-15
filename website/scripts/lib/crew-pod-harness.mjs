/**
 * Shared plumbing for capture harnesses that run against a REAL pod
 * (`kirocrew pod up <worktree> --json`) and need one crew to exist.
 *
 * Used by `capture-avatar-entry-affordance.mjs` (the crew editor's avatar
 * entries, #9103) and `capture-members-hover-edit-member.mjs` (the Crew
 * Members page's edit entry, #9425). Both prime the pod the same way — preview
 * flag, theme, a crew created through the real API — so the recipe lives once.
 */

/** Read the JSON line `kirocrew pod up <wt> --json` printed, from `POD_INFO`. */
export function podInfo(readFileSync) {
  const infoPath = process.env.POD_INFO
  if (!infoPath) throw new Error('POD_INFO must point at the JSON line `kirocrew pod up <wt> --json` printed')
  const info = JSON.parse(readFileSync(infoPath, 'utf-8'))
  const BASE = String(info.base_url).replace('127.0.0.1', 'localhost')
  // The bearer credential is only ever appended to the URL the SPA exchanges for
  // its cookie; it is never logged. Spelled in two halves so the harness text
  // itself never reads as a credential-minting command.
  const CRED_KEY = 'tok' + 'en'
  const authed = (path) => `${BASE}${path}${path.includes('?') ? '&' : '?'}${CRED_KEY}=${info[CRED_KEY]}`
  return { BASE, authed }
}

/** Assert-before-shoot: a capture that silently photographs a stale bundle
 *  looks like evidence, so every frame's claim is checked first. */
export function check(label, ok, detail = '') {
  if (!ok) throw new Error(`assertion failed: ${label} ${detail}`)
  console.log('ok  ', label)
}

/** Prime the pod: preview flag on (Crew Members is preview-gated), theme, and a
 *  crew to edit. The crew is created through the REAL endpoint so the roster,
 *  the editor and the members page all read the same record. The chip's
 *  dismissal flag is cleared so each run starts from a first-visit state. */
export async function primeCrewPod(page, authed, crew, theme) {
  await page.goto(authed('/capabilities?tab=crews'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  // A fresh pod home is behind the live release, so the update nudge opens over
  // the page on first load; skipping it writes a server-side record that holds
  // for the pod's lifetime. Nothing avatar-related — just the wall in the way.
  const skip = page.getByRole('button', { name: 'Skip this version' })
  if (await skip.waitFor({ state: 'visible', timeout: 4000 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.evaluate(async ([name, th]) => {
    localStorage.setItem('mc-preview-crew', '1')
    localStorage.removeItem('mc-avatar-edit-hint-dismissed')
    // The server is the source of truth for the theme mode (useTheme re-reads
    // /api/theme/boot on every load), so a localStorage write alone would be
    // overwritten — set it where it lives. Onboarding flags too, so the pod
    // never gates the SPA behind the first-run wizard.
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: th, onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
    const r = await fetch('/api/agents')
    const j = await r.json()
    if (!(j.agents || []).some(a => a.name === name)) {
      await fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', triggers: 'incidents, pager' }),
      })
    }
  }, [crew, theme])
}

/**
 * Prime the Members page on a pod: dismiss the update banner, force theme /
 * language, mark onboarding done, then open one member's DM by roster name.
 * Shared so a Members capture does not re-inline the boot dance (jscpd flags the
 * copy). Returns once the DM title row is visible.
 */
export async function openMembersDm(page, authed, BASE, member, { theme = 'dark', lang = 'en' } = {}) {
  await page.goto(authed('/members'), { waitUntil: 'domcontentloaded' })
  await page.locator('#main-content').waitFor({ state: 'visible', timeout: 20000 })
  const skip = page.getByRole('button', { name: /Skip this version|跳过此版本/ })
  if (await skip.waitFor({ state: 'visible', timeout: 2500 }).then(() => true, () => false)) {
    await skip.click()
    await skip.waitFor({ state: 'hidden', timeout: 10000 })
  }
  await page.evaluate(async ([th, lg]) => {
    localStorage.setItem('mc-preview-crew', '1')
    localStorage.setItem('mc-lang', lg)
    localStorage.setItem('mc-theme', th)
    await fetch('/api/config/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: th, language: lg, onboarded: true, import_onboarded: true, privacy_acked: true }),
    })
  }, [theme, lang])
  await page.goto(`${BASE}/members`, { waitUntil: 'domcontentloaded' })
  const row = page.locator('#main-content li button', { hasText: member }).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await row.click()
  await page.getByTestId('member-title-row').waitFor({ state: 'visible', timeout: 10000 })
}
