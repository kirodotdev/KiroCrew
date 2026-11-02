/**
 * Screenshot harness: the Add Skill and Add Server dialogs open on a
 * featured list instead of an empty "Type at least 2 characters to search".
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. The featured rows are
 * read from the bundled `src/kiro_crew/data/discovery_featured.json`, so the
 * pictures show the list that ships. A base build never asks for them and
 * renders the old empty dialog, which is the "before" picture.
 *
 * Writes <dialog>-<theme>-<size>-<tag>.png for dialog in {skills, mcp},
 * theme in {dark, light} and size in {desktop, mobile}, plus
 * skills-error-<theme>-<size>-<tag>.png with the featured request failing.
 *
 * Usage: node scripts/capture-26887-featured-discovery.mjs [outDir] [tag]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/featured-discovery'
const TAG = process.argv[3] || 'after'
const PROJECT = '/home/user/.kiro/crew/workspace'
const SIZES = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } }

mkdirSync(OUT, { recursive: true })

const FEATURED = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../src/kiro_crew/data/discovery_featured.json', import.meta.url)), 'utf8'))

const skillRows = FEATURED.skills.map(e => ({
  description: '', tags: [], installs: 0, installed: false, author: '', repo_url: '',
  ...e, display_provider: 'skills.sh',
}))
const mcpRows = FEATURED.mcp_servers.map(e => ({
  title: '', version: '', installed: false, deprecated: false, ...e, display_provider: 'MCP Registry',
}))

function discoverRoute(route, url, rows, provider, fail) {
  if (fail) return json(route, { error: 'Gateway unreachable' }, 502)
  const featured = url.searchParams.get('featured') === '1' && !url.searchParams.get('q')
  return json(route, featured
    ? { results: rows, providers: [provider], provider_outcomes: [] }
    : { results: [], providers: [provider], provider_outcomes: [] })
}

async function shoot(browser, base, dialog, theme, size, fail = false) {
  const context = await browser.newContext({ viewport: SIZES[size], deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/skills/-/discover') return discoverRoute(route, url, skillRows, 'skillsh', fail)
    if (path === '/api/mcp/discover') return discoverRoute(route, url, mcpRows, 'official')
    // The MCP Servers tab reads these as objects; the boot default would crash it.
    if (path === '/api/acp-backends') return json(route, { backends: [] })
    if (path === '/api/mcp/scopes') return json(route, { scopes: [] })
    if (path === '/api/connections/status') return json(route, { schema_version: 1, connections: [] })
    if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '', language: 'en' })
    return handleBootRoute(route, path, { project: PROJECT, theme, fixedApi: makeFixedApi(PROJECT) })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  await page.addInitScript(t => {
    localStorage.clear()
    localStorage.setItem('mc-theme', t)
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'en')
    localStorage.setItem('mc-yolo-ack', '1')
  }, theme)

  const target = dialog === 'skills' ? '/capabilities?tab=skills' : '/capabilities?tab=mcp&view=mcp-servers'
  await page.goto(base + target, { waitUntil: 'domcontentloaded' })
  const button = page.locator(`[data-ui-location="${dialog === 'skills' ? 'skills.add' : 'mcp.add-server'}"]`).first()
  await button.waitFor({ timeout: 15000 })
  // The tab re-renders once its boot queries settle; click after that.
  await page.waitForTimeout(1500)
  await button.click()
  const dlg = page.getByRole('dialog').last()
  await dlg.waitFor({ timeout: 10000 })
  await page.waitForTimeout(900)
  const file = `${OUT}/${dialog}${fail ? '-error' : ''}-${theme}-${size}-${TAG}.png`
  await page.screenshot({ path: file })
  console.log('wrote', file)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const dialog of ['skills', 'mcp']) {
      for (const theme of ['dark', 'light']) {
        for (const size of ['desktop', 'mobile']) await shoot(browser, base, dialog, theme, size)
      }
    }
    await shoot(browser, base, 'skills', 'dark', 'desktop', true)
    await shoot(browser, base, 'skills', 'light', 'mobile', true)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
