/**
 * index.css is the stylesheet entry, and the `src/styles/` partials it imports
 * are the rest of that one stylesheet. Tailwind inlines each `@import` where it
 * stands, so the import list IS the cascade order. These pins keep that true:
 * every partial is imported exactly once, in the one spelling the source readers
 * splice, and only from index.css (a second import anywhere else would emit its
 * rules twice, the later copy winning); a partial carries no entry directive of
 * its own; and `readIndexCss()` (the text the source-reading tests measure) is the
 * entry with every partial spliced in, in order, each line traceable to its file.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { describe, expect, it } from 'vitest'
import { readIndexCss, whereInIndexCss } from './test/indexCssSource'

const SRC = resolve(__dirname)
const ENTRY = readFileSync(join(SRC, 'index.css'), 'utf8')
const IMPORTED = [...ENTRY.matchAll(/^@import "\.\/(styles\/[\w-]+\.css)";$/gm)].map((m) => m[1])

/** Every import specifier in a source file, resolved against that file's directory. */
function importedPaths(file: string): string[] {
  const text = readFileSync(file, 'utf8')
  const specs = file.endsWith('.css')
    ? [...text.matchAll(/@import\s+(?:url\(\s*)?["']([^"']+)["']/g)].map((m) => m[1])
    : [...text.matchAll(/\bimport\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]/g)].map((m) => m[1])
  return specs
    .filter((s) => s.startsWith('.'))
    .map((s) => relative(SRC, resolve(dirname(file), s.replace(/\?.*$/, ''))).split(sep).join('/'))
}

describe('index.css partials', () => {
  it('imports each partial once, in the spliced spelling, and every one exists', () => {
    expect(IMPORTED.length, 'index.css imports no styles/ partial').toBeGreaterThan(0)
    expect(new Set(IMPORTED).size, 'a partial is imported twice').toBe(IMPORTED.length)
    // Any other spelling (single quotes, url(), a media or layer suffix) still
    // ships, but readIndexCss() would not splice it and these pins would not see it.
    expect(importedPaths(join(SRC, 'index.css')).filter((p) => p.startsWith('styles/'))).toEqual(IMPORTED)
    for (const partial of IMPORTED) {
      const text = readFileSync(join(SRC, partial), 'utf8')
      expect(text.endsWith('\n'), `${partial} must end with a newline`).toBe(true)
    }
  })

  it('imports the partials from index.css only', () => {
    const elsewhere: string[] = []
    for (const entry of readdirSync(SRC, { recursive: true, withFileTypes: true })) {
      if (!entry.isFile() || !/\.(css|ts|tsx)$/.test(entry.name)) continue
      const file = join(entry.parentPath ?? entry.path, entry.name)
      if (file === join(SRC, 'index.css')) continue
      for (const target of importedPaths(file)) {
        if (IMPORTED.includes(target)) elsewhere.push(`${relative(SRC, file)} -> ${target}`)
      }
    }
    expect(elsewhere).toEqual([])
  })

  it('keeps the entry directives out of every partial', () => {
    // An @source line resolves against the file that holds it, and the edition
    // seam only rewrites index.css, so these belong to the entry alone.
    for (const partial of IMPORTED) {
      const text = readFileSync(join(SRC, partial), 'utf8')
      expect(text, partial).not.toMatch(/^\s*@(?:import|source|tailwind)\b/m)
      expect(text, partial).not.toContain('@kirocrew-edition-source')
    }
  })

  it('splices every partial into readIndexCss() at its import, in order', () => {
    const sheet = readIndexCss()
    expect(sheet, 'an @import of a partial survived the splice').not.toMatch(/^@import "\.\/styles\//m)
    let from = 0
    for (const partial of IMPORTED) {
      const text = readFileSync(join(SRC, partial), 'utf8')
      const at = sheet.indexOf(text, from)
      expect(at, `${partial} is missing from readIndexCss(), or out of import order`).toBeGreaterThanOrEqual(from)
      from = at + text.length
    }
    expect(sheet.length).toBeGreaterThan(ENTRY.length)
  })

  it('traces lines of readIndexCss() to the file and line they come from', () => {
    const sheet = readIndexCss().split('\n').slice(0, -1)
    const lineAt = (where: string) => {
      const [file, at] = where.split(':')
      return { file, text: readFileSync(join(SRC, file), 'utf8').split('\n')[Number(at) - 1] }
    }
    // Every 50th line, plus both ends: each partial (all over 100 lines) is probed.
    const probes = [...Array.from({ length: Math.ceil(sheet.length / 50) }, (_, k) => k * 50 + 1), sheet.length]
    const seen = new Set<string>()
    for (const n of probes) {
      const { file, text } = lineAt(whereInIndexCss(n))
      seen.add(file)
      expect(text, `line ${n} -> ${whereInIndexCss(n)}`).toBe(sheet[n - 1])
    }
    expect([...seen].sort()).toEqual(['index.css', ...IMPORTED].sort())
  })
})
