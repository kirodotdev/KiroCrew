/**
 * Screenshot harness for Obsidian `![[file]]` image embeds in the Notes app.
 *
 * The feature resolves an embed the way Obsidian does — through the vault's
 * `attachmentFolderPath` setting, then the note's folder, then a vault-wide
 * lookup by name — while a standard `![alt](src)` image keeps resolving against
 * the note. Two frames, one claim each:
 *
 *   01 mixed     - a note out of a vault migrated from Obsidian: a standard
 *                  markdown image, a bare embed found in `z-assets/`, and an
 *                  embed with a `|360` width, all rendered together
 *   02 fallback  - an embed whose file is gone and one whose name two files
 *                  share both show the file name with the broken-image glyph,
 *                  rather than a broken frame or an arbitrary picture; a note
 *                  transclusion keeps the wikilink presentation instead of
 *                  being called a missing image
 *
 * The images are served through `/api/file-raw`, the endpoint the real app uses,
 * and the vault's attachment folder setting and index through
 * `/apps/md-notebook/api/attachments`, so the frames show the production
 * resolution path, not a harness shortcut.
 *
 * kiro-dark only: images carry their own colours, so more themes would
 * photograph the pictures rather than this change.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-embeds.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import {
  MDNB_VAULT,
  MDNB_VAULT_ID,
  mdnbApiStub,
  mdnbNoteDoc,
  mdnbNotesList,
  notePaneClip,
} from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-embeds'
mkdirSync(OUT, { recursive: true })

/** Note images only: the app chrome has a logo <img> that is not evidence. */
const NOTE_IMG = 'img[src^="/api/file-raw"]'

const NOTE_PATH = 'customers/acme/2026-09-02-data-platform.md'
const NOTE_TITLE = '2026-09-02-data-platform'

/** The vault record: the listing carries nothing about attachments. */
const VAULT = MDNB_VAULT
/** An Obsidian vault whose images live in `z-assets/`: the setting the index route reports. */
const ATTACHMENT_FOLDER = 'z-assets'

/** What the backend's attachment index reports for this vault. */
const ATTACHMENTS = [
  'customers/acme/assets/new-flow.svg',
  'z-assets/Pasted image 20260902101512.svg',
  'z-assets/acme-legacy-estate.svg',
  'archive/2025/duplicate-name.svg',
  'archive/2026/duplicate-name.svg',
]

/** Absolute paths the file endpoint answers, keyed the way the page asks for them. */
const SERVED = {
  [`${VAULT.localPath}/customers/acme/assets/new-flow.svg`]: flow(),
  [`${VAULT.localPath}/z-assets/acme-legacy-estate.svg`]: estate(),
  [`${VAULT.localPath}/z-assets/Pasted image 20260902101512.svg`]: badge(),
}

const NOTE = `# Data platform: target state

The new pipeline, documented after the migration to this app:

![Ingest, curate and serve layers on the new platform](assets/new-flow.svg)

The legacy estate, pasted while this vault was still edited in Obsidian and
kept exactly as it was written:

![[acme-legacy-estate.svg]]

Sized the Obsidian way, with a width suffix:

![[Pasted image 20260902101512.svg|360]]

Both syntaxes keep working side by side.
`

const FALLBACK_NOTE = `# Data platform: target state

This embed points at a file that was deleted from \`z-assets/\`:

![[deleted-diagram.svg]]

And this name exists twice in the vault, so the app declines to guess (hover
the name for the reason):

![[duplicate-name.svg]]

A transclusion of another note is not an image at all, so it stays a wikilink:

![[2026-09-01-data-platform-kickoff]]

All three keep the note readable instead of showing a broken frame or the wrong picture.
`

/** Three stacked layers, the shape of a new-platform sketch. */
function flow() {
  const layer = (y, label, fill, stroke) => `
    <rect x="24" y="${y}" width="672" height="40" rx="6" fill="${fill}" opacity="0.35" stroke="${stroke}"/>
    <text x="360" y="${y + 25}" font-size="12" fill="#e5e7eb" text-anchor="middle">${label}</text>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 176" width="720" height="176">
    <rect width="720" height="176" rx="8" fill="#111827"/>
    ${layer(16, 'ingest: streams and batch landing', '#1d4ed8', '#60a5fa')}
    ${layer(68, 'curate: governed catalog and quality gates', '#7c3aed', '#c4b5fd')}
    ${layer(120, 'serve: warehouse, feature store, APIs', '#166534', '#4ade80')}
  </svg>`
}

/** A legacy estate: boxes wired to one central database. */
function estate() {
  const box = (x, y, label) => `
    <rect x="${x}" y="${y}" width="120" height="44" rx="5" fill="#1f2937" stroke="#6b7280"/>
    <text x="${x + 60}" y="${y + 27}" font-size="11" fill="#d1d5db" text-anchor="middle">${label}</text>`
  const wire = (x1, y1) => `<line x1="${x1}" y1="${y1}" x2="300" y2="112" stroke="#9ca3af" stroke-dasharray="4 3"/>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 200" width="600" height="200">
    <rect width="600" height="200" rx="8" fill="#0b1220"/>
    ${wire(84, 46)}${wire(300, 46)}${wire(516, 46)}${wire(84, 178)}${wire(516, 178)}
    ${box(24, 24, 'ERP extracts')}${box(240, 24, 'CRM exports')}${box(456, 24, 'spreadsheets')}
    ${box(24, 156, 'nightly jobs')}${box(456, 156, 'BI reports')}
    <ellipse cx="300" cy="112" rx="70" ry="26" fill="#b45309" opacity="0.5" stroke="#fbbf24"/>
    <text x="300" y="116" font-size="12" fill="#fef3c7" text-anchor="middle">shared Oracle</text>
  </svg>`
}

/** A small status badge, wide enough that `|360` visibly narrows it. */
function badge() {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 96" width="640" height="96">
    <rect width="640" height="96" rx="12" fill="#052e16" stroke="#22c55e"/>
    <circle cx="48" cy="48" r="18" fill="#22c55e"/>
    <text x="88" y="42" font-size="16" fill="#dcfce7">cut-over rehearsal</text>
    <text x="88" y="66" font-size="12" fill="#86efac">all 14 pipelines green, 2026-09-02</text>
  </svg>`
}

/** Answer `/api/file-raw` the way the real handler does for a vault SVG. */
async function fileRaw(path, route) {
  if (path !== '/api/file-raw') return false
  const wanted = new URL(route.request().url()).searchParams.get('path') || ''
  const body = SERVED[wanted]
  if (!body) {
    await route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' })
    return true
  }
  await route.fulfill({ status: 200, contentType: 'image/svg+xml', body })
  return true
}

/** The attachment folder and index, answered before the shared stub's catch-all reaches it. */
async function attachments(path, route) {
  if (!path.startsWith('/apps/md-notebook/api/attachments')) return false
  json(route, { attachmentFolderPath: ATTACHMENT_FOLDER, files: ATTACHMENTS })
  return true
}

async function shoot(browser, base, doc, { file, fallback = false }) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  const mdnb = mdnbApiStub({ vault: VAULT, notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE), doc })
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) =>
      (await fileRaw(path, route)) || (await attachments(path, route)) || (await mdnb(path, route)),
  })
  logPageProblems(page)
  await page.addInitScript(vaultId => localStorage.setItem('mdnb-active-vault', vaultId), MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()

  if (fallback) {
    // Both embeds resolve to nothing, so the frame is evidence once both file
    // names stand in for their images and no note image is left in the DOM.
    await page.getByText('deleted-diagram.svg').waitFor({ timeout: 20000 })
    const ambiguous = page.getByText('duplicate-name.svg', { exact: true })
    await ambiguous.waitFor({ timeout: 5000 })
    // The ambiguous fallback says why as visible text beside the name, naming
    // the paths the resolver found; a plain miss does not.
    await page
      .getByText('Several files in the vault are named duplicate-name.svg (archive/2025/duplicate-name.svg, archive/2026/duplicate-name.svg)')
      .waitFor({ timeout: 5000 })
    if (await page.getByText('deleted-diagram.svg', { exact: true }).getAttribute('title'))
      throw new Error('plain miss carries a reason')
    // The transclusion is neither an image nor a missing one: text, no glyph.
    const note = page.getByText('2026-09-01-data-platform-kickoff')
    await note.waitFor({ timeout: 5000 })
    if ((await note.locator('svg').count()) !== 0) throw new Error('transclusion rendered as a missing image')
    if ((await page.locator(NOTE_IMG).count()) !== 0) throw new Error('expected no note img')
  } else {
    // A rendered frame is only evidence once the BYTES have decoded: an <img>
    // that 404s is still in the DOM for a tick, so assert natural width too,
    // and that the width suffix actually narrowed the third image.
    await page.locator(NOTE_IMG).first().waitFor({ timeout: 20000 })
    await page.waitForFunction(() => {
      const imgs = [...document.querySelectorAll('img[src^="/api/file-raw"]')]
      return imgs.length === 3 && imgs.every(i => i.naturalWidth > 0) && imgs[2].style.width === '360px'
    }, null, { timeout: 20000 })
  }
  await page.waitForTimeout(500)

  const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
  if (applied !== 'kiro-dark') throw new Error(`theme mismatch: wanted kiro-dark, got ${applied || '(none)'}`)

  await page.screenshot({ path: `${OUT}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${OUT}/${file}`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    await shoot(browser, base, mdnbNoteDoc(NOTE_PATH, NOTE), { file: '01-markdown-and-embeds-together.png' })
    await shoot(browser, base, mdnbNoteDoc(NOTE_PATH, FALLBACK_NOTE), {
      file: '02-fallbacks-missing-ambiguous-transclusion.png',
      fallback: true,
    })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
