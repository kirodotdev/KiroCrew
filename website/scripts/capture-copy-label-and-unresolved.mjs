/**
 * Capture the two surfaces the UX lane has no materialized frame for:
 *   - `copy_identifier_label` as it renders on a mapped CHIP and in a PICKER ROW;
 *   - a mapped-but-uninstalled copy: the warn-state chip plus the below-chips count note.
 *
 * Faithfulness, as in the sibling notice capture: the COPY is read from the shipped
 * `src/i18n/locales/en.manual.json`, the CLASS STRINGS are read out of
 * `src/components/AgentSkillsEditor.tsx` and asserted still present (so a renamed class fails the
 * capture instead of producing a lookalike), and the STYLESHEET is the compiled Tailwind bundle
 * from `dist/assets`. It is a component-faithful static render, NOT a live-app screenshot: no
 * click produced it, so it evidences copy and state styling, not the surrounding flow.
 */
import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const here = dirname(fileURLToPath(import.meta.url))
const web = resolve(here, '..')
const OUT = process.argv[2] ? resolve(process.argv[2]) : ''
if (!OUT) throw new Error('usage: capture-copy-label-and-unresolved.mjs <absolute-or-rel.png>')

const t = JSON.parse(readFileSync(join(web, 'src/i18n/locales/en.manual.json'), 'utf8'))
  .components.agentSkillsEditor
const editor = readFileSync(join(web, 'src/components/AgentSkillsEditor.tsx'), 'utf8')

// Read out of the component rather than retyped, and asserted, so a class rename fails here.
const CHIP_OK =
  'group inline-flex items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono'
const CHIP_WARN = 'bg-warn-subtle border border-warn text-warn-fg'
const LABEL_SPAN = 'text-text text-[11px]'
const ROW_TAIL = 'block text-[11px] font-mono text-text truncate'
for (const [name, cls] of Object.entries({ CHIP_OK, CHIP_WARN, LABEL_SPAN, ROW_TAIL })) {
  if (!editor.includes(cls)) throw new Error(`${name} no longer matches the component: ${cls}`)
}

// pathParts() in AgentSkillsEditor.tsx pops trailing `skills` segments, so a mock that
// spells one renders a label the component cannot produce. Apply the same rule here and
// assert it bit, rather than transcribing the expected output by hand.
const tail = w => {
  const parts = w.split('/').filter(Boolean)
  while (parts.length > 1 && parts[parts.length - 1] === 'skills') parts.pop()
  return parts.join('/')
}
if (tail('packages/PkgA/skills') !== 'packages/PkgA') throw new Error('tail rule drifted')
const where = w => t.copy_identifier_label.replaceAll('{{where}}', tail(w))
const countNote = t.mapping_unresolved_count_one.replaceAll('{{count}}', '1')

const css = readdirSync(join(web, 'dist/assets'))
  .filter(f => f.endsWith('.css'))
  .map(f => ({ f, n: readFileSync(join(web, 'dist/assets', f), 'utf8') }))
  .sort((a, b) => b.n.length - a.n.length)[0]
if (!css || !css.n.includes('bg-warn-subtle')) {
  throw new Error('compiled stylesheet missing or lacks the warn tokens; run the website build')
}

const warnGlyph =
  '<svg class="lucide-inline text-warn-fg" width="13" height="13" viewBox="0 0 24 24" ' +
  'fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 ' +
  '1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4"/>' +
  '<path d="M12 17h.01"/></svg>'

const chip = (name, tail, unresolved) =>
  `<span class="${CHIP_OK} ${unresolved ? CHIP_WARN : 'bg-bg-elevated border border-border text-text'}">` +
  (unresolved ? warnGlyph : '') +
  `${name}<span class="${LABEL_SPAN}">${tail}</span></span>`

const row = (name, desc, tail) =>
  '<div class="w-full text-left px-2 py-1.5 rounded-md">' +
  `<span class="block text-[13px] font-mono text-text truncate">${name}</span>` +
  `<span class="block text-[11px] text-muted truncate">${desc}</span>` +
  `<span class="${ROW_TAIL}">${tail}</span></div>`

const html = `<!doctype html><html><head><meta charset="utf-8"><style>${css.n}</style>
<style>body{padding:18px;font-family:ui-sans-serif,system-ui,sans-serif}
.cap{font:11px ui-monospace,monospace;color:#8b8b8b;margin:14px 0 6px}</style>
</head><body class="bg-bg text-text">
<div class="cap">mapped chips — the label names which installed copy is bound</div>
<div class="flex items-center gap-2 flex-wrap">
  ${chip('shared-skill', where('packages/PkgA/skills'), false)}
  ${chip('shared-skill', where('packages/PkgB/skills'), false)}
</div>
<div class="cap">picker rows — same label, third line, for two otherwise identical rows</div>
<div class="max-w-md border border-border rounded-md bg-bg-elevated">
  ${row('shared-skill', 'A skill bundled by more than one package', where('packages/PkgA/skills'))}
  ${row('shared-skill', 'A skill bundled by more than one package', where('packages/PkgB/skills'))}
</div>
<div class="cap">mapped but no longer installed — warn chip, tooltip copy, and the count note</div>
<div class="flex items-center gap-2 flex-wrap">
  ${chip('shared-skill', where('packages/PkgB/skills'), true)}
</div>
<div class="text-[11px] text-muted mt-1.5">${t.mapping_unresolved}</div>
<div class="text-[11px] text-warn-fg mt-1.5">${countNote}</div>
</body></html>`

const page = `${OUT}.html`
writeFileSync(page, html)
// Spawned with an EMPTY library path: node puts its own older libstdc++ on the child's path and
// chrome then dies on a GLIBCXX version, while the same command from a shell runs.
try {
  execFileSync(
    chromium.executablePath(),
    ['--headless', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
     '--window-size=760,450', `--screenshot=${OUT}`, `file://${page}`],
    { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, LD_LIBRARY_PATH: '' } }
  )
} catch (e) {
  if (typeof e.status !== 'number') {
    console.error(`could not launch Chromium at ${chromium.executablePath()}: ${e.code ?? e.message}`)
    console.error('install the Playwright browsers first: npx playwright install chromium')
    process.exit(1)
  }
  console.error(`chrome status=${e.status}: ${String(e.stderr ?? '').slice(0, 400)}`)
}

// The frame is judged by DECODING it, never by chrome's exit status: chrome reports success on
// stderr and has been seen exiting non-zero on a frame it wrote correctly.
const png = readFileSync(OUT)
const SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
if (!png.subarray(0, 8).equals(SIG)) throw new Error('not a PNG: signature mismatch')
if (png.subarray(12, 16).toString('ascii') !== 'IHDR') throw new Error('no IHDR chunk')
const width = png.readUInt32BE(16)
const height = png.readUInt32BE(20)
if (width < 400 || height < 200) throw new Error(`frame too small: ${width}x${height}`)
if (png.length < 5000) throw new Error(`frame is ${png.length} bytes -- probably blank`)
rmSync(page, { force: true })
console.log(`decoded ${OUT}: ${width}x${height}, ${png.length} bytes`)
console.log(`label: ${where('packages/PkgA/skills')}`)
console.log(`unresolved: ${t.mapping_unresolved} | ${countNote}`)
