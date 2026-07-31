/**
 * Translation keys must be literals, so that tooling can see them.
 *
 * A key assembled at runtime — `i18nT(prefix + name)`, `` i18nT(`nav.${id}`) `` —
 * is invisible to every static tool in the ecosystem. Extractors do not find it,
 * unused-key linters report it as dead, and a pruning pass deletes it. The failure
 * is then silent at the call site, because i18next returns the key string itself
 * rather than throwing: the UI shows `pages.settings.aboutPanel.update_error_offline`
 * where a sentence should be.
 *
 * That is not a hypothetical. `AboutPanel.tsx` carries a comment recording a
 * missing key taking the whole Settings panel down through the error boundary,
 * and its keys were assembled from a prefix until this guard went in.
 *
 * ## The rule
 *
 * Every `i18nT()` first argument is a string literal, with one allowlisted
 * exception documented below. The industry answer to a dynamic key is *don't
 * construct one* — write an `as const` map from your enum to full literal keys and
 * index it. Tooling then sees ordinary literals, and the lookup stays one
 * expression. `AboutPanel`'s `UPDATE_ERROR_KEYS` is the worked example.
 *
 * ## Why a test and not an ESLint rule
 *
 * Same reach, no new plugin infrastructure, and it matches what this suite already
 * does elsewhere — `englishIdentity.test.ts` reads codemod source for the same
 * kind of assertion. If this grows more rules it should graduate to a local
 * ESLint plugin; one rule does not justify one.
 */

import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = join(__dirname, '..')

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    // `t.ts` declares `i18nT`; its signature is not a call site.
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry) && entry !== 't.ts') out.push(full)
  }
  return out
}

/**
 * Strip comments before scanning.
 *
 * Without this, every prose mention of `i18nT()` in a doc comment reports as a
 * dynamic call — and this codebase documents itself heavily, so that is dozens of
 * false positives in `LanguageProvider.tsx` and `t.ts` alone. Regex comment
 * stripping is imperfect for a string containing `//`, but the consequence here is
 * only a missed scan of one line, and the assertion is a floor.
 */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:'"\`\\])\/\/[^\n]*/g, (m, p) => p + ' '.repeat(m.length - p.length))
}

/**
 * `i18nT(` whose key is ASSEMBLED — concatenated or interpolated.
 *
 * The defect is assembly, not indirection. `i18nT(ap + 'update_error_offline')` and
 * `` i18nT(`nav.${id}`) `` produce a key that exists nowhere in the source, so no
 * extractor or unused-key tool can find it. By contrast
 * `i18nT(STATUS_LABEL_KEY[st])` indexes a literal map — every key is still written
 * out in full a few lines above, greppable and extractable — so it is fine, and is
 * the recommended shape.
 *
 * Matched across newlines because calls are often wrapped. The argument region is
 * bounded at the first `)` or `,`, which is imprecise for a nested call but errs
 * toward reporting.
 */
const ASSEMBLED_KEY = /\bi18nT\(\s*([^),]*)/g

function findAssembledKeys(source: string): number[] {
  const clean = stripComments(source)
  const hits: number[] = []
  for (const match of clean.matchAll(ASSEMBLED_KEY)) {
    const arg = match[1]
    const concatenated = /\+/.test(arg)
    const interpolated = arg.trimStart().startsWith('`') && arg.includes('${')
    if (concatenated || interpolated) {
      hits.push(clean.slice(0, match.index).split('\n').length)
    }
  }
  return hits
}

describe('translation keys are literals', () => {
  const files = walk(SRC)

  it('finds source files to scan', () => {
    // Guards against the walker silently matching nothing — a green suite that
    // scanned zero files is the failure mode this whole file exists to prevent.
    expect(files.length).toBeGreaterThan(300)
  })

  it('no i18nT() call assembles its key from parts', () => {
    const offenders: string[] = []

    for (const file of files) {
      const rel = relative(SRC, file).split('\\').join('/')
      const source = readFileSync(file, 'utf-8')
      const sourceLines = source.split('\n')
      for (const lineNo of findAssembledKeys(source)) {
        offenders.push(`${rel}:${lineNo}  ${(sourceLines[lineNo - 1] ?? '').trim()}`)
      }
    }

    expect(
      offenders,
      'A key built by concatenation or interpolation exists nowhere in the source, so ' +
        'extractors and unused-key tooling cannot see it, and it renders as the raw key ' +
        'when it goes missing. Write an `as const` map from your enum to full literal keys ' +
        'and index that instead — see `UPDATE_ERROR_KEYS` in pages/settings/AboutPanel.tsx ' +
        'or `STATUS_LABEL_KEY` in pages/chat/McpToolsPanel.tsx.',
    ).toEqual([])
  })

})
