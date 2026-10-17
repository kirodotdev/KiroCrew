/**
 * Screenshot + measurement harness: a refused name whose first bad character is
 * a look-alike letter, in Code Review Sage's Learning rail at 320px and on the
 * Papyrus paper list.
 *
 * Both surfaces explain a refused name in red. When the refused character is a
 * letter, the copy names its script and code point, because a Cyrillic capital
 * `О` renders exactly like the Latin `O` the rule allows; the rule itself is one
 * clause so the narrow rail holds a few lines of red text rather than a
 * paragraph. This captures the rail message for a Cyrillic capital and for a
 * non-letter, in English and in German (the longest catalog), plus the Papyrus
 * new-paper refusal for a Cyrillic name, and prints how many lines each message
 * wraps to.
 *
 * Runs the REAL SPA with every /api/** call answered from fixtures (no gateway),
 * the same way scripts/capture-sage-rail-bar.mjs does. The rail width is seeded
 * through the rail's own persisted width, so the capture is the width a user who
 * dragged the rail to 320px sees.
 *
 * Usage: node scripts/capture-lookalike-name-refusal.mjs <baseUrl> <outDir> [prefix]
 *   prefix defaults to `after`; run it against a checkout without the change and
 *   pass `before` to capture the same scenes there.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

import { handleBootRoute, json } from './lib/boot-api.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:4319'
const OUT = process.argv[3] || '../temp-screenshots/lookalike-name-refusal'
const PREFIX = process.argv[4] || 'after'

mkdirSync(OUT, { recursive: true })

const SAGE = '/api/apps/code-review-sage'
const PAPYRUS = '/api/apps/papyrus'

/** The app routes these scenes read; every other boot call is answered by the
 *  shared boot fixtures. */
const FIXTURES = {
  [`${SAGE}/runs`]: { runs: [], pool: null, reviewer: { model: 'auto', effort: 'high' } },
  [`${SAGE}/repos`]: { repos: [{ owner: 'example-org', repo: 'example-service' }] },
  [`${SAGE}/repo-prs`]: { repo: 'example-org/example-service', prs: [], count: 0 },
  [`${SAGE}/settings`]: {
    settings: { model: null, effort: 'high', active_namespaces: ['default'], max_concurrent: 2 },
    models: [], efforts: ['low', 'high'], namespaces: ['default'], reviewer: null,
  },
  [`${SAGE}/learnings`]: { namespace: 'default', learnings: [] },
  [`${PAPYRUS}/health`]: {
    status: 'ok', compiler: 'tectonic', git: true,
    managed: {
      supported: true, installed: true, release: '', version: '',
      job: { state: 'idle', error: '', attempt: 0, bytes_downloaded: 0, bytes_total: 0, elapsed: 0 },
    },
  },
}

/** The refusal the backend answers for each typed name. */
const REFUSALS = {
  'Отчет': { char: 'О', error: "invalid name: 'отчет'" },
  'team/a': { char: '/', error: "invalid name: 'team/a'" },
}

const SCENES = [
  { name: 'cyrillic-en-320', app: 'sage', lang: 'en', typed: 'Отчет' },
  { name: 'slash-en-320', app: 'sage', lang: 'en', typed: 'team/a' },
  { name: 'cyrillic-de-320', app: 'sage', lang: 'de', typed: 'Отчет' },
  { name: 'papyrus-cyrillic-en', app: 'papyrus', lang: 'en', typed: 'Отчет' },
]

/** Each surface's own accessible names, read from the catalog so the harness
 *  finds the controls in any language. */
function labels(app, lang) {
  const url = new URL(`../src/i18n/locales/${lang}.json`, import.meta.url)
  const apps = JSON.parse(readFileSync(url, 'utf-8')).apps
  if (app === 'papyrus') return { open: null, field: apps.papyrus.page.new_paper_name }
  const rail = apps.codeReviewSage.components.learningRail
  return { open: rail.new_namespace, field: rail.new_namespace_name }
}

async function preparePage(context, scene) {
  const { app, lang } = scene
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path === `${SAGE}/namespaces` && req.method() === 'POST') {
      const { name } = JSON.parse(req.postData() || '{}')
      const refusal = REFUSALS[name] || { char: '', error: 'invalid name' }
      return json(route, { ok: false, code: 'invalid_namespace_name', ...refusal }, 400)
    }
    if (path === `${SAGE}/namespaces`) {
      return json(route, { namespaces: [{ name: 'default', patterns: 3, candidate: 0 }], active: ['default'] })
    }
    if (path === `${PAPYRUS}/projects` && req.method() === 'POST') {
      const { name } = JSON.parse(req.postData() || '{}')
      const refusal = REFUSALS[name] || { char: '', error: 'invalid name' }
      return json(route, { code: 'invalid_project_name', ...refusal }, 400)
    }
    if (path === `${PAPYRUS}/projects`) return json(route, { projects: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '', language: lang })
    if (path in FIXTURES) return json(route, FIXTURES[path])
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    return handleBootRoute(route, path, { project: '/home/example/project', theme: 'light' })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(({ language }) => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
    localStorage.setItem('kc:code-review-sage:rail-width', '320')
    localStorage.setItem('kc:code-review-sage:ui-state', JSON.stringify({
      v: 1,
      state: { mainView: 'learning', listTab: 'pulls', activeRepo: null, selectedRunId: null, selectedPr: null, detailTab: null },
    }))
    localStorage.setItem('mc-lang', language)
  }, { language: lang })
  await page.goto(`${BASE}/${app === 'papyrus' ? 'papyrus' : 'code-review-sage'}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3500)
  return page
}

/** The red message as measurements: its text, its width, and how many lines it
 *  wraps to. Found as the smallest element whose text quotes the refused
 *  character, so it does not depend on the notice's class names. The match is
 *  a plain substring test over the text with its spaces removed, so a quote
 *  style with a space inside it (French guillemets) still matches. */
const measure = (page, char) => page.evaluate(refused => {
  const QUOTE_PAIRS = [['“', '”'], ['„', '“'], ['«', '»'], ['‘', '’'], ['「', '」']]
  const quotes = text => {
    const bare = text.replace(/\s+/g, '')
    return QUOTE_PAIRS.some(([open, close]) => bare.includes(`${open}${refused}${close}`))
  }
  const candidates = [...document.querySelectorAll('body *')]
    .filter(el => el.children.length === 0 && quotes(el.textContent || ''))
  const notice = candidates[0]
  if (!notice) return { text: null }
  const r = notice.getBoundingClientRect()
  const lineHeight = parseFloat(getComputedStyle(notice).lineHeight) || 16
  return {
    text: notice.textContent.trim(),
    width: Math.round(r.width),
    height: Math.round(r.height),
    lines: Math.round(r.height / lineHeight),
  }
}, char)

/** The region to capture: the rail column for Sage, the notice and the
 *  new-paper card for Papyrus. */
const clipFor = (field, app) => field.evaluate((el, which) => {
  if (which === 'papyrus') {
    const bottom = el.getBoundingClientRect().bottom
    return { x: 0, y: 0, width: document.documentElement.clientWidth, height: bottom + 120 }
  }
  let node = el
  while (node && node.getBoundingClientRect().width < 300) node = node.parentElement
  const r = (node || el).getBoundingClientRect()
  return { x: r.x, y: 0, width: r.width, height: el.getBoundingClientRect().bottom + 140 }
}, app)

async function main() {
  // This host's node sets LD_LIBRARY_PATH to its own bundled lib dir, and a
  // browser child inheriting it picks up that older libstdc++ instead of the
  // system one and dies before it opens a page. Clear it for the child only.
  const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '' } })
  for (const scene of SCENES) {
    const viewport = scene.app === 'papyrus' ? { width: 1024, height: 720 } : { width: 1280, height: 720 }
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    const page = await preparePage(context, scene)
    const names = labels(scene.app, scene.lang)
    if (names.open) await page.getByRole('button', { name: names.open, exact: true }).click()
    const field = page.getByRole('textbox', { name: names.field, exact: true })
    await field.fill(scene.typed)
    await field.press('Enter')
    await page.waitForTimeout(800)
    const m = await measure(page, REFUSALS[scene.typed].char)
    console.log(`${PREFIX}-${scene.name}:`, JSON.stringify(m))
    await page.screenshot({ path: `${OUT}/${PREFIX}-${scene.name}.png`, clip: await clipFor(field, scene.app) })
    await context.close()
  }
  await browser.close()
}

main().catch(err => { console.error(err); process.exit(1) })
