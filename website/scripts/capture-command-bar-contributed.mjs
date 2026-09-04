/**
 * Screenshot harness for commands CONTRIBUTED by an installed app.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No gateway,
 * no dashboard auth, no kiro-cli — which is what lets it run on a host where the
 * pod's port-ownership proof cannot be made.
 *
 * The four frames are the four states that carry the feature's whole contract, in
 * the order a user meets them:
 *   1. the three modes offered once the query names them
 *   2. the argument state — the chip names the mode, the field asks for the link
 *   3. an unrecognized link refused in the field, before anything is created
 *   4. a recognized link, one Enter from a seeded session
 *
 * Frame 3 is the one worth photographing most: it is the state that proves a wrong
 * clipboard stops at the bar instead of reaching a session told to approve
 * everything behind it.
 *
 * Usage: node scripts/capture-command-bar-contributed.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/command-bar-contributed'

mkdirSync(OUT, { recursive: true })

const SLOT = 'chat-1'

/**
 * Two apps: the launcher itself (a builtin, so it claims the quick-search slot) and
 * the EXTERNAL app whose commands this harness photographs. The contribution below
 * is copied from that app's real app.json, so these frames show the platform seam
 * working from outside this repository rather than a hand-built row.
 *
 * Two details a fixture gets wrong easily: `/api/apps` answers a BARE ARRAY
 * (`normalizeInstalledApps` takes `T[]`), and the declarations live under
 * `manifest.*` -- the overlay resolver reads `manifest.ui.overlays` and the command
 * reader `manifest.contributes.commands`, never a top-level copy.
 */
const APPS = [
  {
    name: 'command-bar',
    displayName: 'Command Bar',
    enabled: true,
    origin: 'builtin',
    source: 'builtin',
    version: '0.1.0',
    manifest: {
      name: 'command-bar',
      displayName: 'Command Bar',
      version: '0.1.0',
      ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] },
    },
  },
  {
    name: 'pr-bulk-ops',
    displayName: 'PR Bulk Ops',
    enabled: true,
    // NOT builtin: an external app cannot claim an overlay, but it CAN contribute
    // commands -- which is the whole point of this surface.
    origin: 'registry',
    source: 'git',
    version: '1.0.0',
    manifest: {
      name: 'pr-bulk-ops',
      displayName: 'PR Bulk Ops',
      version: '1.0.0',
      contributes: {
          "commands": [
                {
                      "id": "review-all",
                      "title": "Review all PRs",
                      "subtitle": "Review every pull request behind a link",
                      "icon": "ScanEye",
                      "keywords": [
                            "pr",
                            "prs",
                            "github",
                            "review",
                            "all"
                      ],
                      "argument": {
                            "placeholder": "Paste a GitHub link\u2026",
                            "hint": "A PR search, a repo's PR list, a label, a milestone, or a single pull request.",
                            "kind": "url",
                            "hosts": ["github.com"],
                            "patternError": "Not a github.com link."
                      },
                      "prompt": "Load the $pr-bulk-ops skill and run its `review` mode over {argument}\n\nResolve the link to a PR list and print that list before the first write. If it is empty, say so and stop rather than widening the query. If it holds more than 25 pull requests, print it and stop for my confirmation.\n\nReview each pull request properly, one clean context per PR - fan out a sub-agent per PR rather than reviewing them all in this one. Post your review comments.\n\nDo NOT approve anything. This is review only; the verdict is mine.\n\nFinish with one table: PR, author, verdict, and the headline finding for each.",
                      "autoSend": true
                },
                {
                      "id": "approve-all",
                      "title": "Approve all PRs",
                      "subtitle": "Approve every pull request behind a link, without reviewing",
                      "icon": "Check",
                      "keywords": [
                            "pr",
                            "prs",
                            "github",
                            "approve",
                            "lgtm",
                            "all"
                      ],
                      "argument": {
                            "placeholder": "Paste a GitHub link\u2026",
                            "hint": "A PR search, a repo's PR list, a label, a milestone, or a single pull request.",
                            "kind": "url",
                            "hosts": ["github.com"],
                            "patternError": "Not a github.com link."
                      },
                      "prompt": "Load the $pr-bulk-ops skill and run its `approve` mode over {argument}\n\nResolve the link to a PR list and print that list before the first write. If it is empty, say so and stop rather than widening the query. If it holds more than 25 pull requests, print it and stop for my confirmation.\n\nApprove each one without reviewing it - I have already decided. `gh pr review <n> --repo <owner>/<repo> --approve`.\n\nTwo skips, both recorded rather than retried: a PR I authored (GitHub refuses a self-approval, so it cannot be done at all), and a PR already approved.\n\nFinish with one table: PR, author, approved or skipped, and the reason for each skip.",
                      "autoSend": true
                },
                {
                      "id": "merge-all",
                      "title": "Merge all PRs",
                      "subtitle": "Merge every ready pull request behind a link, approving first where allowed",
                      "icon": "GitMerge",
                      "keywords": [
                            "pr",
                            "prs",
                            "github",
                            "merge",
                            "ship",
                            "land",
                            "all"
                      ],
                      "argument": {
                            "placeholder": "Paste a GitHub link\u2026",
                            "hint": "A PR search, a repo's PR list, a label, a milestone, or a single pull request.",
                            "kind": "url",
                            "hosts": ["github.com"],
                            "patternError": "Not a github.com link."
                      },
                      "prompt": "Load the $pr-bulk-ops skill and run its `merge` mode over {argument}\n\nResolve the link to a PR list and print that list before the first write. If it is empty, say so and stop rather than widening the query. If it holds more than 25 pull requests, print it and stop for my confirmation.\n\nMerge each one that is legally mergeable, and approve first only where that is both needed and permitted:\n\n- Not approved yet and NOT authored by me - approve it, then merge.\n- Not approved yet and authored by me - skip it. The approval that would unblock the merge is the one GitHub will not let me give.\n- Draft, conflicting, red checks, or an outstanding CHANGES_REQUESTED - skip, naming which.\n\nNever force, never bypass with --admin, never touch branch protection, and never rebase someone's branch to get a merge through. Prefer a squash merge when the repo allows it.\n\nFinish with one table: PR, author, merged or skipped, and the reason for each skip.",
                      "autoSend": true
                }
          ]
    },
    },
  },
]

const { srv, base } = await serveDist()
const browser = await chromium.launch()

async function openBar() {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 1 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, APPS)
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    slots: [{ key: SLOT, messages: 0, running: false, agent: 'default', mode: '' }],
    extra,
  })

  await page.goto(`${base}/chat`)
  await page.waitForLoadState('networkidle')
  // The quick-search chord. The overlay claims the slot, so this opens the launcher.
  await page.keyboard.press('Control+k')
  await page.waitForSelector('[role="dialog"]', { timeout: 10_000 })
  return { context, page }
}

async function shot(page, name) {
  await page.waitForTimeout(350)
  const file = join(OUT, name)
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
}

// ── 1. the app's three commands, once the query names them ─────────────────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('all prs')
  await shot(page, '1-commands-offered.png')
  await context.close()
}

// ── 2. the argument state ──────────────────────────────────────────────────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('approve all')
  // Activate the row by clicking it: mousedown is what the list binds.
  const row = page.getByRole('option').filter({ hasText: 'Approve all PRs' }).first()
  await row.dispatchEvent('mousedown')
  await shot(page, '2-argument-state.png')
  await context.close()
}

// ── 3. a value the app's declared kind refuses ───────────────────────────────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('approve all')
  await page.getByRole('option').filter({ hasText: 'Approve all PRs' }).first().dispatchEvent('mousedown')
  await page.getByRole('combobox').fill('https://gitlab.com/group/project/-/merge_requests/12')
  await page.getByRole('combobox').press('Enter')
  await page.waitForSelector('[role="alert"]', { timeout: 5_000 })
  await shot(page, '3-value-refused.png')
  await context.close()
}

// ── 4. the resolved prompt, shown before an auto-sending command fires ─────
{
  const { context, page } = await openBar()
  await page.getByRole('combobox').fill('merge all')
  await page.getByRole('option').filter({ hasText: 'Merge all PRs' }).first().dispatchEvent('mousedown')
  await page.getByRole('combobox').fill('https://github.com/kirodotdev/KiroCrew/pulls?q=is%3Aopen+is%3Apr+label%3A%22readiness%3A+passed%22')
  // Wait for the CUE, not just the preview. The clipped warning is set by an effect that
  // measures the rendered box, so it lands one render after the prompt text does -- and
  // shooting between the two produced exactly the misleading frame this review caught: a
  // prompt cut mid-sentence with nothing saying it continues. This prompt is 947
  // characters over roughly 25 rendered lines against a ~10-line box, so it always
  // clips; if the cue ever stops rendering, this line fails instead of quietly
  // photographing its absence.
  await page.getByText(/Scroll to read the rest/).waitFor({ timeout: 5_000 })
  await shot(page, '4-prompt-preview.png')
  await context.close()
}

await browser.close()
srv.close()
