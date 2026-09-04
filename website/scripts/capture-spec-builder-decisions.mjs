/**
 * Screenshot harness for Spec Builder's DECISION TRAY (SpecStatePanel) and the
 * post-approval button state.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered from fixtures — no gateway, no kiro-cli, no agent.
 * Only the network is stubbed, so the tray, its sticky question headers and the
 * approval control are exercised exactly as they run in production.
 *
 * Captures:
 *   01-decision-tray-dark    the tray at rest: bordered, padded, off the edges
 *   02-decision-tray-mid     scrolled: the question stays with its options
 *   03-decision-sending      an answered option before the agent records it
 *   04-drafting-design       the approval control after Approve → Design
 *   05-decision-tray-light   the tray in the light palette
 *
 * Usage: node scripts/capture-spec-builder-decisions.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/spec-builder-decisions'
mkdirSync(OUT, { recursive: true })

const NAME = 'codex-acp-oauth'
const SPEC_DIR = '/proj/KiroCrew/.kiro/specs/' + NAME

const REQUIREMENTS = `# Requirements — pluggable ACP backend

## Glossary

- **ACP**: Agent Client Protocol — the JSON-RPC 2.0 over stdio protocol Kiro Crew
  speaks to an agent subprocess.
- **Gateway**: the long-running Kiro Crew process that owns sessions, config,
  approvals, and the dashboard.
- **LLMProvider**: the internal provider ABC. \`agent.provider\` selects it and
  stays fixed at \`acp\`; this feature does not add a second LLMProvider.
- **ACP_Backend**: which ACP agent subprocess the \`acp\` provider launches and
  which protocol dialect it speaks.
- **Backend_Descriptor**: a declarative in-core record describing one ACP_Backend.
- **Backend_Registry**: the ordered collection of Backend_Descriptors the build
  ships. Adding a backend is a data change to this collection.
`

/** Two open decisions and a blocking note — the state the tray exists for. */
const STATE = {
  decisions: [
    {
      id: 'gate',
      title: 'Amend AGENTS.md now, or keep the policy question upstream?',
      options: [
        'Amend it to permit an experimental backend registry',
        'Leave it and keep this work fork-only',
        'Amend it only after upstream answers PR #2107',
      ],
      recommended: 'Amend it to permit an experimental backend registry',
    },
    {
      id: 'granularity',
      title: 'Backend selection granularity in v1',
      options: ['Gateway-global config field only', 'Per-session override too', 'Per-agent binding too'],
      recommended: 'Gateway-global config field only',
    },
  ],
  blocking: 'Requirements draft is written and awaiting your review; the gate-posture decision is the one that would change the document materially.',
  context: { template: 'docs/system-specs/modules/providers.md + acp-client.md' },
}

const detail = {
  name: NAME,
  phase: 'requirements',
  status: 'planning',
  running: false,
  working_dir: '/proj/KiroCrew',
  spec_dir: SPEC_DIR,
  spec_type: 'feature',
  slot_key: 'spec-builder-' + NAME + '-1',
  files: { 'requirements.md': REQUIREMENTS },
  state: STATE,
  context: { turns: 1, tool_calls: 17, worktree_branch: 'feat/codex-acp-oauth' },
}

const DOCS_COLUMN = 'section:has([data-testid="doc-view"]), .sb-doc'

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  await page.addInitScript((name) => {
    try {
      localStorage.setItem('kc.onboarded', '1')
      localStorage.setItem('kc.changelogSeen', '9999')
      localStorage.setItem('spec-builder:last-open', name)
    } catch { /* private mode */ }
  }, NAME)

  // Answers dispatched instructions without moving the phase on: the point of
  // these shots is the window BEFORE the agent has written the next document.
  // Each branch returns `true` explicitly — `json()` resolves to undefined, and
  // returning it lets the shared map handle the route a second time ("Route is
  // already handled!").
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/apps') {
        await json(route, {
          apps: [{ name: 'spec-builder', enabled: true, installed: true, version: '0.1.0', builtin: true }],
          serverPlatform: { os: 'linux', arch: 'x64' },
        })
        return true
      }
      if (path.endsWith('/apps/spec-builder/specs')) {
        await json(route, { specs: [{ name: NAME, phase: 'requirements', status: 'planning', running: false }] })
        return true
      }
      if (path.endsWith('/apps/spec-builder/specs/' + NAME)) { await json(route, detail); return true }
      if (path.endsWith('/messages')) { await json(route, { messages: [] }); return true }
      if (path.includes('/apps/spec-builder/')) { await json(route, { ok: true }); return true }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(base + '/spec-builder', { waitUntil: 'domcontentloaded' })
  if (process.env.SB_DEBUG) {
    await page.waitForTimeout(4000)
    await page.screenshot({ path: `${OUT}/00-debug.png` })
    console.log((await page.locator('body').innerText()).slice(0, 1200))
  }
  const openSpec = async () => {
    await page.getByRole('button', { name: new RegExp(NAME) }).first().click()
    await page.getByText('DECISIONS').first().waitFor({ timeout: 20_000 })
  }
  await openSpec()

  const shot = async (name) => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  await shot('01-decision-tray-dark')

  // Scrolled halfway: the second question must still name itself above its options.
  await page.evaluate(() => {
    const scroller = [...document.querySelectorAll('div.overflow-y-auto')]
      .find((el) => el.textContent?.includes('DECISIONS') && el.textContent?.includes('BLOCKING'))
    if (scroller) scroller.scrollTop = 260
  })
  await page.waitForTimeout(400)
  await shot('02-decision-tray-mid')

  await page.reload({ waitUntil: 'domcontentloaded' })
  await openSpec()
  await page.getByRole('button', { name: /be answered\?Gateway-global config field only/ }).click()
  await page.getByText('sending…', { exact: true }).waitFor({ timeout: 5_000 })
  await shot('03-decision-sending')

  await page.getByRole('button', { name: /Approve → Design/ }).click()
  await page.getByRole('button', { name: /Drafting design/ }).waitFor({ timeout: 5_000 })
  await shot('04-drafting-design')

  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'))
  await page.waitForTimeout(400)
  await shot('05-decision-tray-light')

  await browser.close()
  srv.close()
}

main().catch((e) => { console.error(e); process.exit(1) })
