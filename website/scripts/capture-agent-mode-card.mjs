/**
 * Screenshots for the Crew Members -> Agents rename (PR #11869): the three
 * surfaces whose renamed copy is only visible on screen, so the PR can SHOW the
 * words rather than assert them in prose.
 *
 *   agent-mode-card             Settings > Developer > Feature Previews, the
 *                               switch that reveals the Agents page.
 *   create-agent-dialog         The create form reached from the Agents roster's
 *                               "+" (?new=1&from=members): titled "Add agent",
 *                               section "What this agent uses", the template
 *                               hint "The starting setup this agent uses...",
 *                               primary action "Create agent".
 *   sort-filter-agents-popover  The roster's "Sort and filter agents" menu,
 *                               open, with the renamed origin descriptions
 *                               ("Agents you created yourself" / "Agents that
 *                               ship with ...") on its source rows.
 *
 * Gateway-free: the built SPA is served from loopback and every /api/** call is
 * answered from fixtures, so this needs `npm run build` and nothing else
 * running. Frame 1 boots through `openSettingsPage`; frames 2 and 3 drive the
 * real /capabilities and /members routes with the shared `serveDist` +
 * `stubDashboardApi` stub, following the roster capture scripts' conventions.
 *
 * EVERY frame is GATED on the rendered copy: a surface whose text is not the
 * expected label writes no PNG and the run exits nonzero, so a stale capture
 * cannot be attached as evidence for a rename that did not land. And every
 * frame's target is deleted BEFORE its page is driven (see `armFrame`), so a
 * failed frame leaves NO file rather than the previous run's -- and a frame that
 * fails its own assertions never suppresses a sibling frame that passed.
 *
 * Usage (from website/):
 *   npm run build
 *   node scripts/capture-agent-mode-card.mjs [outDir]
 */
import { existsSync, mkdirSync, rmSync } from 'node:fs'
import path from 'node:path'

import { chromium } from 'playwright'

import { openSettingsPage } from './lib/settings-capture.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'
import { crewsApi } from './lib/crews-fixtures.mjs'

const OUT = path.resolve(process.argv[2] || '../temp-screenshots/agent-mode')
mkdirSync(OUT, { recursive: true })

// A non-zero exit if ANY frame's assertions failed, so a red gate on one
// surface fails the whole run even though the other frames still wrote.
let failed = false

/**
 * Resolve a frame's absolute path and remove any previous copy BEFORE the page
 * is driven -- not after a failure. A run that fails its assertions writes
 * nothing, but a PREVIOUS run's frame is still sitting at this exact path (the
 * path the PR body references), so "no PNG written" would silently mean "the
 * old PNG is still your evidence". Removing it up front makes a failed frame
 * leave no file at all.
 *
 * `force` only swallows a MISSING file, so a permission error or a directory at
 * this path still throws -- and either way the old frame would survive, which
 * is the exact case this delete exists to prevent. Refuse loudly and name the
 * file rather than letting a stack trace scroll past above a stale PNG. Only
 * this one frame's file is touched, so a sibling frame in the same directory is
 * never removed.
 */
function armFrame(file) {
  const p = path.join(OUT, file)
  try {
    rmSync(p, { force: true })
  } catch (err) {
    console.error(`refusing to capture: cannot remove the previous frame at ${p} (${err.message})`)
    process.exit(2)
  }
  if (existsSync(p)) {
    console.error(`refusing to capture: the previous frame is still at ${p}`)
    process.exit(2)
  }
  return p
}

/**
 * A per-frame assertion collector. Each frame gets its own so a mismatch on one
 * surface writes no PNG for THAT frame while leaving the others untouched; a
 * miss also trips the global `failed` for the exit code.
 */
function frameGate(label) {
  let ok = true
  return {
    check(name, cond, detail = '') {
      const good = !!cond
      console.log(`[${label}] ${name}: ${good ? 'OK' : 'MISMATCH'} ${detail}`)
      if (!good) { ok = false; failed = true }
      return good
    },
    get ok() { return ok },
  }
}

// ---------------------------------------------------------------------------
// Frame 1 -- the Settings "Agent Mode" card (the PR's original frame).
// ---------------------------------------------------------------------------
async function captureAgentModeCard() {
  const SHOT = armFrame('agent-mode-card.png')
  const EXPECTED_LABEL = 'Agent Mode'
  const g = frameGate('agent-mode-card')

  const { browser, context, page, srv } = await openSettingsPage({ tab: 'developer', height: 1000 })
  try {
    const toggle = page.getByRole('switch', { name: EXPECTED_LABEL, exact: true })
    await toggle.waitFor({ state: 'visible', timeout: 30_000 })

    // Climb from the switch to the nearest ancestor that actually carries the
    // card's prose. Picking a fixed div depth guesses at markup this PR does
    // not own, and an empty container silently shoots a blank frame.
    const box = await toggle.evaluate(el => {
      let node = el
      while (node.parentElement) {
        node = node.parentElement
        const t = (node.innerText || '').trim()
        if (t.length > 60) {
          const r = node.getBoundingClientRect()
          return { text: t, x: r.x, y: r.y, width: r.width, height: r.height }
        }
      }
      return null
    })
    if (!box) throw new Error('no ancestor of the switch carries the card prose')

    const text = box.text.replace(/\s+/g, ' ')
    g.check('card label', text.startsWith(EXPECTED_LABEL), `text="${text.slice(0, 90)}"`)
    g.check('names the Agents page', /\bAgents page\b/.test(text), `text="${text.slice(0, 120)}"`)
    g.check('no stale noun', !/Crew Members/i.test(text), 'card must not say "Crew Members"')

    if (g.ok) {
      const pad = 8
      await page.screenshot({
        path: SHOT,
        clip: {
          x: Math.max(0, box.x - pad),
          y: Math.max(0, box.y - pad),
          width: box.width + pad * 2,
          height: box.height + pad * 2,
        },
      })
      console.log(`wrote ${SHOT}`)
    } else {
      console.log('assertions failed - no PNG written, and any earlier frame was removed')
    }
  } finally {
    await context.close()
    await browser.close()
    srv.close()
  }
}

// ---------------------------------------------------------------------------
// Frame 2 -- the create-agent dialog, reached the way the Members roster's "+"
// reaches it (?new=1&from=members auto-opens the form in the roster's words).
// ---------------------------------------------------------------------------
async function captureCreateAgentDialog(browser, base) {
  const SHOT = armFrame('create-agent-dialog-dark.png')
  const g = frameGate('create-agent-dialog')

  const context = await browser.newContext({ viewport: { width: 1280, height: 940 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: crewsApi({
      crews: [
        { name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', source: 'builtin' },
        { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-mem', source: 'aim' },
      ],
      defaultAgent: 'kirocrew',
    }),
  })

  try {
    // new=1 auto-opens the create form; from=members is the Members "+" path
    // (#9513), where the primary action is "Create agent", not "Create".
    await page.goto(`${base}/capabilities?tab=crews&new=1&from=members`, { waitUntil: 'domcontentloaded' })

    const dialog = page.getByRole('dialog', { name: 'Add agent' })
    await dialog.waitFor({ state: 'visible', timeout: 30_000 })

    const text = (await dialog.innerText()).replace(/\s+/g, ' ')
    // Selecting the dialog by name already proves the "Add agent" title; assert
    // it in the body too so the frame's own copy is gated, not just the a11y name.
    g.check('title "Add agent"', /\bAdd agent\b/.test(text), `text="${text.slice(0, 90)}"`)
    // The section heading is CSS-uppercased ("WHAT THIS AGENT USES"), which
    // innerText reflects, so match case-insensitively -- it still catches a
    // "member" regression, which is the word the rename is about.
    g.check('section "What this agent uses"', /what this agent uses/i.test(text))
    g.check(
      'template hint "The starting setup this agent uses"',
      /The starting setup this agent uses/.test(text),
    )
    g.check(
      'primary action "Create agent"',
      (await dialog.getByRole('button', { name: 'Create agent', exact: true }).count()) === 1,
      'the submit button must read "Create agent"',
    )
    g.check(
      'no stale member/crew copy',
      !/Crew Members/i.test(text) && !/What this member uses/i.test(text),
      'dialog must not carry the pre-rename wording',
    )

    if (g.ok) {
      await page.waitForTimeout(300)
      await dialog.screenshot({ path: SHOT })
      console.log(`wrote ${SHOT}`)
    } else {
      console.log('assertions failed - no PNG written for the create-agent dialog')
    }
  } finally {
    await context.close()
  }
}

// ---------------------------------------------------------------------------
// Frame 3 -- the roster's "Sort and filter agents" popover, open. A viewport
// below the mobile breakpoint (768px) keeps the roster a single column and
// stops the desktop auto-open of a member thread, so the frame is just the
// roster and its menu -- no ChatPane, no thread fixtures.
// ---------------------------------------------------------------------------
const MEMBERS_FIXTURE = [
  { name: 'conductor', slug: 'conductor', source: 'kirocrew', starred: true, bound: false, slot_key: '', running: false, kiro_agent: 'conductor', workspace: 'default', memory_store: 'default', last_active_ts: 300, last_message: 'Cleared the triage queue.' },
  { name: 'kirocrew', slug: 'kirocrew', source: 'builtin', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', last_active_ts: 200, last_message: 'Ready.' },
  { name: 'triage', slug: 'triage', source: 'package', bound: false, slot_key: '', running: false, kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', last_active_ts: 100, last_message: 'Two PRs opened.' },
]

async function membersExtra(pathname, route) {
  if (pathname === '/api/members') return json(route, { members: MEMBERS_FIXTURE, default_agent: 'kirocrew' }), true
  if (pathname === '/api/autonudge') return json(route, { enabled: true, loops: [] }), true
  if (pathname === '/api/crons') return json(route, { jobs: [] }), true
  if (pathname === '/api/webhooks') return json(route, { tokens: [] }), true
  // Safety net: below the breakpoint no thread auto-opens, but keep the member
  // sub-endpoints honest so an unexpected fetch cannot error-boundary the page.
  const thread = pathname.match(/^\/api\/members\/([^/]+)\/thread$/)
  if (thread) {
    const slug = decodeURIComponent(thread[1])
    return json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false }), true
  }
  if (/^\/api\/members\/[^/]+\/activity$/.test(pathname)) {
    return json(route, { slug: '', member: '', capped: false, entries: [] }), true
  }
  return false
}

async function captureSortFilterPopover(browser, base) {
  const SHOT = armFrame('sort-filter-agents-popover-dark.png')
  const g = frameGate('sort-filter-agents-popover')

  const context = await browser.newContext({ viewport: { width: 760, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme: 'dark', extra: membersExtra })

  try {
    await page.goto(`${base}/members`, { waitUntil: 'domcontentloaded' })

    const trigger = page.getByTestId('member-filter-menu')
    await trigger.waitFor({ state: 'visible', timeout: 30_000 })
    g.check(
      'trigger reads "Sort and filter agents"',
      (await trigger.getAttribute('aria-label')) === 'Sort and filter agents',
      `aria-label="${await trigger.getAttribute('aria-label')}"`,
    )

    await trigger.click()
    const mine = page.getByTestId('member-filter-source-mine')
    await mine.waitFor({ state: 'visible', timeout: 10_000 })

    const mineTitle = await mine.getAttribute('title')
    g.check(
      'origin "mine" description renamed',
      mineTitle === 'Agents you created yourself',
      `title="${mineTitle}"`,
    )
    const builtinTitle = await page.getByTestId('member-filter-source-builtin').getAttribute('title')
    g.check(
      'origin "built-in" description renamed',
      /^Agents that ship with\b/.test(builtinTitle || ''),
      `title="${builtinTitle}"`,
    )

    const menuText = (await page.getByTestId('member-filters').innerText()).replace(/\s+/g, ' ')
    g.check(
      'no stale member/crew copy in the menu',
      !/crew member/i.test(menuText),
      `menu="${menuText.slice(0, 120)}"`,
    )

    if (g.ok) {
      await page.waitForTimeout(300)
      // The menu is a floating overlay over the roster; a viewport shot frames
      // both the open popover and the roster it filters.
      await page.screenshot({ path: SHOT })
      console.log(`wrote ${SHOT}`)
    } else {
      console.log('assertions failed - no PNG written for the sort/filter popover')
    }
  } finally {
    await context.close()
  }
}

// ---------------------------------------------------------------------------

await captureAgentModeCard()

{
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await captureCreateAgentDialog(browser, base)
    await captureSortFilterPopover(browser, base)
  } finally {
    await browser.close()
    srv.close()
  }
}

process.exit(failed ? 1 : 0)
