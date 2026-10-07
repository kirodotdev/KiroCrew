/**
 * Screenshot harness for the credit segment on a KAS-mode install, before and
 * after reading the balance with Crew's own Kiro credential.
 *
 * This PR changes no client code (`git diff origin/main..HEAD -- website/` is
 * empty apart from this script and the usage-footnote copy), so the two frames
 * are driven by the same built SPA and differ in what `/api/sessions/usage`
 * answers. That is the delta the change produces, which is why a stub is the
 * right instrument here: it isolates the payload, and the backend that now
 * produces it is covered by `test/test_usage_crew_credential.py`.
 *
 *   node scripts/capture-usage-crew-oidc.mjs <distDir> <outDir>
 *
 * The host is pinned to the shape that had the defect:
 *   - `agent.acp_backend: 'kas'` — kiro-cli is not the agent runtime, so its
 *     readiness latch is never verified-ready. This is the install whose users
 *     have NOT been migrated to Crew's own OIDC in any sense that matters here:
 *     kiro-cli may not even be present.
 *   - `mc-lang: zh-CN` — the locale the report came in on.
 *
 * Scenarios, in the order the user lived them:
 *   before  `/api/sessions/usage` answers 503 `kiro_prerequisite_required`,
 *           which is what the readiness gate returned on EVERY 30s poll. The
 *           query errors with nothing cached, so the segment renders its
 *           terminal dash and the modal behind it says the balance could not be
 *           read -- about a balance it never asked anyone for.
 *   after   the endpoint serves the reading Crew's own credential produces.
 *
 * The `after` payload is the REAL field set, which matters because an earlier
 * revision of this harness omitted the identity fields and the resulting frame
 * was misread as the code publishing an anonymous number:
 *   - `account` — the profile display name. `fetch_usage_limits` attaches it
 *     from the same ListAvailableProfiles probe that proves the ARN, on this
 *     path exactly as on the kiro-cli one.
 *   - `account_type` — the stored kind of the sign-in Crew itself performed,
 *     spelled the way `whoami` spells it so the panel reads one vocabulary.
 *   - `email` — a member of the GetUsageLimits response, which the request now
 *     asks for (`isEmailRequired`). Same place kiro-cli's own whoami gets it.
 *   - `start_url` — the directory the sign-in went to, now kept on Crew's own
 *     credential the way kiro-cli keeps it on its own. The panel pairs its host
 *     with the kind, so the account line names WHICH organization. A sign-in
 *     stored before that field existed has none and shows the kind alone; no
 *     API can backfill it.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join, resolve as resolvePath } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import {
  KIROCREW_CONFIG_FIXTURE,
  logPageProblems,
  stubDashboardApi,
  json,
} from './lib/stub-dashboard-api.mjs'

const DIST = resolvePath(process.argv[2] || 'dist')
const OUT = resolvePath(process.argv[3] || '/tmp/usage-crew-oidc-shots')
mkdirSync(OUT, { recursive: true })

/** What the vault-anchored read publishes: the numbers, plus what Crew knows. */
const USAGE_FROM_CREW_CREDENTIAL = {
  usage: {
    credits_used: 3044,
    credits_plan: 10000,
    credits_overage: 0,
    resets: '2026-11-01',
    plan: 'KIRO POWER',
    cost_usd: 0,
    overage_rate: 0.04,
    account: 'Engineering',
    account_type: 'IamIdentityCenter',
    email: 'dev@example.com',
    start_url: 'https://d-906679cc0e.awsapps.com/start',
  },
}

/** The gate's own refusal body, verbatim from `kiro_readiness`. */
const READINESS_REFUSAL = {
  error: 'Kiro CLI setup or sign-in is required before starting a session.',
  code: 'kiro_prerequisite_required',
}

/** kiro-cli is not the agent runtime here -- the install the defect lived on. */
const KAS_CONFIG = {
  ...KIROCREW_CONFIG_FIXTURE,
  agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' },
}

const SLOT = 'usage-crew-oidc'
const slots = [{
  key: SLOT,
  title: '额度读数',
  running: false,
  last_message: '看一眼顶栏。',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: '~/.kiro/crew/workspace',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()

for (const scenario of ['before', 'after']) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
    locale: 'zh-CN',
  })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots,
    localStorageEntries: { 'mc-lang': 'zh-CN', 'mc-active-slot': SLOT },
    extra: async (path, route) => {
      if (path === '/api/kirocrew/config') {
        await json(route, KAS_CONFIG)
        return true
      }
      if (path === '/api/sessions/usage') {
        if (scenario === 'before') {
          await json(route, READINESS_REFUSAL, 503)
          return true
        }
        await json(route, USAGE_FROM_CREW_CREDENTIAL)
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] })
        return true
      }
      return false
    },
  })

  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  const capsule = page.locator('.tb-capsule').first()
  await capsule.waitFor({ state: 'visible', timeout: 15_000 })
  // Let the query settle -- or visibly fail to -- before shooting.
  await page.waitForTimeout(2_000)

  const credit = capsule.locator('button').last()
  console.log(
    `${scenario}: aria-label=${JSON.stringify(await credit.getAttribute('aria-label'))}`
    + ` text=${JSON.stringify((await credit.innerText()).trim())}`,
  )
  await capsule.screenshot({ path: join(OUT, `${scenario}-capsule.png`) })

  // The drill-in carries the sentence the user reported, so it is part of the
  // claim: a segment and the panel it opens must not describe different worlds.
  await credit.click()
  const dialog = page.locator('[role="dialog"]').first()
  await dialog.waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(500)
  // Printed, not just shot: the rendered sentence is the machine-checkable part
  // of the claim, and it is what a reviewer greps this log for.
  console.log(`${scenario}: modal=${JSON.stringify((await dialog.innerText()).replace(/\s+/g, ' ').trim())}`)
  await dialog.screenshot({ path: join(OUT, `${scenario}-modal.png`) })
  await ctx.close()
}

await browser.close()
srv.close()
console.log(`shots in ${OUT}`)
