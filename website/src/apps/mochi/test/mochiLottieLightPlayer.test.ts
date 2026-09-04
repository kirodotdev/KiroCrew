/**
 * App code must load the LIGHT lottie player, never the full one.
 *
 * The full player (`lottie-web`'s package main, `build/player/lottie.js`)
 * carries the animation-expression compiler and a direct `eval()`. The apps
 * render imported appearance-pack JSON — third-party authored — in the
 * gateway's origin, request only the SVG renderer, and strip expressions
 * before loading, so nothing they draw needs the full build. A bare
 * `lottie-web` value import resolves to that full player and re-ships the
 * dead sink (~135 KB minified of expression compiler and unused renderers);
 * the light entry removes the sink instead of shipping it disarmed. See
 * #6549; crew-companion and mochi both made this call deliberately.
 *
 * Structural pin, same pattern as lottieTeardownGuard's setup.ts pin: the
 * property under guard is which module SPECIFIER value imports name, and that
 * is invisible at runtime under the suite's mocks (integration/setup.ts mocks
 * both specifiers identically), so assert on the source text. Reads happen
 * inside the tests so a resolution problem fails as a named assertion, not as
 * collection-time noise.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

// Resolved from cwd: vitest runs with cwd at the website root (where
// vite.config.ts lives). import.meta.url is not usable here — under happy-dom
// it carries the environment's http URL, not a file: URL.
const APPS_DIR = () => resolve(process.cwd(), 'src/apps')
const RENDERER = () =>
  resolve(process.cwd(), 'src/apps/mochi/src/renderer/LottieRenderer.tsx')

/** Every production (non-test) .ts/.tsx file under src/apps, recursively. */
function productionSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      return entry.name === 'test' ? [] : productionSources(full)
    }
    if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) return []
    return [full]
  })
}

/** Drop block and line comments so a prose mention cannot false-positive. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

describe('lottie player choice across src/apps', () => {
  it('mochi LottieRenderer value-imports the light player', () => {
    const source = readFileSync(RENDERER(), 'utf8')
    expect(source).toMatch(
      /import\s+lottie\s+from\s+'lottie-web\/build\/player\/lottie_light'/,
    )
  })

  it('no production module names the bare specifier outside a type-only import', () => {
    // The type-only import may — and should — stay on the package root: types
    // live there, not under build/player/. Only a VALUE binding pulls code
    // into the bundle, and every value path to it — static `from`, dynamic
    // `import()`, `require()`, a multi-line import with the specifier on its
    // own line — has to name the bare quoted specifier somewhere. Type-only
    // statements are removed as whole statements (not line-matched) so a
    // wrapped `import type {…} from 'lottie-web'` cannot false-positive.
    const offenders = productionSources(APPS_DIR()).flatMap((file) => {
      const withoutTypeOnly = stripComments(readFileSync(file, 'utf8')).replace(
        /(?:import|export)\s+type(?:(?!\bfrom\b)[\s\S])*?from\s*['"]lottie-web['"]/g,
        '',
      )
      const residual = withoutTypeOnly.match(/[^\n]*['"]lottie-web['"][^\n]*/g) ?? []
      return residual.map((snippet) => `${file}: ${snippet.trim()}`)
    })
    expect(offenders).toEqual([])
  })
})
