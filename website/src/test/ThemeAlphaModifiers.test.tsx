/**
 * An opacity modifier on an ARBITRARY Tailwind value emits no CSS at all.
 *
 * `border-[var(--warn)]/40` looks like it should work and does not: Tailwind
 * cannot inject an alpha channel into an arbitrary value, so the whole
 * declaration is dropped. The visible result is worse than "no tint" — a bare
 * `border` then falls back to Preflight's `#e5e7eb`, i.e. a bright white line in
 * every dark theme, and a dropped `bg-` leaves the surface unfilled.
 *
 * The token form compiles, because `tailwind.config.js` wraps each theme colour
 * in `withAlpha`: `border-warn/40` emits
 * `color-mix(in srgb, var(--warn) calc(0.4 * 100%), transparent)`.
 *
 * This is a whole DEFECT CLASS, not a style preference, and it is invisible to
 * every other gate: `tsc` sees a valid string, eslint has no opinion, and the
 * class silently vanishes at build time with no warning. It reached three
 * surfaces before anyone noticed. So it is pinned at zero here rather than fixed
 * one instance at a time.
 *
 * If a new theme colour genuinely needs an alpha, add the token to
 * `tailwind.config.js` (wrapped in `withAlpha`) and use `token/NN`.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..')

/** `<utility>-[var(--token)]/<alpha>` — the form that compiles to nothing. */
const ARBITRARY_WITH_ALPHA = /[a-z-]+-\[var\(--[a-z0-9-]+\)\]\/\d+/g

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === '.vitest-reports') continue
    const p = join(dir, entry)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(p)
  }
  return out
}

describe('theme colour alpha modifiers', () => {
  it('never applies an opacity modifier to an arbitrary var() value', () => {
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      const src = readFileSync(file, 'utf8')
      src.split('\n').forEach((line, i) => {
        for (const hit of line.match(ARBITRARY_WITH_ALPHA) ?? []) {
          offenders.push(`${file.slice(SRC.length + 1)}:${i + 1}  ${hit}`)
        }
      })
    }
    expect(
      offenders,
      'These compile to NOTHING — a bare `border` then shows Preflight #e5e7eb (a white\n'
      + 'line in dark themes) and a dropped `bg-` leaves the surface unfilled. Use the\n'
      + 'token form instead: border-[var(--warn)]/40 -> border-warn/40.\n\n'
      + offenders.join('\n'),
    ).toEqual([])
  })
})
