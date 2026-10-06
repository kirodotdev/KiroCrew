/**
 * Screenshot harness for the Git panel's branch switcher.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures.
 * The branch fixture is stateful: a POST to /api/project/git/switch moves
 * `current`, so the frames show the header and the list following a switch.
 *
 * Frames:
 *   1-closed        header shows the checked-out branch with a chevron
 *   2-open          picker: local (newest first, ahead pill) + remote sections
 *   3-filtered      typed query narrowing the list, plus the create row
 *   4-switched      header after switching to a local branch
 *   5-dirty         409 git_switch_dirty rendered as the localized notice
 *   6-blocked       switchBlocked: "filter" explains why every row is inert
 *   7-light-open    the open picker in the light theme
 *   8-chip-closed   the composer's project chip with its branch segment
 *   9-chip-open     the same picker opened upward from the composer chip
 *   10-chip-switched chip and panel header both follow a switch made from the chip
 *   11-chip-light-open the composer picker in the light theme
 *
 * Usage: node scripts/capture-branch-switcher.mjs <outDir>
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/branch-switcher'
const SLOT = 'chat-branch-switcher'
const PROJECT = '/home/user/workspace/demo-service'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Branch switcher',
  running: false,
  last_message: 'Switch branches from the Changes panel.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 60, content: 'Can I switch branches from the Changes tab?' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Yes: click the branch name in the panel header.' },
  ],
}

const h = await openTranscriptHarness({
  slot: SLOT,
  project: PROJECT,
  slots,
  detail,
  viewport: { width: 1400, height: 820 },
  deviceScaleFactor: 1,
})

const ago = s => new Date(Date.now() - s * 1000).toISOString()
const row = (name, s, subject, extra = {}) => ({
  name, sha: 'a1b2c3d', date: ago(s), author: 'Gray Smith', subject, switchable: true, ...extra,
})

let current = 'develop'
let mode = 'ok'
const localRows = () => [
  row('develop', 3600, 'Add Namespace Governance tab to AI SDLC dashboard', { upstream: 'origin/develop', ahead: 1 }),
  row('sdlc-0911', 6 * 86400, 'Add SCTE 2026 speaking materials, webinar diagrams'),
  row('second-brain', 90 * 86400, 'Add sea-shell skills'),
  row('feature/dev-backup-042426', 210 * 86400, 'Update docs'),
  row('feature/main-backup', 400 * 86400, "Merge branch 'develop' into 'main'"),
  row('main', 400 * 86400, "Merge branch 'develop' into 'main'", { upstream: 'origin/main', behind: 3 }),
].map(r => ({ ...r, current: r.name === current }))

const dashCfg = { auto_open_git_panel: true }
await h.page.route(/\/api\/dashboard\/config$/, async route => json(route, dashCfg))

await h.page.route(/\/api\/project\/git/, async route => {
  const req = route.request()
  const path = new URL(req.url()).pathname
  if (path === '/api/project/git/status') {
    return json(route, {
      repo: true,
      repoRoot: PROJECT,
      branch: current,
      ahead: current === 'develop' ? 1 : undefined,
      files: [
        { path: 'resources/ai-tools/kirocrew-questions-feedback.md', status: 'M', staged: false, additions: 16, deletions: 0 },
        { path: 'maps/MOC-tools.md', status: 'M', staged: false, additions: 3, deletions: 0 },
        { path: 'areas/dev-ai-platform/kiro-administration-transition-plan.html', status: '?', staged: false, additions: 128 },
      ],
    })
  }
  if (path === '/api/project/git/log') {
    return json(route, {
      repo: true,
      commits: [
        { sha: '19af431', message: 'Add Namespace Governance tab to AI SDLC dashboard', author: 'Gray Smith', date: ago(3600), isHead: true },
        { sha: 'a6b0d9e', message: 'chore: ignore .kiro/settings and add program overview backup', author: 'Gray Smith', date: ago(86400), isHead: false },
      ],
    })
  }
  if (path === '/api/project/git/branches') {
    return json(route, {
      repo: true,
      current,
      local: localRows(),
      remote: [row('origin/feature/ai-deck-v5', 30 * 86400, 'Refactor DocsWithQPhing layout')],
      ...(mode === 'blocked' ? { switchBlocked: 'filter' } : {}),
    })
  }
  if (path === '/api/project/git/switch') {
    const body = JSON.parse(req.postData() || '{}')
    if (mode === 'dirty') {
      return json(route, { error: 'Your uncommitted changes would be overwritten by this switch.', code: 'git_switch_dirty' }, 409)
    }
    const previous = current
    current = body.branch
    return json(route, { ok: true, branch: current, previous })
  }
  return json(route, { repo: true, repoRoot: PROJECT, branch: current })
})

const trigger = '[data-testid="branch-switcher-trigger"][data-variant="header"]'
const chipTrigger = '[data-testid="branch-switcher-trigger"][data-variant="chip"]'
const pop = '[data-testid="branch-switcher"]'

async function boot(theme = 'dark') {
  await h.load(theme, { selector: 'textarea', settle: 1500 })
  await h.page.waitForSelector(trigger, { timeout: 15000 })
  await h.page.waitForTimeout(400)
}

async function shot(name) {
  await h.page.waitForTimeout(300)
  await h.page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('SHOT', name)
}

async function openPicker() {
  await h.page.click(trigger)
  await h.page.waitForSelector(`${pop} [data-testid="branch-row-local"]`, { timeout: 15000 })
}

await boot()
await shot('1-closed')

await openPicker()
await shot('2-open')

await h.page.keyboard.type('feat')
await shot('3-filtered')
await h.page.keyboard.press('Escape')

await openPicker()
await h.page.locator(`${pop} [data-testid="branch-row-local"]`, { hasText: 'sdlc-0911' }).dispatchEvent('mousedown')
await h.page.waitForSelector(pop, { state: 'detached', timeout: 15000 })
await h.page.waitForSelector(`${trigger}:has-text("sdlc-0911")`, { timeout: 15000 })
console.log('SWITCHED', await h.page.locator(trigger).textContent())
await shot('4-switched')

mode = 'dirty'
await openPicker()
await h.page.locator(`${pop} [data-testid="branch-row-local"]`, { hasText: 'second-brain' }).dispatchEvent('mousedown')
await h.page.waitForSelector('[data-testid="branch-switcher-error"]', { timeout: 15000 })
console.log('DIRTY', await h.page.locator('[data-testid="branch-switcher-error"]').textContent())
await shot('5-dirty')
await h.page.keyboard.press('Escape')

mode = 'blocked'
await h.page.evaluate(() => {})
await boot()
await openPicker()
await h.page.waitForSelector('[data-testid="branch-switcher-blocked"]', { timeout: 15000 })
await shot('6-blocked')

mode = 'ok'
await boot('light')
await openPicker()
await shot('7-light-open')
await h.page.keyboard.press('Escape')

// The composer's project chip opens the same picker, upward.
current = 'develop'
await boot()
await h.page.waitForSelector(`${chipTrigger}:has-text("develop")`, { timeout: 15000 })
await shot('8-chip-closed')
await h.page.click(chipTrigger)
await h.page.waitForSelector(`${pop}[data-placement="above"] [data-testid="branch-row-local"]`, { timeout: 15000 })
await shot('9-chip-open')
await h.page.locator(`${pop} [data-testid="branch-row-local"]`, { hasText: 'second-brain' }).dispatchEvent('mousedown')
await h.page.waitForSelector(pop, { state: 'detached', timeout: 15000 })
await h.page.waitForSelector(`${chipTrigger}:has-text("second-brain")`, { timeout: 15000 })
await h.page.waitForSelector(`${trigger}:has-text("second-brain")`, { timeout: 15000 })
console.log('CHIP SWITCHED', await h.page.locator(chipTrigger).textContent(), '| header:', await h.page.locator(trigger).textContent())
await shot('10-chip-switched')

await boot('light')
await h.page.click(chipTrigger)
await h.page.waitForSelector(`${pop} [data-testid="branch-row-local"]`, { timeout: 15000 })
await shot('11-chip-light-open')

await h.close()
