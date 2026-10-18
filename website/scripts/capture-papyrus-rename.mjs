/**
 * Recording + screenshot harness for renaming a Papyrus paper (#13336).
 *
 * Scenes, on the REAL built SPA with every /api/** call answered from fixtures
 * (no gateway), served by `vite preview` started in-process:
 *
 *   rename-flow.webm         open the pencil, type a name, Enter saves; open it
 *                            again, type, Escape cancels. Step stills 01-05.
 *   rename-refused-<lang>    a refused rename (`title_rejected`), banner in the
 *                            user's language, for en and de.
 *   list-load-error          the paper list fails to load; the banner says so.
 *   toolbar-<width>          the open paper's toolbar at 1280px and 320px: the
 *                            header name and the main-document group.
 *
 * Usage (from website/, after `npx vite build`):
 *   node scripts/capture-papyrus-rename.mjs [outDir] [distDir] [prefix]
 * With prefix `before` (a build of the base branch), only the toolbar scenes
 * run, since the list scenes need the rename control.
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync, readdirSync, renameSync } from 'node:fs'

import { handleBootRoute, json } from './lib/boot-api.mjs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/papyrus-rename'
const DIST = process.argv[3] || DEFAULT_DIST
const PREFIX = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const PAPYRUS = '/api/apps/papyrus'
const HEALTH = {
  status: 'ok', compiler: 'tectonic', git: true,
  managed: {
    supported: true, installed: true, release: '', version: '',
    job: { state: 'idle', error: '', attempt: 0, bytes_downloaded: 0, bytes_total: 0, elapsed: 0 },
  },
}

const now = Math.floor(Date.now() / 1000)
// A main document with a long path: a short one (`main.tex`) hid the overlap.
const MAIN_FILE = 'chapters/introduction.tex'
const FILES = ['main.tex', MAIN_FILE, 'references.bib']
const seedProjects = (mode) => [
  { name: '6a16ddbf', title: mode === 'renamed' ? 'MACKEREL' : '6a16ddbf', modified: now - 3600, has_pdf: true, generation: 'g1' },
  { name: 'thesis-draft', title: 'thesis-draft', modified: now - 86400, has_pdf: false, generation: 'g2' },
]

function catalog(lang) {
  const url = new URL(`../src/i18n/locales/${lang}.json`, import.meta.url)
  return JSON.parse(readFileSync(url, 'utf-8')).apps.papyrus.page
}

/**
 * @param mode 'ok'           PUT /title saves into the in-memory list
 *             'refuse'       PUT /title answers 400 title_rejected
 *             'list-error'   GET /projects answers 500
 */
async function preparePage(context, { lang, mode }) {
  const projects = seedProjects(mode)
  const page = await context.newPage()
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path === `${PAPYRUS}/health`) return json(route, HEALTH)
    if (path === `${PAPYRUS}/projects` && req.method() === 'GET') {
      if (mode === 'list-error') {
        return json(route, { error: 'could not read the papers folder: permission denied' }, 500)
      }
      return json(route, { projects })
    }
    if (path === `${PAPYRUS}/project` && req.method() === 'GET') {
      const p = projects.find(x => x.name === new URL(req.url()).searchParams.get('name')) || projects[0]
      return json(route, {
        name: p.name, title: p.title, main_file: MAIN_FILE, files: FILES, has_pdf: false,
      })
    }
    if (path === `${PAPYRUS}/files`) return json(route, { files: FILES })
    if (path === `${PAPYRUS}/file` && req.method() === 'GET') {
      return json(route, { path: MAIN_FILE, content: '\\documentclass{article}\n\\begin{document}\nIntro.\n\\end{document}\n' })
    }
    if (path === `${PAPYRUS}/git`) return json(route, { branch: 'main', dirty: false, ahead: 0, behind: 0, files: [] })
    if (path === `${PAPYRUS}/title` && req.method() === 'PUT') {
      const { name, title } = JSON.parse(req.postData() || '{}')
      if (mode === 'refuse') {
        return json(route, {
          error: 'this name has no displayable text; leave the field empty to clear it',
          code: 'title_rejected',
        }, 400)
      }
      const p = projects.find(x => x.name === name)
      p.title = title || name
      return json(route, { ok: true, title: p.title })
    }
    if (path === '/api/theme/boot') return json(route, { mode: 'light', theme: '', language: lang })
    if (path.startsWith('/api/apps')) return json(route, { apps: [], installed: [] })
    return handleBootRoute(route, path, { project: '/home/example/project', theme: 'light' })
  })
  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 240)))
  await page.addInitScript(language => {
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('kc-onboarded', '1')
    localStorage.setItem('mc-lang', language)
  }, lang)
  await page.goto(`${BASE}/papyrus`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return page
}

/** The paper list card (and the banner above it), not the whole window. */
const listClip = page => page.evaluate(() => {
  const table = document.querySelector('table')
  const bottom = table ? table.getBoundingClientRect().bottom : 600
  return { x: 0, y: 0, width: document.documentElement.clientWidth, height: Math.min(bottom + 40, 720) }
})

let BASE = ''

/**
 * The open paper's toolbar, for a paper renamed `MACKEREL` whose directory is
 * `6a16ddbf`: the header should read the name, and the main-document group
 * should sit inside the row (no overlap at 1280px, wrapped as a unit at 320px).
 * Prints each group box against its parent so the overlap is a number too.
 */
async function captureToolbar(browser) {
  const ws = { ...JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf-8')).apps.papyrus.workspace }
  for (const width of [1280, 320]) {
    const context = await browser.newContext({ viewport: { width, height: 720 }, deviceScaleFactor: 2 })
    const page = await preparePage(context, { lang: 'en', mode: 'renamed' })
    await page.getByText(PREFIX === 'before' ? '6a16ddbf' : 'MACKEREL', { exact: true }).first().click()
    // The trigger, not the label text: on the base branch the label is a bare
    // text node beside the select, so it has no element of its own.
    const trigger = page.locator(`[aria-label="${ws.main_document}"]`).first()
    await trigger.waitFor({ timeout: 20000 })
    await page.waitForTimeout(1500)
    const m = await trigger.evaluate(el => {
      // The group is the muted span holding the label and the select.
      const group = el.closest('span.text-muted')
      let row = group
      while (row && !(row.getBoundingClientRect().width > 0.6 * document.documentElement.clientWidth)) row = row.parentElement
      const box = e => { const r = e.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width), right: Math.round(r.right), h: Math.round(r.height) } }
      return {
        row: row ? box(row) : null,
        group: group ? box(group) : null,
        trigger: box(el),
        // > 0 means the select paints past the group that should hold it.
        overflow: group ? Math.round(el.getBoundingClientRect().right - group.getBoundingClientRect().right) : null,
        clip: row ? { y: Math.max(0, row.getBoundingClientRect().top - 8), h: row.getBoundingClientRect().height + 16 } : null,
      }
    })
    console.log(`${PREFIX}-toolbar-${width}:`, JSON.stringify(m))
    const clip = m.clip
      ? { x: 0, y: m.clip.y, width, height: Math.min(m.clip.h, 400) }
      : { x: 0, y: 0, width, height: 240 }
    await page.screenshot({ path: `${OUT}/${PREFIX}-toolbar-${width}.png`, clip })
    await context.close()
  }
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  BASE = base
  // This host's node sets LD_LIBRARY_PATH to its own bundled lib dir; give the
  // browser child the extracted chromium libs instead (CHROME_LIBS).
  const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: process.env.CHROME_LIBS || '' } })
  const viewport = { width: 1024, height: 640 }

  await captureToolbar(browser)
  if (PREFIX === 'before') {
    await browser.close()
    srv.close()
    return
  }

  // 1. Recording of the rename flow, with step stills.
  {
    const en = catalog('en')
    const context = await browser.newContext({ viewport, recordVideo: { dir: `${OUT}/video`, size: viewport } })
    const page = await preparePage(context, { lang: 'en', mode: 'ok' })
    // The pointer leaves the row first, so a hover tint on the pencil is not
    // read as the field still being open.
    const shot = async name => {
      await page.mouse.move(viewport.width - 10, viewport.height - 10)
      await page.waitForTimeout(200)
      return page.screenshot({ path: `${OUT}/${name}.png`, clip: await listClip(page) })
    }
    await shot('01-list-before')
    await page.getByRole('button', { name: en.rename_paper.replace('{{name}}', '6a16ddbf'), exact: true }).click()
    await page.waitForTimeout(700)
    await shot('02-field-open')
    const field = page.getByRole('textbox', { name: en.paper_name, exact: true })
    await field.fill('')
    await field.pressSequentially('MACKEREL', { delay: 90 })
    await page.waitForTimeout(500)
    await shot('03-typed')
    await field.press('Enter')
    await page.waitForTimeout(900)
    await shot('04-saved')
    await page.getByRole('button', { name: en.rename_paper.replace('{{name}}', 'MACKEREL'), exact: true }).click()
    await page.waitForTimeout(600)
    await field.fill('')
    await field.pressSequentially('Thrown away', { delay: 90 })
    await page.waitForTimeout(500)
    await field.press('Escape')
    await page.waitForTimeout(900)
    await shot('05-escape-cancelled')
    await page.waitForTimeout(600)
    await context.close()
    const [webm] = readdirSync(`${OUT}/video`).filter(f => f.endsWith('.webm'))
    renameSync(`${OUT}/video/${webm}`, `${OUT}/rename-flow.webm`)
  }

  // 2. A refused rename, in the user's language.
  for (const lang of ['en', 'de']) {
    const t = catalog(lang)
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    const page = await preparePage(context, { lang, mode: 'refuse' })
    await page.getByRole('button', { name: t.rename_paper.replace('{{name}}', '6a16ddbf'), exact: true }).click()
    const field = page.getByRole('textbox', { name: t.paper_name, exact: true })
    await field.fill('\u200b\u200b')
    await field.press('Enter')
    await page.getByText(t.rename_title_rejected.replace('{{name}}', '6a16ddbf')).waitFor()
    await page.screenshot({ path: `${OUT}/06-rename-refused-${lang}.png`, clip: await listClip(page) })
    await context.close()
  }

  // 3. The paper list fails to load.
  {
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    const page = await preparePage(context, { lang: 'en', mode: 'list-error' })
    await page.getByText('could not read the papers folder', { exact: false }).first().waitFor({ timeout: 20000 })
    await page.screenshot({ path: `${OUT}/07-list-load-error.png`, clip: { x: 0, y: 0, width: 1024, height: 420 } })
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
