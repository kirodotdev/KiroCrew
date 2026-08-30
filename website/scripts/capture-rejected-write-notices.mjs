/**
 * Capture the three rejected-write notices as rendered.
 *
 * Why this exists rather than the Playwright harness: on this host every Playwright launch
 * form fails at the CDP handshake, while `chrome --headless --screenshot` needs no websocket
 * and works. So the frame is produced by rasterising a page instead of driving the app.
 *
 * What keeps it faithful rather than a lookalike:
 *   - the COPY is read from `src/i18n/locales/en.manual.json`, the shipped catalogue;
 *   - the MARKUP is the block-variant container and inner structure copied out of
 *     `src/components/ErrorNotice.tsx` (the classes are asserted present below);
 *   - the STYLESHEET is the compiled Tailwind bundle from `dist/assets/`.
 *
 * It is a component-faithful static render, NOT a live-app screenshot: no click produced it,
 * so it evidences the copy and its notice styling, not the surrounding flow.
 */

import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = dirname(fileURLToPath(import.meta.url))
const web = resolve(here, '..')
const OUT = process.argv[2] ? resolve(process.argv[2]) : ''
if (!OUT) throw new Error('usage: node capture-rejected-write-notices.mjs <out.png>')

const catalog = JSON.parse(
  readFileSync(join(web, 'src/i18n/locales/en.manual.json'), 'utf8')
).components.agentSkillsEditor

const notice = readFileSync(join(web, 'src/components/ErrorNotice.tsx'), 'utf8')
const CONTAINER =
  'rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 flex items-start gap-2 text-[13px] text-danger'
if (!notice.includes(CONTAINER)) {
  throw new Error('ErrorNotice block classes changed -- refusing to render a stale lookalike')
}

const css = readdirSync(join(web, 'dist/assets'))
  .filter(f => f.startsWith('src-') && f.endsWith('.css'))
  .map(f => join(web, 'dist/assets', f))[0]
if (!css) throw new Error('no compiled stylesheet found in dist/assets')

const fill = (s, name) => s.replaceAll('{{name}}', name)
const cases = [
  ['key_changed_repick', fill(catalog.key_changed_repick, 'widgets')],
  ['key_changed_remove_blocked', fill(catalog.key_changed_remove_blocked, 'prepare-pr')],
  ['key_changed_remove_blocked_generic', catalog.key_changed_remove_blocked_generic],
]
for (const [k, v] of cases) {
  if (!v) throw new Error(`catalogue is missing ${k}`)
}

// Inlined so the frame cannot depend on a relative asset path resolving under file://.
const style = readFileSync(css, 'utf8')
const svg =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" class="mt-[2px] shrink-0" aria-hidden="true">' +
  '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>' +
  '<path d="M12 9v4"/><path d="M12 17h.01"/></svg>'

const blocks = cases
  .map(
    ([key, text]) => `
    <div class="mb-4">
      <div class="text-[11px] text-muted font-mono mb-1">${key}</div>
      <div role="alert" class="${CONTAINER}">
        ${svg}
        <div class="min-w-0 flex-1 whitespace-pre-wrap" style="overflow-wrap:anywhere">${text}</div>
      </div>
    </div>`
  )
  .join('')

const html = `<!doctype html><html class="dark"><head><meta charset="utf-8">
<style>${style}</style>
<style>body{width:720px;padding:24px;background:var(--bg,#0d0f12)}</style>
</head><body class="bg-bg text-text">${blocks}</body></html>`

const page = `${OUT}.html`
writeFileSync(page, html)
// Exit status is NOT the check: chrome reports success on stderr and its status has been
// observed non-zero on a frame it wrote correctly. The PNG itself is the evidence, so it is
// DECODED below and the run fails on what the header says, not on what the process returned.
// Resolved through Playwright's own browser registry, the way every sibling capture script
// under this directory gets its Chromium -- no second copy of an absolute path to go stale.
const browser = chromium.executablePath()

try {
  execFileSync(
    browser,
    ['--headless', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
     '--window-size=760,272', `--screenshot=${OUT}`, `file://${page}`],
    // Node's own lib dir is on the child's library path, and its `libstdc++.so.6` is older
    // than the one chrome links against -- so chrome spawned from node dies on a GLIBCXX
    // version while the identical command from a shell runs. Dropping the inherited path is
    // what makes the capture work here at all.
    { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, LD_LIBRARY_PATH: '' } }
  )
} catch (e) {
  // A spawn failure carries no exit status, and it must ABORT here rather than surface later as
  // an unreadable PNG. A status DID come back means chrome ran, which the decode adjudicates.
  if (typeof e.status !== 'number') {
    console.error(`could not launch Chromium at ${browser}: ${e.code ?? e.message}`)
    console.error('install the Playwright browsers first: npx playwright install chromium')
    process.exit(1)
  }
  console.error(`chrome status=${e.status}: ${String(e.stderr ?? '').slice(0, 400)}`)
}

const png = readFileSync(OUT)
const SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
if (!png.subarray(0, 8).equals(SIG)) throw new Error('not a PNG: signature mismatch')
if (png.subarray(12, 16).toString('ascii') !== 'IHDR') throw new Error('no IHDR chunk')
const width = png.readUInt32BE(16)
const height = png.readUInt32BE(20)
if (width < 400 || height < 200) throw new Error(`frame too small to show a notice: ${width}x${height}`)
if (png.length < 5000) throw new Error(`frame is ${png.length} bytes -- probably blank`)
// The page is an intermediate, and it inlines the whole compiled stylesheet -- committing a
// quarter-megabyte of generated CSS beside the frame would be repo-hygiene debt.
rmSync(page, { force: true })
console.log(`decoded ${OUT}: ${width}x${height}, ${png.length} bytes`)
console.log(`rendered ${cases.length} notices`)
for (const [k, v] of cases) console.log(`  ${k}: ${v}`)
