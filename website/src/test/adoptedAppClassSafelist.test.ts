import { describe, it, expect, beforeAll } from 'vitest'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { gzipSync } from 'node:zlib'
import { compile } from '@tailwindcss/node'
import { emittedClasses } from '../../scripts/check-phantom-classes.mjs'
import {
  SAFELIST_PATH,
  parseSafelist,
  readPins,
  renderSafelist,
  treeCandidates,
  utilitiesOf,
  wheelCopyCandidates,
  wheelCopyFiles,
} from '../../scripts/gen-adopted-app-classes.mjs'

/**
 * The adopted-app class safelist, `src/tailwind-adopted-app-classes.css`.
 *
 * A former built-in that installs from its own repository ships a UI bundle with
 * no stylesheet; its Tailwind utilities come from the dashboard's compiled sheet,
 * which holds only the classes `src/index.css` finds in `src/`. While core still
 * carries the app's wheel copy under `src/apps/<name>/`, that copy keeps the
 * classes compiled. Deleting it drops every class nothing else uses, and the
 * adopted app renders unstyled on every host. The safelist lists those classes
 * as `@source inline` candidates so the sheet keeps them without the copy.
 *
 * What fails here:
 *  - a wheel copy of a pinned app uses a utility the list does not hold, so the
 *    deletion of that copy cannot silently drop it;
 *  - the list drops a class the base branch held (append-only: the copy it came
 *    from may be gone, and a host may still run the pin that uses it);
 *  - `adoption-pins.json` names a commit the list was not generated against, so a
 *    re-pin cannot skip the scan of its tree;
 *  - the list stops reaching the compiled sheet.
 *
 * The fix for the first and third is `npm run gen:adopted-app-classes`.
 */

const REPO = join(__dirname, '..', '..', '..')
const WEBSITE = join(REPO, 'website')
const text = readFileSync(SAFELIST_PATH, 'utf8')
const listed = parseSafelist(text)
const listedSet = new Set(listed.classes)
const REGEN = 'run `npm run gen:adopted-app-classes` in website/ and commit the result'

describe('adopted-app class safelist', () => {
  it('is imported by the Tailwind header of index.css, ahead of the emitted utilities', () => {
    const entry = readFileSync(join(WEBSITE, 'src', 'index.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
    const at = entry.indexOf('@import "./tailwind-adopted-app-classes.css";')
    expect(at, 'index.css must import the safelist').toBeGreaterThan(-1)
    expect(at).toBeLessThan(entry.indexOf('@tailwind utilities source(none);'))
  })

  it("is exactly the generator's output for its own classes and pins", () => {
    // A hand edit that reorders, duplicates or reformats a line fails here; the
    // classes are sorted and unique, so the append-only check below compares sets.
    expect(listed.classes.length).toBeGreaterThan(500)
    expect(text).toBe(renderSafelist(listed))
    expect(listed.classes).toEqual([...listedSet].sort())
  })

  it('was generated against every commit adoption-pins.json pins', () => {
    const pinned = readPins().map((p: { commit: string }) => p.commit).sort()
    expect(listed.commits.slice().sort(), `the pin table moved: ${REGEN}`).toEqual(pinned)
  })

  it("covers every utility a pinned app's wheel copy under src/apps/ uses", async () => {
    const missing: Record<string, string[]> = {}
    let scanned = 0
    for (const { name } of readPins()) {
      if (wheelCopyFiles(name).length === 0) continue
      scanned += 1
      const gap = (await utilitiesOf(wheelCopyCandidates(name))).filter((c: string) => !listedSet.has(c))
      if (gap.length) missing[name] = gap
    }
    // While any copy exists the scan must have read it; an empty scan that
    // passes is the failure this gate exists to prevent.
    expect(scanned).toBeGreaterThan(0)
    expect(missing, `a wheel copy uses classes the safelist lacks: ${REGEN}`).toEqual({})
  })

  describe('reaches the compiled sheet', () => {
    // The phantom-classes gate's oracle shell: the app's own theme bridge, no
    // template scan, so the only candidates are the ones handed in.
    const SHELL = [
      '@import "tailwindcss/theme.css" layer(theme);',
      '@import "tw-animate-css";',
      '@import "./src/tailwind-theme.css";',
    ]
    let viaInline = ''
    let viaBuild = ''
    beforeAll(async () => {
      const inline = await compile(
        [...SHELL, '@import "./src/tailwind-adopted-app-classes.css";', '@tailwind utilities source(none);'].join('\n'),
        { base: WEBSITE, onDependency() {} },
      )
      viaInline = inline.build([])
      const direct = await compile([...SHELL, '@tailwind utilities source(none);'].join('\n'), {
        base: WEBSITE,
        onDependency() {},
      })
      viaBuild = direct.build(listed.classes)
    })

    it('emits a rule for every listed class', async () => {
      // A Tailwind upgrade that stops emitting a utility an adopted bundle uses
      // fails here instead of leaving that app unstyled.
      const emitted = await emittedClasses(listed.classes)
      expect(listed.classes.filter((c) => !emitted.has(c))).toEqual([])
    })

    it('through @source inline exactly as if the classes were in a scanned file', () => {
      expect(viaInline.length).toBeGreaterThan(10000)
      expect(viaInline).toBe(viaBuild)
      expect(viaInline).toContain('.hover\\:text-\\[var\\(--accent\\)\\]:hover')
    })
  })

  it('never drops a class the base branch listed', () => {
    // CI's unit-test job exports the PR base as I18N_BASE_REF and fetches it
    // (.github/scripts/resolve-i18n-base.sh, whose name is historical: it serves
    // any diff-scoped gate). Unset is a bare local run with nothing to compare.
    const baseRef = process.env.I18N_BASE_REF
    if (!baseRef) return
    const git = (args: string[]) =>
      execFileSync('git', args, { cwd: REPO, encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'] })
    // A configured base that does not resolve is a broken gate, so it throws.
    git(['rev-parse', '--verify', `${baseRef}^{commit}`])
    let from: string
    try {
      from = git(['merge-base', baseRef, 'HEAD']).trim()
    } catch {
      from = baseRef
    }
    const rel = relative(REPO, SAFELIST_PATH).split('\\').join('/')
    let before: string
    try {
      before = git(['show', `${from}:${rel}`])
    } catch {
      return // the base predates the list
    }
    const dropped = parseSafelist(before).classes.filter((c: string) => !listedSet.has(c))
    expect(dropped, 'the safelist is append-only: restore these lines').toEqual([])
  })

  it('reads only ui/src/ of a pinned tree, test files excluded, long names included', () => {
    // A hand-built archive in the shape codeload serves: one top directory, a pax
    // `path` record for a name over 100 bytes, and files the scan must skip.
    const block = (name: string, size: number, type: string) => {
      const h = Buffer.alloc(512)
      h.write(name.slice(0, 100), 0)
      h.write(size.toString(8).padStart(11, '0'), 124)
      h.write(type, 156)
      return h
    }
    const pad = (b: Buffer) => Buffer.concat([b, Buffer.alloc((512 - (b.length % 512)) % 512)])
    const entry = (name: string, content: string) => {
      const data = Buffer.from(content)
      if (name.length <= 100) return [block(name, data.length, '0'), pad(data)]
      const record = ` path=${name}\n`
      const pax = Buffer.from(`${record.length + String(record.length + 2).length}${record}`)
      return [block('pax', pax.length, 'x'), pad(pax), block('trimmed', data.length, '0'), pad(data)]
    }
    const top = 'app-0123456789abcdef'
    const long = `${top}/ui/src/${'nested/'.repeat(14)}Deep.tsx`
    expect(long.length).toBeGreaterThan(100)
    const archive = gzipSync(
      Buffer.concat([
        ...entry(`${top}/ui/src/Page.tsx`, '<div className="bg-[var(--probe-page)]" />'),
        ...entry(long, '<div className="bg-[var(--probe-deep)]" />'),
        ...entry(`${top}/ui/src/Page.test.tsx`, '<div className="bg-[var(--probe-test)]" />'),
        ...entry(`${top}/ui/tests/helper.tsx`, '<div className="bg-[var(--probe-helper)]" />'),
        ...entry(`${top}/backend/routes.ts`, '"bg-[var(--probe-backend)]"'),
        Buffer.alloc(1024),
      ]),
    )
    const { candidates, files } = treeCandidates(archive)
    expect(files).toBe(2)
    const probes = candidates.filter((c: string) => c.startsWith('bg-[var(--probe-')).sort()
    expect(probes).toEqual(['bg-[var(--probe-deep)]', 'bg-[var(--probe-page)]'])
  })
})
