/**
 * Screenshot harness for Settings > Environment Variables.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server and the
 * shared gateway-free API stub, so only /api/variables is fixture-specific. The
 * client code under test is unmodified — the cascade indicators, the add-row
 * validation and the per-workspace sections render exactly as in production, and
 * the stubbed PUT mutates the fixture so an after-shot is a real re-render.
 *
 * A FRESH page per shot: a validation alert left open on a reused page swallows
 * the next interaction.
 *
 * Usage: node scripts/capture-variables.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/variables-shots'
mkdirSync(OUT, { recursive: true })

/** Mutated by the stubbed PUT so an after-shot renders real state. */
const store = {
  global: {
    baseUrl: 'https://api.dev.internal',
    orgName: 'Acme Robotics',
    region: 'us-west-2',
    supportQueue: 'tier-1',
  },
  workspaces: {
    default: {},
    ops: { baseUrl: 'https://api.example.com', pagerRotation: 'primary' },
  },
}

/** The effective map for the ops workspace, narrowest scope winning. */
function view() {
  const effective = { ...store.global }
  const winning = {}
  for (const k of Object.keys(store.global)) winning[k] = 'global'
  for (const [k, v] of Object.entries(store.workspaces.ops)) {
    effective[k] = v
    winning[k] = 'workspace'
  }
  return { global: store.global, workspaces: store.workspaces, effective, winning_scope: winning }
}

function extra(path, route) {
  if (path !== '/api/variables') return false
  if (route.request().method() === 'PUT') {
    const body = JSON.parse(route.request().postData() || '{}')
    if (body.scope === 'global') store.global = body.values || {}
    else if (body.scope === 'workspace') store.workspaces[body.workspace] = body.values || {}
    json(route, { ok: true })
    return true
  }
  json(route, view())
  return true
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** Row editors are associated with aria-labelledby, so a by-label lookup for
 *  "Value" also matches them. The add row is addressed by its id suffix instead,
 *  which only the new-pair inputs carry. */
const ADD_NAME = 'input[id$="-new-name"]'
const ADD_VALUE = 'input[id$="-new-value"]'

/** Each shot gets the pristine fixture: shot 3 exercises a real PUT, and without
 *  this its mutation leaks into every later frame. */
const PRISTINE = JSON.stringify(store)
function resetStore() {
  const fresh = JSON.parse(PRISTINE)
  store.global = fresh.global
  store.workspaces = fresh.workspaces
}

async function openPanel() {
  resetStore()
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { extra })
  await page.goto(`${base}/settings?tab=variables`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Global Environment Variables').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  return { ctx, page }
}

// Shot 1 — the global layer.
{
  const { ctx, page } = await openPanel()
  await page.screenshot({ path: join(OUT, '01-global.png') })
  console.log('01-global.png')
  await ctx.close()
}

// Shot 2 — per-workspace pairs and the scope indicator.
{
  const { ctx, page } = await openPanel()
  const region = page.getByRole('region', { name: /ops/i }).first()
  if (await region.count()) await region.scrollIntoViewIfNeeded()
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, '02-workspace-scope.png') })
  console.log('02-workspace-scope.png')
  await ctx.close()
}

// Shot 3 — a reserved name refused before any round trip.
{
  const { ctx, page } = await openPanel()
  await page.locator(ADD_NAME).first().fill('MAX_SUBAGENTS')
  await page.locator(ADD_VALUE).first().fill('9')
  await page.getByRole('button', { name: 'Add', exact: true }).first().click()
  await page.getByRole('alert').first().waitFor({ timeout: 5000 })
  await page.waitForTimeout(200)
  await page.screenshot({ path: join(OUT, '03-reserved-name-refused.png') })
  console.log('03-reserved-name-refused.png')
  await ctx.close()
}

// Shot 4 — the whole panel, both scopes in one frame.
{
  const { ctx, page } = await openPanel()
  await page.screenshot({ path: join(OUT, '04-full-panel.png'), fullPage: true })
  console.log('04-full-panel.png')
  await ctx.close()
}

// Shots 5 and 6 — the narrow layout the blocking narrow-viewport rule requires.
// 390px is a modern phone; 320px is the floor the rule names.
for (const [width, label] of [[390, '05-narrow-390.png'], [320, '06-narrow-320.png']]) {
  resetStore()
  const ctx = await browser.newContext({ viewport: { width, height: 844 } })
  const page = await ctx.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { extra })
  await page.goto(`${base}/settings?tab=variables`, { waitUntil: 'domcontentloaded' })
  await page.getByText('Global Environment Variables').first().waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  await page.screenshot({ path: join(OUT, label), fullPage: true })
  // Overflow check: a horizontally scrollable document at this width is the exact
  // failure the rule exists to catch, so assert it rather than eyeballing the frame.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  console.log(`${label} (horizontal overflow: ${overflow}px)`)
  if (overflow > 0) process.exitCode = 1
  await ctx.close()
}

await browser.close()
srv.close()
console.log('wrote shots to', OUT)
