/**
 * Screenshot harness for the two agent pickers that still rendered a failed
 * roster fetch as an empty list (#7656) — the Task Runner compose panel and
 * Issue Radar's crew editor.
 *
 * Same contract as capture-agent-roster-failure.mjs (the schedule form's
 * harness, #5990): `GET /api/agents` fails for the "failed" frames and is
 * healthy for the "recovered" ones, so Retry has something to recover to. The
 * frames cannot lie — the script ASSERTS the error copy is present in every
 * failed frame, the misleading "No matches" is absent from the compose picker,
 * and every seeded agent is offered after Retry. Any of those failing exits
 * non-zero and the PNGs are not citable.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server
 * and answers every /api/** call from fixtures through `stubDashboardApi`.
 *
 * Usage: node scripts/capture-agent-picker-roster-failure.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'
import { makeExtra, seedState } from './lib/issue-radar-crews-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/agent-picker-roster-failure'
const ERROR_COPY = "Couldn't load the agent list."

mkdirSync(OUT, { recursive: true })

const AGENTS = [
  { name: 'kirocrew', kiro_agent: 'kirocrew', source: 'builtin', description: 'The built-in agent' },
  { name: 'gpu-research', kiro_agent: 'gpu-research', source: 'package', description: 'Internal research' },
  { name: 'oncall', kiro_agent: 'oncall-agent', source: 'package', description: 'Paging and triage' },
]

// Flipped between frames: the roster endpoint is broken first, healthy after.
let rosterHealthy = false

const crewsExtra = makeExtra(json)

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const failures = []

try {
  const context = await browser.newContext({
    // 1x, per the image-read ceiling the sibling harnesses observe.
    viewport: { width: 1500, height: 940 },
    deviceScaleFactor: 1,
  })

  async function newPage(seed) {
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      localStorageEntries: { 'mc-lang': 'en' },
      extra: async (path, route) => {
        if (path === '/api/agents') {
          if (!rosterHealthy) {
            await route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"gateway restarting"}' })
            return true
          }
          await json(route, { agents: AGENTS, default_agent: 'kirocrew' })
          return true
        }
        if (path === '/api/agents/sync') {
          await json(route, { ok: true, synced: [] })
          return true
        }
        if (path === '/api/taskrunner') {
          await json(route, { running: false, available: true, runs: [] })
          return true
        }
        return crewsExtra(path, route)
      },
    })
    if (seed) {
      await page.addInitScript((entries) => {
        for (const [k, v] of Object.entries(entries)) localStorage.setItem(k, v)
      }, seed)
    }
    return page
  }

  // ── Surface 1: Task Runner compose panel (ProjectsPage) ──
  rosterHealthy = false
  let page = await newPage()
  await page.goto(base + '/projects', { waitUntil: 'domcontentloaded' })
  const trigger = page.getByLabel('Switch agent').first()
  await trigger.waitFor({ timeout: 15000 })
  await trigger.click()

  const picker = page.getByRole('dialog', { name: 'Agent list' })
  await picker.waitFor({ state: 'visible', timeout: 5000 })
  await picker.getByText(ERROR_COPY).waitFor({ timeout: 5000 })
  // The popover fades in; a shot on text-appearance alone catches opacity 0.
  await page.waitForTimeout(600)

  const misleading = await picker.getByText('No matches').count()
  if (misleading !== 0) failures.push(`compose failed frame still shows "No matches" (${misleading}x)`)
  await page.screenshot({ path: join(OUT, 'projects-failed.png') })
  console.log('wrote projects-failed.png')

  rosterHealthy = true
  await picker.getByText('Retry').click()
  await picker.getByRole('option').first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(600)
  const offered = await picker.getByRole('option').allTextContents()
  const missing = AGENTS.filter(a => !offered.some(t => t.includes(a.name))).map(a => a.name)
  if (missing.length) failures.push(`compose Retry did not recover the roster; missing: ${missing.join(', ')}`)
  await page.screenshot({ path: join(OUT, 'projects-recovered.png') })
  console.log('wrote projects-recovered.png')
  await page.close()

  // ── Surface 2: Issue Radar crew editor ──
  rosterHealthy = false
  page = await newPage(seedState({ crewFilter: 'all' }))
  await page.goto(base + '/issue-radar', { waitUntil: 'domcontentloaded' })
  await page.locator('[data-testid="crew-create"]').first().waitFor({ state: 'visible', timeout: 20000 })
  await page.locator('[data-testid="crew-create"]').first().click()
  const dialog = page.getByTestId('crew-editor')
  await dialog.waitFor({ state: 'visible', timeout: 10000 })

  const agentField = dialog.getByTestId('crew-editor-agent')
  await agentField.getByText(ERROR_COPY).waitFor({ timeout: 10000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: join(OUT, 'crew-editor-failed.png') })
  console.log('wrote crew-editor-failed.png')

  rosterHealthy = true
  await agentField.getByText('Retry', { exact: true }).click()
  // Recovery clears the error line; the roster now feeds the picker.
  await agentField.getByText(ERROR_COPY).waitFor({ state: 'detached', timeout: 10000 })
  await agentField.getByRole('combobox').click()
  const crewOptions = page.getByRole('option')
  await crewOptions.first().waitFor({ timeout: 10000 })
  await page.waitForTimeout(600)
  const crewOffered = await crewOptions.allTextContents()
  const crewMissing = AGENTS.filter(a => !crewOffered.some(t => t.includes(a.name))).map(a => a.name)
  if (crewMissing.length) failures.push(`crew editor Retry did not recover the roster; missing: ${crewMissing.join(', ')}`)
  await page.screenshot({ path: join(OUT, 'crew-editor-recovered.png') })
  console.log('wrote crew-editor-recovered.png')
  await page.close()

  await context.close()

  if (failures.length) {
    for (const f of failures) console.error('FAIL:', f)
    process.exitCode = 1
  } else {
    console.log('wrote', OUT)
  }
} finally {
  await browser.close()
  srv.close()
}
