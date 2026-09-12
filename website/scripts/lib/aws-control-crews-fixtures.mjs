/**
 * Shared fixtures and route table for the AWS Control remote-crews harnesses.
 *
 * Two harnesses drive the same pane -- `capture-aws-control-crews.mjs` takes stills and
 * measures layout, `record-aws-control-crews.mjs` records a clip -- and they must answer
 * the API identically or the two pieces of evidence describe different software. The
 * recorder started life with this block copied verbatim, which the duplication gate
 * (`jscpd`) reported as an 18-line, 502-token clone. Extracting it is the same remedy
 * the BOOT table below already documents for its own sibling clone: keep the payload,
 * have one copy of the control flow.
 *
 * `createFixtureRouter()` returns the route handler together with the `setMode` that
 * drives it, so the phase state is owned here rather than living as a mutable
 * module-level `let` in each caller.
 */

// ---- fixtures -------------------------------------------------------------
export const ACCOUNTS = {
  supported: true,
  accounts: [
    {
      account: '111122223333', name: 'personal', health: 'ok',
      profiles: [{ name: 'personal', kind: 'credential-process', region: 'us-west-2', account: '111122223333', default: true, identityOk: true }],
    },
  ],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
}

export const DRIVE = {
  exists: true, bucket: 'kirocrew-drive-7f3a91c4', region: 'us-west-2',
  usage: {
    bytes: 44677427, objects: 18,
    sections: {
      drive: { objects: 4, bytes: 32715570 },
      library: { objects: 6, bytes: 11157402 },
      backup: { objects: 8, bytes: 804455 },
    },
  },
}

/**
 * One crew as the LIST route answers: service empty and both counts zero. The
 * fixture keeps that honest on purpose - a fixture that filled the counts in
 * would hide the very thing the card must not render.
 *
 * The stack name is DERIVED from the crew name, the way `crews.py` parses it back
 * out of `smc-crew-<name>`. An earlier version hardcoded one stack for all four,
 * which made the Stack cell look like a constant and hid whether it was readable.
 *
 * The image is digest-pinned with a full 64 hex characters, because the template's
 * `AllowedPattern` (`.+@sha256:[a-f0-9]{64}$`) refuses a tag. A short fake digest
 * would understate the length the card has to cope with.
 */
export const DIGEST = '9f1c2d3e4b5a67788990aabbccddeeff00112233445566778899aabbccddeeff'

export const listCrew = (name, over = {}) => ({
  name,
  stack: `smc-crew-${name}`,
  stackStatus: 'CREATE_COMPLETE',
  memory: 'chatbot',
  service: '',
  running: 0,
  desired: 0,
  image: `111122223333.dkr.ecr.us-west-2.amazonaws.com/smc@sha256:${DIGEST}`,
  controlBase: 'https://d1abcdefghij.cloudfront.net',
  region: 'us-west-2',
  ...over,
})

// Five crews chosen so the grid shows every case the pane has to get right in
// one frame: a settled one with no badge, one mid-update (warn), one mid-delete
// (err, listed on purpose), one whose stack predates the Memory parameter, and a
// near-maximum name.
//
// The ORDER is the backend's (sorted by name), and the names are picked so that
// alphabetical order puts a badge-carrying card beside a bare one in BOTH rows of
// the two-column grid. That is what makes the alignment measurement in the capture
// harness test the mixed case instead of passing vacuously on a uniform row.
export const CREWS = {
  account: '111122223333',
  region: 'us-west-2',
  baseMissing: false,
  crews: [
    listCrew('billing-help', { memory: 'persistent' }),
    listCrew('checkout-bot', { stackStatus: 'UPDATE_IN_PROGRESS' }),
    listCrew('legacy-triage', { memory: '', image: '', controlBase: '' }),
    listCrew('winter-promo', { stackStatus: 'DELETE_IN_PROGRESS', memory: 'persistent' }),
    // A near-maximum name (the backend's own pattern allows 32 characters), which
    // is what the clipping measurement is really aimed at: this is the crew whose
    // `smc-crew-<name>` stack did not fit while the cell was half a card wide, and
    // the card that proves it fits now.
    listCrew('regional-returns-desk-au-south', { memory: 'persistent' }),
  ],
}

export const DETAIL = listCrew('billing-help', {
  memory: 'persistent',
  service: 'smc-billing-help', running: 2, desired: 2,
})

export const BASE = '/api/apps/aws-control'

/**
 * The dashboard shell's own boot endpoints, as a TABLE rather than an if-chain.
 *
 * The shell mounts before the app page and consumes several of these as ARRAYS,
 * so a blanket `{}` crashes its error boundary ("x.filter is not a function") and
 * the app page never mounts at all. Every harness in this directory therefore has
 * to answer the same set with the same shapes.
 *
 * A table because of that: written as the chain of `if (path === ...)` lines its
 * siblings use, this block IS a 406-token clone of
 * `capture-aws-control-library-remove.mjs`, which the duplication gate reports.
 * The same remedy `capture-prose-diff-fold.mjs` used - keep the payload, drop the
 * repeated control flow - removes the clone without an exemption entry.
 */
export const BOOT = {
  '/api/apps': [],
  '/api/auth/me': { user: 'owner', app: '' },
  '/api/status': { sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, uptime: 1, version: '0.1.0' },
  '/api/kiro-prerequisite': { installed: true, authenticated: true, ready: true },
  '/api/dashboard/branding': { bot_name: 'Kiro Crew', avatar: '' },
  '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/themes': { themes: [], installed: [] },
  '/api/notifications': { notifications: [], unread: 0 },
  '/api/chat/slots': [],
  '/api/models': { models: [], default: 'auto' },
}

const json = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/**
 * Build the route handler plus the phase switch that drives it.
 *
 * Modes: `list` (the populated grid), `base` (no shared base stack), `empty` (base
 * ready, account holds none), `mismatch` (409 `account_mismatch`). They are three
 * DIFFERENT screens in the pane, which is why the harnesses photograph each one.
 */
/**
 * The page wiring both harnesses need before they drive anything: route the API at the
 * fixture router, refuse the websocket, surface page errors, and pre-set the localStorage
 * keys that keep onboarding out of the frame.
 *
 * Shared for the same reason the router is. With this written out in each harness the
 * two files still held a 9-line identical block, which is over `jscpd`'s 5-line floor,
 * so the duplication gate would have kept reporting them.
 */
/**
 * The phase switch both harnesses drive the page with: set the fixture mode, then do a
 * FULL navigation rather than an in-app route change, so the pane re-fetches and the new
 * mode is what it renders.
 *
 * Shared because the two copies were byte-identical at exactly 5 lines, which is
 * `jscpd`'s floor -- the last block that could still have been reported as a clone.
 */
export function makeReload(page, base, setMode) {
  return async (m, pane = 'crews') => {
    setMode(m)
    await page.goto(`${base}/aws-control/${pane}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1200)
  }
}

export async function preparePage(page, answer) {
  await page.route('**/api/**', answer)
  await page.route('**/api/ws', (route) => route.abort())
  page.on('pageerror', (err) => console.log('PAGEERROR:', (err.stack || String(err)).slice(0, 400)))
  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
    localStorage.setItem('mc-theme-mode', 'dark')
  })
}

export function createFixtureRouter() {
  let mode = 'list'

  const answer = async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/accounts')) return json(route, ACCOUNTS)
    if (path === '/api/aws/consent') {
      return json(route, { service: 's3', granted: true, region: 'us-west-2', account: '111122223333' })
    }
    const app = path.startsWith(BASE) ? path.slice(BASE.length) : ''
    if (/^\/crews\/[^/]+\/[^/]+$/.test(app)) return json(route, DETAIL)
    if (/^\/crews\/[^/]+$/.test(app)) {
      if (mode === 'base') return json(route, { ...CREWS, baseMissing: true, crews: [] })
      if (mode === 'empty') return json(route, { ...CREWS, crews: [] })
      if (mode === 'mismatch') {
        // `(unknown)` rather than a twelve-digit placeholder, for two reasons.
        // It is what the backend itself emits when `sts get-caller-identity`
        // answers nothing (`_assert_account` in backend/crews.py formats
        // `resolved or '(unknown)'`), so the fixture is the more faithful for it.
        // And any twelve-digit run here reads as a real AWS account id to the
        // repository's content scan, which is a finding this string cannot
        // justify: `api.ts` renders the machine-readable `code`, never this
        // prose, so the digits were never on screen and never asserted.
        return json(route, { error: 'profile resolves to account (unknown)', code: 'account_mismatch' }, 409)
      }
      return json(route, CREWS)
    }
    if (/^\/drive\/[^/]+$/.test(app)) return json(route, DRIVE)
    if (/^\/drive\/[^/]+\/list$/.test(app)) return json(route, { folders: [], files: [] })
    if (/^\/costs\/[^/]+$/.test(app)) return json(route, { monthToDate: 2.25, currency: 'USD', fresh: true, fetchedAt: new Date().toISOString(), byService: [] })
    if (app === '/profiles/available') return json(route, { supported: true, profiles: [], max: 20, registeredCount: 1 })
    if (/^\/library\/[^/]+$/.test(app)) return json(route, { artifacts: [] })
    if (/^\/backup\/[^/]+$/.test(app)) return json(route, { nightly: false, runs: {}, remote: null, jobs: {} })
    if (app.startsWith('/shares')) return json(route, { shares: [] })
    if (path in BOOT) return json(route, BOOT[path])
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    // Unknown paths: object-ish names get {}, everything else an array, because a
    // list endpoint answered with an object is what crashes the shell.
    const objectish = /(config|tips|voice|autonudge|branding|status|themes|system)/.test(path)
    return json(route, objectish ? {} : [])
  }

  return { answer, setMode: (m) => { mode = m } }
}
