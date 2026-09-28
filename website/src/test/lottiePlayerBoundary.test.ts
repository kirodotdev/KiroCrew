/**
 * ONE file loads lottie-web, and it loads the LIGHT player.
 *
 * `components/appearancePacks/LottieRenderer.tsx` is the fenced player: it
 * refuses a document that names a remote image or font before `loadAnimation`,
 * because lottie-web resolves those by requesting them from the dashboard's own
 * authenticated origin, and it imports `lottie-web/build/player/lottie_light`,
 * which ships no expression compiler (the full build's is a direct `eval()`).
 * Both properties hold only while every Lottie document in the dashboard goes
 * through that file. Mochi once carried a second copy of the player — with the
 * light import but without the fence — and it was the one unfenced
 * `loadAnimation` in the tree until it became a re-export of core's (#10249).
 *
 * A second value import of `lottie-web` anywhere under `src/` is therefore a
 * second player to fence, and this test names it. Same shape as
 * `appearancePacksCoreBoundary.test.ts`: the property under guard is which
 * module SPECIFIER production code names, which is invisible at runtime under
 * the suite's mocks (integration/setup.ts mocks both specifiers identically),
 * so it is asserted on the source text. Tests are exempt — a test may mock or
 * probe either build — and a `type`-only import may stay on the package root,
 * because the types live there and a type import pulls no code into the bundle.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = resolve(__dirname, '..')

/** The one production module allowed to name lottie-web as a value. */
const PLAYER = 'components/appearancePacks/LottieRenderer.tsx'
const LIGHT = 'lottie-web/build/player/lottie_light'

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full))
      continue
    }
    if (!/\.(ts|tsx)$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue
    out.push(full)
  }
  return out
}

/** Drop block and line comments so a prose mention cannot false-positive. */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

/**
 * Every `lottie-web…` specifier a file names as a VALUE: a static `from`, a
 * dynamic `import()`, a `require()`, a multi-line import with the specifier on
 * its own line. Type-only statements are removed whole (not line-matched) so a
 * wrapped `import type {…} from 'lottie-web'` cannot count.
 */
function lottieValueSpecifiers(source: string): string[] {
  const withoutTypeOnly = stripComments(source).replace(
    /(?:import|export)\s+type(?:(?!\bfrom\b)[\s\S])*?from\s*['"]lottie-web[^'"]*['"]/g,
    '',
  )
  const out: string[] = []
  const re = /['"](lottie-web[^'"]*)['"]/g
  let m: RegExpExecArray | null
  while ((m = re.exec(withoutTypeOnly)) !== null) out.push(m[1])
  return out
}

describe('lottie-web is loaded by one fenced player', () => {
  const files = sourceFiles(SRC)
  const importers = files
    .map((file) => ({
      rel: file.slice(SRC.length + 1).replace(/\\/g, '/'),
      specifiers: lottieValueSpecifiers(readFileSync(file, 'utf-8')),
    }))
    .filter((entry) => entry.specifiers.length > 0)

  it('no production module outside the core renderer names lottie-web as a value', () => {
    const offenders = importers
      .filter((entry) => entry.rel !== PLAYER)
      .map((entry) => `${entry.rel} -> ${entry.specifiers.join(', ')}`)
    expect(offenders).toEqual([])
  })

  it('the core renderer imports the light player, and only the light player', () => {
    const player = importers.find((entry) => entry.rel === PLAYER)
    expect(player, `${PLAYER} must value-import lottie-web`).toBeDefined()
    expect([...new Set(player!.specifiers)]).toEqual([LIGHT])
  })

  it('scans a real set of production files, so a passing run is not a vacuous one', () => {
    // Without this, moving `src/` or renaming the renderer would empty the scan
    // above and it would keep reporting success while checking nothing.
    expect(files.length).toBeGreaterThan(100)
    expect(files.some((file) => file.slice(SRC.length + 1).replace(/\\/g, '/') === PLAYER)).toBe(true)
  })
})
