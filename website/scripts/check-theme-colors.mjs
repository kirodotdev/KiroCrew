#!/usr/bin/env node
/**
 * Theme customization-contract guardrail (ADVISORY).
 *
 * Flags raw color literals (`#rrggbb`, `rgb(...)`, `rgba(...)`) in
 * `website/src/**` outside the theme-definition files. Per the Customization
 * Contract, new UI must be themable at the color layer — style with the theme
 * CSS custom properties (`var(--bg)`, `var(--text)`, `var(--accent)`, …) or
 * Tailwind classes mapped to them, never a hardcoded literal.
 *
 * This is intentionally ADVISORY: the existing codebase (23 built-in themes,
 * SVG icons, session palettes, test mocks) contains many legitimate literals,
 * so the checker exits 0 by default and only reports. Pass `--strict` to exit
 * non-zero (for a future ratchet once the baseline is burned down). It is NOT
 * wired into the blocking CI gate yet — see website/docs/theming-contract.md.
 *
 * Usage:
 *   node scripts/check-theme-colors.mjs           # report, exit 0
 *   node scripts/check-theme-colors.mjs --strict  # report, exit 1 if any found
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const SRC = join(ROOT, 'src')

// Files where raw color literals are legitimate (theme definitions, sanitizer,
// palette data, generated code, and tests).
const EXCLUDE_SUBSTR = [
  'src/hooks/useTheme.tsx',
  'src/components/themeEditor.tsx',
  'src/index.css',
  'src/lib/cssSanitize.ts',
  'src/utils/sessionColors.ts',
]
const EXCLUDE_RE = [/\.test\.tsx?$/, /\/test\//, /\.gen\.ts$/, /\.d\.ts$/]

const SCAN_EXT = /\.(tsx?|css)$/
// #rgb / #rrggbb / #rrggbbaa, and rgb()/rgba()
const COLOR_RE = /#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3}(?:[0-9a-fA-F]{2})?)?\b|\brgba?\s*\(/

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    const st = statSync(p)
    if (st.isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

function excluded(rel) {
  if (EXCLUDE_SUBSTR.some((s) => rel.includes(s))) return true
  return EXCLUDE_RE.some((re) => re.test(rel))
}

const offenders = []
for (const file of walk(SRC)) {
  if (!SCAN_EXT.test(file)) continue
  const rel = relative(ROOT, file)
  if (excluded(rel)) continue
  const lines = readFileSync(file, 'utf8').split('\n')
  const hits = []
  lines.forEach((line, i) => {
    if (COLOR_RE.test(line)) hits.push(i + 1)
  })
  if (hits.length) offenders.push({ rel, count: hits.length, lines: hits })
}

const total = offenders.reduce((n, o) => n + o.count, 0)
const strict = process.argv.includes('--strict')

if (total === 0) {
  console.log('✅ theme-colors: no raw color literals found in scanned files.')
  process.exit(0)
}

offenders.sort((a, b) => b.count - a.count)
console.log(
  `theme-colors: ${total} raw color literal(s) across ${offenders.length} file(s) ` +
    `(advisory — see website/docs/theming-contract.md).`
)
for (const o of offenders.slice(0, 20)) {
  console.log(`  ${o.rel}: ${o.count}  [lines: ${o.lines.slice(0, 10).join(', ')}${o.lines.length > 10 ? ', …' : ''}]`)
}
if (offenders.length > 20) console.log(`  … and ${offenders.length - 20} more file(s)`)
console.log(
  '\nNew UI should use theme CSS vars (var(--bg)/--text/--accent, …), not literals. ' +
    'A new color role goes into BOTH ALLOWED_CSS_VARS (useTheme.tsx) and ' +
    '_THEME_CSS_VARS_SET (backend) in parity.'
)

process.exit(strict ? 1 : 0)
