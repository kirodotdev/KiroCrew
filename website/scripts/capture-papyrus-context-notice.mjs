/**
 * Screenshot harness for the Papyrus CO-AUTHOR context-failure notice.
 *
 * Sibling of `capture-artifact-context-notice.mjs` and built on the same parts: the REAL built
 * SPA (website/dist) behind the shared in-process static server, with every /api/** call answered
 * by route interception, so no gateway and no live backend are involved.
 *
 * Opening the co-author starts a session and POSTs the paper as background context. That POST can
 * fail, and until this notice existed the failure was silent -- the co-author answered with no
 * document context and the writer had no way to know. The artifact page's equivalent had three
 * committed frames; this surface had none, so a reviewer could not see it at all.
 *
 * Frames:
 *   papyrus-co-author  the context POST failed -- consequence + remedy on the Papyrus surface
 *
 * This ASSERTS as well as photographs: the run exits non-zero unless the notice rendered with the
 * Papyrus title and body read from `en.manual.json`, so a regression that drops the notice or
 * reroutes it through the page's generic save-error channel fails instead of producing a frame.
 *
 * Usage: node scripts/capture-papyrus-context-notice.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-context-notice'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

// The AUTHORED English, not a copy of it: a hardcoded expectation would keep passing after the
// copy changed, asserting a string the product no longer shows.
const readWorkspace = (file) => {
  try {
    const j = JSON.parse(readFileSync(new URL(`../src/i18n/locales/${file}`, import.meta.url), 'utf-8'))
    return j?.apps?.papyrus?.workspace || {}
  } catch { return {} }
}
// BOTH English catalogs: the authored copy is split between them, and reading one leaves a key
// undefined -- which a role filter then treats as "no filter" and matches every button on the page.
const WS = { ...readWorkspace('en.json'), ...readWorkspace('en.manual.json') }
const need = (key) => {
  const v = WS[key]
  if (typeof v !== 'string' || !v) throw new Error(`no English copy for apps.papyrus.workspace.${key}`)
  return v
}
const TITLE = need('context_notice_title')
const BODY = need('context_not_attached')
const CO_AUTHOR = need('co_author')

const PROJECT = 'quarterly-review'
const SLOT = 'chat-papyrus-co-author'

const PROJECT_DETAIL = {
  name: PROJECT,
  main_file: 'main.tex',
  files: ['main.tex'],
  compiler: { state: 'ready' },
}

const extra = async (path, route) => {
  const method = route.request().method()

  // Papyrus's own backend. Answered narrowly so an unstubbed path fails loudly rather than
  // rendering a half-loaded page that still screenshots.
  if (path === '/api/apps/papyrus/health') {
    return json(route, { ok: true, compiler: 'ready' }), true
  }
  if (path === '/api/apps/papyrus/projects') {
    return json(route, { projects: [{ name: PROJECT, main_file: 'main.tex' }] }), true
  }
  if (path === '/api/apps/papyrus/project') return json(route, PROJECT_DETAIL), true
  if (path === '/api/apps/papyrus/files') return json(route, { files: ['main.tex'] }), true
  if (path === '/api/apps/papyrus/file') {
    return json(route, { path: 'main.tex', content: '\\documentclass{article}\n\\begin{document}\nQuarterly review.\n\\end{document}\n' }), true
  }
  if (path === '/api/apps/papyrus/git') {
    return json(route, { branch: 'main', dirty: false, ahead: 0, behind: 0, files: [] }), true
  }
  if (path.startsWith('/api/apps/papyrus/')) return json(route, {}), true

  // The co-author session: created, then handed the paper as context -- which fails.
  if (path === '/api/chat/slots' && method === 'POST') {
    return json(route, { key: SLOT, title: `Papyrus: ${PROJECT}` }), true
  }
  if (/\/api\/chat\/slots\/[^/]+\/context$/.test(path)) {
    return json(route, { error: 'upstream write failed' }, 500), true
  }
  if (/\/api\/chat\/slots\/[^/]+$/.test(path)) {
    return json(route, { key: SLOT, messages: [] }), true
  }
  return false
}

/**
 * The notice must be the co-author's OWN surface. Asserting the absence of the page's generic
 * error channel is what discriminates: routing this through `setError` would still render an
 * alert, still screenshot, and still look right.
 */
async function assertPapyrusNotice(page, scene) {
  const body = await page.locator('body').innerText()
  if (!body.includes(TITLE)) {
    throw new Error(`${scene}: the co-author notice did not render "${TITLE}"`)
  }
  if (!body.includes(BODY)) {
    throw new Error(`${scene}: the notice rendered without its body copy -- ${BODY}`)
  }
  if (/Failed to fetch|upstream write failed|500/i.test(body)) {
    throw new Error(`${scene}: leaked transport text into the notice copy`)
  }
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1100 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await stubDashboardApi(page, { slots: [], extra })
  logPageProblems(page)

  await page.goto(base + '/papyrus', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)

  // Open the paper from the project list, which is what gives the page a project to share.
  await page.getByText(PROJECT, { exact: false }).first().click()
  await page.waitForTimeout(2500)

  // Opening the co-author starts the session and posts the paper; the stub refuses that post.
  await page.getByRole('button', { name: CO_AUTHOR }).click()
  await page.waitForTimeout(3000)

  await assertPapyrusNotice(page, 'papyrus-co-author')
  await page.screenshot({
    path: `${OUT}/${PREFIX}-papyrus-co-author.png`,
    clip: { x: 0, y: 0, width: 1500, height: 820 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-papyrus-co-author.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
