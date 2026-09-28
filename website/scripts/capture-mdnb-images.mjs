/**
 * Screenshot harness for markdown images in the Notes app.
 *
 * The feature renders `![alt](src)` while keeping the app's promises intact:
 * the SOURCE stays one click away, and a source the app will not load degrades
 * to the alt text instead of a broken frame. Three frames, one claim each:
 *
 *   01 rendered  - two images drawn between ordinary markdown blocks, each
 *                  capped at the reading column rather than overflowing it
 *   02 editing   - clicking an image opens that line's SOURCE in the mono
 *                  block editor, the same gesture as a table or a diagram
 *   03 fallback  - a file that is gone and a source the app refuses both show
 *                  their alt text
 *
 * The local images are served through `/api/file-raw`, the endpoint the real
 * app uses: the stub answers it with SVG bytes, which is what the real handler
 * does for an SVG in a vault. The frames therefore show the production path,
 * not a harness shortcut.
 *
 * kiro-dark only: images carry their own colours, so more themes would
 * photograph the pictures rather than this change.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures. No gateway, no token.
 *
 * Usage: node scripts/capture-mdnb-images.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import {
  MDNB_VAULT,
  MDNB_VAULT_ID,
  mdnbApiStub,
  mdnbNoteDoc,
  mdnbNotesList,
  notePaneClip,
} from './lib/mdnb-fixtures.mjs'

const OUT = process.argv[2] || '../temp-screenshots/mdnb-images'
mkdirSync(OUT, { recursive: true })

/** Note images only: the app chrome has a logo <img> that is not evidence. */
const NOTE_IMG = 'img[src^="/api/file-raw"]'

const NOTE_PATH = 'docs/vpc-topology.md'
const NOTE_TITLE = 'VPC Topology'

/** Vault-relative sources, resolved against the note's own directory. */
const SERVED = {
  [`${MDNB_VAULT.localPath}/docs/assets/topology.svg`]: topology(),
  [`${MDNB_VAULT.localPath}/docs/assets/subnet-table.svg`]: subnets(),
}

const NOTE = `# ${NOTE_TITLE}

The production VPC spans three availability zones, each with a public and a
private subnet:

![Three-tier VPC across three availability zones](assets/topology.svg)

Address allocation follows the same split in every zone, which is what keeps
the route tables interchangeable:

![Subnet address allocation per availability zone](assets/subnet-table.svg)

Peering to the shared-services account is documented separately.
`

const FALLBACK_NOTE = `# ${NOTE_TITLE}

The diagram below was renamed in the vault, so its file is no longer there:

![Three-tier VPC across three availability zones](assets/renamed.svg)

And this source is one the app refuses to load at all:

![Untrusted source](javascript:void 0)

Both keep the note readable instead of leaving a broken frame behind.
`

/** A three-zone VPC sketch, wide enough to prove the column cap. */
function topology() {
  const zone = (x, name) => `
    <rect x="${x}" y="46" width="150" height="128" rx="6" fill="#0f172a" stroke="#3f4a63"/>
    <text x="${x + 75}" y="68" font-size="12" fill="#93a4c3" text-anchor="middle">${name}</text>
    <rect x="${x + 12}" y="80" width="126" height="34" rx="4" fill="#1d4ed8" opacity="0.35" stroke="#60a5fa"/>
    <text x="${x + 75}" y="101" font-size="11" fill="#dbeafe" text-anchor="middle">public subnet</text>
    <rect x="${x + 12}" y="124" width="126" height="34" rx="4" fill="#166534" opacity="0.35" stroke="#4ade80"/>
    <text x="${x + 75}" y="145" font-size="11" fill="#dcfce7" text-anchor="middle">private subnet</text>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 200" width="720" height="200">
    <rect width="720" height="200" rx="8" fill="#111827"/>
    <text x="24" y="30" font-size="13" fill="#e5e7eb">vpc-prod  10.0.0.0/16</text>
    ${zone(24, 'eu-west-1a')}${zone(200, 'eu-west-1b')}${zone(376, 'eu-west-1c')}
    <rect x="552" y="46" width="144" height="128" rx="6" fill="#0f172a" stroke="#3f4a63"/>
    <text x="624" y="68" font-size="12" fill="#93a4c3" text-anchor="middle">shared services</text>
    <text x="624" y="112" font-size="11" fill="#cbd5f5" text-anchor="middle">transit gateway</text>
    <text x="624" y="132" font-size="11" fill="#cbd5f5" text-anchor="middle">attachment</text>
  </svg>`
}

/** A second, differently shaped image so the frame is not one picture twice. */
function subnets() {
  const row = (y, zoneName, pub, priv) => `
    <text x="20" y="${y}" font-size="11" fill="#cbd5f5">${zoneName}</text>
    <text x="150" y="${y}" font-size="11" fill="#dbeafe">${pub}</text>
    <text x="320" y="${y}" font-size="11" fill="#dcfce7">${priv}</text>`
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 116" width="480" height="116">
    <rect width="480" height="116" rx="8" fill="#111827"/>
    <text x="20" y="26" font-size="11" fill="#93a4c3">zone</text>
    <text x="150" y="26" font-size="11" fill="#93a4c3">public</text>
    <text x="320" y="26" font-size="11" fill="#93a4c3">private</text>
    <line x1="20" y1="34" x2="460" y2="34" stroke="#3f4a63"/>
    ${row(56, 'eu-west-1a', '10.0.0.0/20', '10.0.48.0/20')}
    ${row(78, 'eu-west-1b', '10.0.16.0/20', '10.0.64.0/20')}
    ${row(100, 'eu-west-1c', '10.0.32.0/20', '10.0.80.0/20')}
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

async function shoot(browser, base, doc, { file, edit = false, fallback = false }) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })
  const page = await context.newPage()
  const mdnb = mdnbApiStub({ notes: mdnbNotesList(NOTE_PATH, NOTE_TITLE), doc })
  await stubDashboardApi(page, {
    theme: 'dark',
    extra: async (path, route) => (await fileRaw(path, route)) || (await mdnb(path, route)),
  })
  logPageProblems(page)
  await page.addInitScript(vaultId => localStorage.setItem('mdnb-active-vault', vaultId), MDNB_VAULT_ID)

  await page.goto(base + '/md-notebook', { waitUntil: 'domcontentloaded' })
  await page.getByText(NOTE_TITLE).first().waitFor({ timeout: 15000 })
  await page.getByText(NOTE_TITLE).first().click()

  if (fallback) {
    // The failure paths resolve once both alt texts stand in for their images.
    await page.getByText('Three-tier VPC across three availability zones').waitFor({ timeout: 20000 })
    await page.getByText('Untrusted source').waitFor({ timeout: 5000 })
    // Scoped to note images: the app chrome carries its own logo <img>, so a
    // bare count would never reach zero and would hide a real regression.
    if ((await page.locator(NOTE_IMG).count()) !== 0) throw new Error('expected no note img')
  } else {
    // A rendered frame is only evidence once the BYTES have decoded: an <img>
    // that 404s is still in the DOM for a tick, so assert natural width too.
    await page.locator(NOTE_IMG).first().waitFor({ timeout: 20000 })
    await page.waitForFunction(() => {
      const imgs = [...document.querySelectorAll('img[src^="/api/file-raw"]')]
      return imgs.length === 2 && imgs.every(i => i.naturalWidth > 0)
    }, null, { timeout: 20000 })
  }
  await page.waitForTimeout(500)

  const applied = await page.evaluate(() => document.documentElement.dataset.theme || '')
  if (applied !== 'kiro-dark') throw new Error(`theme mismatch: wanted kiro-dark, got ${applied || '(none)'}`)

  if (edit) {
    await page.locator(NOTE_IMG).first().click()
    await page.locator('textarea').first().waitFor({ timeout: 5000 })
    await page.waitForTimeout(300)
  }

  await page.screenshot({ path: `${OUT}/${file}`, clip: await notePaneClip(page) })
  console.log('wrote', `${OUT}/${file}`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const rendered = mdnbNoteDoc(NOTE_PATH, NOTE)
  const broken = mdnbNoteDoc(NOTE_PATH, FALLBACK_NOTE)
  try {
    await shoot(browser, base, rendered, { file: '01-images-rendered.png' })
    await shoot(browser, base, rendered, { file: '02-click-opens-source.png', edit: true })
    await shoot(browser, base, broken, { file: '03-fallback-to-alt-text.png', fallback: true })
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => { console.error(err); process.exit(1) })
