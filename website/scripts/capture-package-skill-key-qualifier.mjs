/**
 * Screenshot harness + behavior check for the PACKAGE SKILL KEY QUALIFIER.
 *
 * Runs the REAL built SPA (website/dist) behind an in-process static server and
 * answers every /api/** call from fixtures via Playwright route interception. The
 * client code is unmodified — only the network is stubbed — so the Agent Templates
 * skill picker, its chips and the PATCH round-trip are exercised as they run in
 * production.
 *
 * What the qualifier changes that a user can SEE. Two package bundles vendoring a
 * skill at the same relative path used to collapse into ONE catalog row: the
 * second copy was unaddressable, which is the reported defect (skills missing from
 * the picker on an install with colliding bundles). Each colliding copy now gets
 * its own `package/<digest>:<rel>` key.
 *
 * READ THIS BEFORE TRUSTING THE SHOTS. Those keys are minted by
 * `enumerate_skill_catalog`, which feeds the tree, detail and agent-mapping
 * surfaces, and `GET /api/skills` -- what this picker reads -- re-keys a colliding
 * package row onto its qualified spelling, so such a copy DOES reach the picker
 * under its own key.
 * The catalog below is a stub; it is shaped for the surfaces that receive
 * qualified keys, which includes the mapped chip and the rejected-write path.
 *
 * Scene 2 covers the chip: a mapped qualified key renders its readable half plus
 * the first 8 hex, which is the surface that has to say WHICH copy is mapped.
 *
 * Scene 3 covers the other half: the editor now re-enumerates the catalog when a
 * write is REJECTED. A key carries its root's identity, so a bundle upgrade under a
 * live editor re-spells it and the backend refuses the whole request; the cached
 * catalog is then the stale one that minted the refused key, so without the
 * invalidation a retry re-sends exactly the key that was just rejected.
 *
 * This asserts as well as photographs and exits non-zero on a mismatch. Nothing in
 * CI runs this file — the CI-enforced half is AgentSkillsEditor.test.tsx and
 * test/test_resolve_skill_root_package.py.
 *
 * Usage: node scripts/capture-package-skill-key-qualifier.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/package-skill-key-qualifier'
const DIST = fileURLToPath(new URL('../dist/', import.meta.url))

mkdirSync(OUT, { recursive: true })

// Read from the catalogue the UI renders rather than restated here, so this harness
// cannot drift out of agreement with the copy again.
const REPICK_NOTICE = JSON.parse(
  readFileSync(new URL('../src/i18n/locales/en.manual.json', import.meta.url), 'utf8'),
).components.agentSkillsEditor.key_changed_repick

// Two 16-byte identity digests, spelled as the backend spells them. They stand for
// two bundles that vendor `shared-skill` at the same relative path.
const Q_A = '05c564ec5e9e4b7a8c1d2e3f4a5b6c7d'
const Q_B = 'b81f3a9042d75c6e1a4b8d2f6c0e9a37'

const CATALOG = [
  {
    key: `package/${Q_A}:shared-skill`,
    name: 'shared-skill',
    description: 'From the eventId-1 bundle — reconcile a shipment against its manifest',
    path: '/opt/edition/packages/PkgA/eventId-1/skills/shared-skill/SKILL.md',
    source: 'package',
  },
  {
    key: `package/${Q_B}:shared-skill`,
    name: 'shared-skill',
    description: 'From the eventId-2 bundle — reconcile a shipment against its manifest',
    path: '/opt/edition/packages/PkgB/eventId-2/skills/shared-skill/SKILL.md',
    source: 'package',
  },
  {
    key: 'package/only-a',
    name: 'only-a',
    description: 'Vendored by one bundle only, so its key stays unqualified',
    path: '/opt/edition/packages/PkgA/eventId-1/skills/only-a/SKILL.md',
    source: 'package',
  },
  {
    key: 'babysit',
    name: 'babysit',
    description: 'Same-session monitoring loop for PRs and CI runs',
    path: '/home/user/.kiro/crew/skills/babysit/SKILL.md',
    source: 'kirocrew',
  },
]

/** Mutated by the stubbed PATCH so an after-shot renders real state. */
const mapping = { 'release-captain': [] }

const AGENTS = [{
  name: 'release-captain',
  description: 'Cuts releases and babysits the pipeline',
  source: 'builtin',
  model: 'auto',
  mcp_servers: [],
  filename: 'release-captain.json',
}]

const DETAIL = {
  'release-captain': {
    name: 'release-captain',
    description: 'Cuts releases and babysits the pipeline',
    model: 'auto',
    tools: ['fs_read', 'execute_bash'],
  },
}

function installed() {
  return AGENTS.map(a => ({ ...a, skills: (mapping[a.name] || []).map(k => k.split(':').pop()) }))
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // Chips and option labels are 11-13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()

  const failures = []
  // Counted so scene 3 can prove the catalog was RE-FETCHED, not merely that an
  // error rendered. A rejection that leaves the count at 1 is the pre-fix defect.
  let skillsCalls = 0
  let rejectPatch = false

  const extra = async (path, route) => {
    const method = route.request().method()
    if (path.startsWith('/api/agents/detail/')) {
      const name = decodeURIComponent(path.split('/').pop())
      if (method === 'PATCH') {
        if (rejectPatch) {
          // The production shape: the editor branches on `code`, so a prose-only body
          // renders no notice and this scene captures an unchanged page.
          await route.fulfill({
            status: 400,
            contentType: 'application/json',
            body: JSON.stringify({
              error: 'unknown skills',
              skills: [`package/${Q_A}:shared-skill`],
              code: 'skills_unknown',
            }),
          })
          return true
        }
        const body = JSON.parse(route.request().postData() || '{}')
        if (Array.isArray(body.skills)) mapping[name] = body.skills
        await json(route, { ok: true, model: DETAIL[name]?.model || '', skills: mapping[name] })
        return true
      }
      await json(route, { ...(DETAIL[name] || { name }), skills: mapping[name] || [], unmanaged_skills: [] })
      return true
    }
    if (path === '/api/agents/installed') { await json(route, installed()); return true }
    if (path === '/api/skills') { skillsCalls += 1; await json(route, CATALOG); return true }
    if (path === '/api/config/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
    if (path.startsWith('/api/agent-metadata/')) { await json(route, { content: '' }); return true }
    if (path === '/api/mcp/probe') { await json(route, []); return true }
    if (path === '/api/spawn') { await json(route, { agents: [] }); return true }
    if (path === '/api/sessions/context') { await json(route, { sessions: [] }); return true }
    if (path === '/api/sessions/usage') { await json(route, { usage: null }); return true }
    if (path === '/api/models') { await json(route, [{ model_name: 'auto', description: 'Let Kiro choose' }]); return true }
    return false
  }

  logPageProblems(page)
  await stubDashboardApi(page, { extra })
  await page.goto(base + '/capabilities?tab=templates', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // The editor lives behind the agent detail's own Skills TAB — the detail panel
  // gained that tab strip after the sibling capture harnesses were written.
  const skillsTab = page.getByRole('tab', { name: /^Skills$/ })
  await skillsTab.first().waitFor({ timeout: 15000 })
  await skillsTab.first().click()
  await page.waitForTimeout(1200)

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /* ── Scene 1: the picker opens; colliding copies are distinguishable ── */
  await page.getByRole('button', { name: /add skill/i }).click()
  await page.waitForTimeout(600)
  // The invariant is DISTINCTNESS, not the absence of a digest: `/api/skills` re-keys a
  // colliding row, so the picker does render a disambiguator and must render two different
  // ones -- a shared one would let a pick land on the copy nobody chose.
  const twinRows = await page
    .getByRole('option')
    .filter({ hasText: 'shared-skill' })
    .allTextContents()
  if (twinRows.length !== 2) {
    failures.push(`scene 1: expected 2 colliding rows, saw ${twinRows.length}`)
  } else if (new Set(twinRows.map(t => t.trim())).size !== 2) {
    failures.push(`scene 1: both colliding rows read the same: ${JSON.stringify(twinRows)}`)
  }
  if (await page.getByRole('option').filter({ hasText: 'only-a' }).count() !== 1) {
    failures.push('scene 1: the uncollided package skill is missing from the picker')
  }
  await shot('1-picker-distinguishes-colliding-copies')

  /* ── Scene 2: selecting one maps it, so the qualified key resolves ── */
  await page.getByRole('option').filter({ hasText: 'shared-skill' }).first().click()
  await page.waitForTimeout(1000)
  // Matched loosely rather than by exact text: a chip for a colliding copy carries its
  // qualifier alongside the name, so its textContent is not the bare name.
  if (await page.getByText(/shared-skill/).count() < 1) {
    failures.push('scene 2: the selected colliding copy did not render as a chip')
  }
  await shot('2-qualified-key-maps-and-resolves')

  /* ── Scene 3: a REJECTED write re-enumerates rather than re-sending ── */
  rejectPatch = true
  const before = skillsCalls
  await page.getByRole('button', { name: /add skill/i }).click()
  await page.waitForTimeout(600)
  await page.getByRole('option').filter({ hasText: 'shared-skill' }).first().click()
  await page.waitForTimeout(1800)
  const notice = page.getByRole('alert').filter({ hasText: REPICK_NOTICE })
  if ((await notice.count()) < 1) {
    failures.push('scene 3: the rejection was not surfaced to the user')
  } else {
    // Equality, not the substring the locator filtered on: the notice must say only what
    // the UI ships, so raw backend detail appearing alongside it is a failure.
    const shown = ((await notice.first().textContent()) ?? '').trim()
    if (shown !== REPICK_NOTICE) {
      failures.push(`scene 3: the notice read ${JSON.stringify(shown)}`)
    }
  }
  if (skillsCalls <= before) {
    failures.push(`scene 3: catalog NOT re-enumerated after the rejection (calls stayed at ${before})`)
  }
  await shot('3-rejected-write-re-enumerates')

  /* ── Scene 4: the CHIP under hover, with the tooltip text it carries ── */
  // Appended rather than folded into the scenes above: the existing three are the
  // reviewed evidence layout and stay as they are.
  //
  // A native `title` tooltip is painted by BROWSER CHROME, not by the page, so
  // `page.screenshot` cannot contain it -- captured here is the real hover STATE (the
  // chip's group-hover reveals its remove control), and the tooltip's TEXT is asserted
  // instead, which is the part a picture could not prove anyway.
  const chip = page.getByText(/shared-skill/).first()
  await chip.hover()
  await page.waitForTimeout(500)
  const chipTitle = await chip.evaluate(el => el.closest('[title]')?.getAttribute('title') ?? '')
  if (!chipTitle.includes('package/')) {
    failures.push(`scene 4: the chip tooltip does not carry the precise key: ${chipTitle}`)
  }
  if (/^[0-9a-f]{8}\n/.test(chipTitle)) {
    failures.push('scene 4: the chip tooltip leads with hex rather than a readable label')
  }
  await shot('4-chip-hover-and-tooltip-text')

  /* ── Scene 5: a PICKER ROW under hover, carrying its absolute path ── */
  await page.getByRole('button', { name: /add skill/i }).click()
  await page.waitForTimeout(600)
  const row = page.getByRole('option').filter({ hasText: 'shared-skill' }).first()
  await row.hover()
  await page.waitForTimeout(500)
  const rowTitle = await row.evaluate(el => el.closest('[title]')?.getAttribute('title') ?? '')
  if (!rowTitle.includes('/')) {
    failures.push('scene 5: the picker row tooltip carries no path to tell the twins apart')
  }
  await shot('5-picker-row-hover-and-tooltip-text')

  await browser.close()
  srv.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exit(1)
  }
  console.log(`PASS: the picker exposes no qualifier, a mapped qualified key names its copy, and a refused write re-enumerated (/api/skills calls: ${skillsCalls})`)
}

main().catch(err => { console.error(err); process.exit(1) })
