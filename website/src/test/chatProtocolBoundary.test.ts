/**
 * The marker protocol has exactly one home, and that home renders nothing.
 *
 * `[OPTIONS: …]` and `[STEERING steer-<id>: …]` are a contract between the agent's text
 * and whatever draws it. When the parsers sit inside a message component, only surfaces
 * that render that component can honour the contract: a transcript assembled any other
 * way prints the raw marker, and a util that needs to read options ends up importing a
 * React component to parse a string.
 *
 * Two properties keep that from coming back, and neither is visible to a type checker:
 * the protocol module imports no rendering, and no other non-test source defines the
 * markers a second time. Both are asserted here against the files on disk.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Separators normalized to `/` so the path comparisons below hold on Windows
 *  too. `join`/`resolve` emit `\` there, while the exclusions are spelled with
 *  `/` — without this, `startsWith(PROTOCOL + '/')` and the `src/test` filter
 *  never match, every test file counts as an offender, and the "only home"
 *  assertion fails for every Windows contributor while passing on CI's Linux
 *  runner. `fs` accepts either separator, so only the comparisons change. */
const posix = (p: string) => p.split('\\').join('/')

const SRC = posix(resolve(dirname(fileURLToPath(import.meta.url)), '..'))
const PROTOCOL = `${SRC}/app-sdk/protocol`

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = `${dir}/${name}`
    return statSync(p).isDirectory() ? walk(p) : [p]
  })
}

const read = (f: string) => readFileSync(f, 'utf-8')
const isSource = (f: string) => /\.tsx?$/.test(f)
const protocolFiles = walk(PROTOCOL).filter(isSource)

/** Regex-shaped marker definitions — `\[OPTIONS`, `\[OPTION`, `\[STEERING`. A test
 *  fixture contains the literal `[OPTIONS: a|b]`, which is deliberately NOT matched:
 *  only an escaped bracket means someone is defining the pattern rather than using it. */
const MARKER_DEFINITION = /\\\[(?:OPTIONS?|STEERING)/

describe('the marker protocol module', () => {
  it('exists as more than one file, so this test cannot pass vacuously', () => {
    expect(protocolFiles.length).toBeGreaterThan(1)
  })

  it('imports no rendering layer', () => {
    const offenders = protocolFiles.flatMap((f) => {
      const bad = [...read(f).matchAll(/^\s*import\s[^\n]*?from\s+'([^']+)'/gm)]
        .map((m) => m[1])
        .filter(
          (spec) =>
            /^(react|react-dom|framer-motion|lucide-react)(\/|$)/.test(spec) ||
            /\/(pages|components)\//.test(spec) ||
            spec.endsWith('.tsx'),
        )
      return bad.map((spec) => `${relative(SRC, f)} imports ${spec}`)
    })
    expect(offenders).toEqual([])
  })

  it('is the only non-test source that defines the markers', () => {
    const elsewhere = walk(SRC)
      .filter(isSource)
      .filter((f) => !f.startsWith(PROTOCOL + '/') && !f.includes(`${SRC}/test/`))
      .filter((f) => MARKER_DEFINITION.test(read(f)))
      .map((f) => relative(SRC, f))
    expect(elsewhere).toEqual([])
  })
})

describe('the protocol surface apps import', () => {
  it('exports every parser, so a rename cannot silently drop one', async () => {
    const surface = await import('../app-sdk/protocol')
    for (const name of [
      'parseOptions',
      'deriveFollowUpOptions',
      'extractSteeringAcks',
      'stripPartialOptionMarker',
    ]) {
      expect(surface, `protocol must export ${name}`).toHaveProperty(name)
    }
  })

  it('re-exports the types its own signatures use', () => {
    // A type export leaves no runtime trace, and `tsconfig.app.json` excludes `src/test` from the
    // build, so neither the property check above nor `tsc -b` can see one go missing. The barrel's
    // source can. Without these a caller cannot annotate a transcript or a parse result without
    // importing from the dashboard's own type tree.
    const barrel = read(join(PROTOCOL, 'index.ts'))
    for (const name of ['ChatMessage', 'ParsedOptions', 'FollowUpDerivation']) {
      expect(
        new RegExp(`export type \\{[^}]*\\b${name}\\b`).test(barrel),
        `protocol/index.ts must re-export the ${name} type`,
      ).toBe(true)
    }
  })

  it('is exported by the vendor stub apps actually resolve', () => {
    // `@kirocrew/app-sdk` resolves to a stub that re-exports the host module by NAME. A name the
    // barrel exports and the stub omits is not a missing convenience: the browser fails to
    // instantiate the module, so an app importing it does not load. Two hand-written lists cannot
    // be kept in agreement by review alone.
    const stub = read(resolve(SRC, '..', 'public', 'vendor', 'kirocrew-app-sdk.mjs'))
    // BOTH barrels: scoping this to the protocol barrel is what let four chat
    // exports sit in the app surface with no stub entry, each one a load-time
    // failure for any app that imported it.
    const barrels = [read(join(PROTOCOL, 'index.ts')), read(join(SRC, 'app-sdk', 'index.ts'))]
    // Value exports only — `export type` lines vanish at runtime and need no
    // stub entry, and `export type {…}` must not be mistaken for a value block.
    const values = barrels.flatMap(barrel => [
      ...[...barrel.matchAll(/^export \{([^}]+)\}/gm)]
        .flatMap(m => m[1].split(','))
        .map(s => s.trim().replace(/^default as /, '')),
      ...[...barrel.matchAll(/^export (?:async )?function (\w+)/gm)].map(m => m[1]),
    ]).filter(Boolean)
    expect(values.length).toBeGreaterThan(10)
    const missing = values.filter(name => !new RegExp(`\\b${name}\\b`).test(stub))
    expect(missing, 'vendor stub must re-export every value the app surface exports').toEqual([])
  })

  it('parses correctly even after the shared regex has been left mid-string', async () => {
    // The pattern is g-flagged, and `matchAll` seeds its internal clone from `lastIndex` — so a
    // single `.test()` elsewhere would otherwise make the scan start past the marker and return no
    // options, which renders the raw `[OPTIONS: …]` to the user with no buttons.
    const { OPTION_MARKER_RE } = await import('../app-sdk/protocol/optionMarker')
    const { parseOptions } = await import('../app-sdk/protocol')

    OPTION_MARKER_RE.test('elsewhere [OPTIONS: X]')  // `$`-anchored: must end at the bracket
    expect(OPTION_MARKER_RE.lastIndex).toBeGreaterThan(0)

    const parsed = parseOptions('Pick one [OPTIONS: Alpha|Beta]')
    expect(parsed.options).toEqual(['Alpha', 'Beta'])
    expect(parsed.text).toBe('Pick one')
  })

  it('keeps the mutable regex out of the app surface', () => {
    // Withdrawn from the barrel on purpose: an app cannot corrupt state it cannot reach.
    const barrel = read(join(PROTOCOL, 'index.ts'))
    const exported = [...barrel.matchAll(/^export \{([^}]+)\}/gm)]
      .flatMap(m => m[1].split(','))
      .map(v => v.trim())
    expect(exported).not.toContain('OPTION_MARKER_RE')
  })

  it('parses a marker without any component being rendered', async () => {
    const { parseOptions, extractSteeringAcks } = await import('../app-sdk/protocol')
    expect(parseOptions('Pick one [OPTIONS: Alpha|Beta]').options).toEqual(['Alpha', 'Beta'])
    expect(parseOptions('Pick one [OPTIONS: Alpha|Beta]').text).toBe('Pick one')
    const { cleaned, acks } = extractSteeringAcks('ok [STEERING steer-ab12: rebased first]')
    expect(acks).toEqual(['rebased first'])
    expect(cleaned).toBe('ok')
  })
})
