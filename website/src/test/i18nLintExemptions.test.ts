/**
 * The exemptions in `eslint.i18n.config.js`, asserted through the real lint.
 *
 * ## Why this file exists
 *
 * `eslint-plugin-i18next` compiles every `words.exclude` entry with
 * `generateFullMatchRegExp`, which appends `$` unless the source already ends in
 * one. A pattern written as a PREFIX therefore matches only itself:
 *
 *   `^https?://`  ->  /^^https?:\/\/$/    matched only the bare scheme
 *   `^[.~]?/`     ->  /^^[.~]?\/$/        matched only `/`, `./`, `~/`
 *
 * Both shipped like that and exempted nothing. Nothing failed, because the URL /
 * path class was silently carried by the lowercase-token pattern instead — whose
 * character class holds no `*`, `?`, `_`, `=`, `&` or capital. So `/api/chat/*`
 * was reported as untranslated user copy while `/api/chat` on the line above was
 * not, and the difference was one character nobody could see in the config.
 *
 * That failure mode is invisible in two directions at once: existing code is
 * frozen in the ledgers (and both whole-repo ledger checks are report-only), and
 * the diff-scoped gates fail only lines a branch WROTE. So the authors who could
 * have noticed never saw it, and the authors who hit it had no way to tell a
 * config bug from a real finding.
 *
 * These tests lint real source text with the real config, so they fail if a
 * pattern stops matching what it claims to — including after a dependency bump
 * that changes how patterns are compiled.
 */

import { describe, it, expect } from 'vitest'
import { ESLint } from 'eslint'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import baseConfig from '../../eslint.i18n.config.js'

const WEBSITE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

/** Lint one snippet exactly as the diff-scoped gates do. */
async function lint(code: string): Promise<string[]> {
  const engine = new ESLint({
    cwd: WEBSITE_ROOT,
    overrideConfigFile: 'eslint.i18n.strict.config.js',
    allowInlineConfig: false,
  })
  const [result] = await engine.lintText(code, {
    filePath: path.join(WEBSITE_ROOT, 'src/probe.tsx'),
  })
  return result.messages.map(m => m.message)
}

/** The `words.exclude` list actually in force. */
function wordsExclude(): Array<string | RegExp> {
  for (const block of baseConfig as Array<Record<string, unknown>>) {
    const rules = block.rules as Record<string, unknown> | undefined
    const options = rules?.['i18next/no-literal-string']
    if (Array.isArray(options) && options[1] && typeof options[1] === 'object') {
      const words = (options[1] as { words?: { exclude?: Array<string | RegExp> } }).words
      if (words?.exclude) return words.exclude
    }
  }
  throw new Error('no words.exclude found in eslint.i18n.config.js')
}

/**
 * Whether `pattern` is genuinely end-anchored once the plugin compiles it.
 *
 * A raw `endsWith('$')` is not enough, and the gap is exactly the class this
 * guard exists to catch: `'^Price: \\$'` ends in `$` as source text, but that `$`
 * is ESCAPED, so `generateFullMatchRegExp` sees a terminal `$` and appends
 * nothing — leaving `/^^Price: \$/`, which matches `'Price: $5 Save changes'`.
 * That is a bare-prefix pattern smuggled past the check.
 *
 * Three ways a pattern can be end-unanchored, all of them checked:
 *
 *   1. no trailing `$` at all — the original bug;
 *   2. a trailing `$` that is escaped — count the backslashes before it, an even
 *      number (including none) leaves it an anchor, an odd number escapes it;
 *   3. a TOP-LEVEL alternation — `'^X|.*$'` ends in an unescaped `$`, yet only
 *      its second branch is anchored and that branch matches every string. A `|`
 *      inside a group or a character class is fine (`^(Ctrl|Alt)$`), so depth has
 *      to be tracked rather than the character merely searched for.
 *
 * A `RegExp` entry is checked the same way via its `source`: the plugin returns a
 * `RegExp` untouched, so one written as a bare prefix is unanchored exactly as
 * written.
 */
function endAnchored(pattern: string | RegExp): boolean {
  const source = pattern instanceof RegExp ? pattern.source : pattern
  if (!source.endsWith('$')) return false
  const escapes = /(\\*)\$$/.exec(source)?.[1].length ?? 0
  if (escapes % 2 !== 0) return false
  return !hasTopLevelAlternation(source)
}

/** A `|` outside every group and character class, which splits the whole pattern. */
function hasTopLevelAlternation(source: string): boolean {
  let group = 0
  let inClass = false
  for (let i = 0; i < source.length; i += 1) {
    const ch = source[i]
    if (ch === '\\') { i += 1; continue }
    if (inClass) { if (ch === ']') inClass = false; continue }
    if (ch === '[') inClass = true
    else if (ch === '(') group += 1
    else if (ch === ')') group -= 1
    else if (ch === '|' && group === 0) return true
  }
  return false
}

describe('words.exclude is compiled full-string', () => {
  it('has no bare-prefix pattern, because the plugin anchors both ends', () => {
    // The invariant that makes the trap unrepeatable. `generateFullMatchRegExp`
    // appends `$` to any pattern that lacks one, so a pattern not ending in an
    // unescaped `$` is silently end-anchored and almost never means what its
    // author wrote. Spell the tail out (`\S*$`, `.*$`, an explicit class) instead.
    expect(wordsExclude().filter(p => !endAnchored(p))).toEqual([])
  })

  it('the guard catches an ESCAPED trailing dollar, not just a missing one', () => {
    // Without this, the check above passes on a pattern that is still a prefix.
    expect(endAnchored('^Price: \\$')).toBe(false)
    expect(endAnchored('^[.~]?/')).toBe(false)
    expect(endAnchored('^[.~]?/\\S*$')).toBe(true)
    // An even run of backslashes escapes the backslash, not the dollar.
    expect(endAnchored('^a\\\\$')).toBe(true)
    expect(endAnchored('^a\\\\\\$')).toBe(false)
  })

  it('the guard catches a top-level alternation whose other branch is open', () => {
    expect(endAnchored('^X|.*$')).toBe(false)
    // A `|` inside a group or a class is not an escape hatch — this is a real
    // pattern in the list and must stay valid.
    expect(endAnchored('^(Ctrl|Alt|Shift|Cmd|Win)$')).toBe(true)
    expect(endAnchored('^[a|b]+$')).toBe(true)
  })

  it('the guard reads a RegExp entry rather than waving it through', () => {
    // `generateFullMatchRegExp` returns a RegExp untouched, so one written as a
    // bare prefix is unanchored exactly as written.
    expect(endAnchored(/^https?:\/\/\S*$/)).toBe(true)
    expect(endAnchored(/^https?:\/\//)).toBe(false)
  })
})

describe('paths, routes and URLs are exempt', () => {
  // Every string here is a real shape from this codebase. Each was REPORTED as
  // untranslated user copy before the two prefix patterns were repaired.
  const machine = [
    "'/api/chat/*'", // permission-scope wildcard (PR #1276)
    "'/api/apps/ops-mission-control/*'",
    "'/api/file_read'", // underscore
    "'/api/chat?slot=1'", // query string
    "'/api/v1/Sessions'", // capital
    "'/settings?tab=chat'",
    "'/run?id=' + rid",
    "'/-/merge_requests/'",
    "'https://github.com/kirodotdev/KiroCrew/blob/main/README.md'",
    "'~/.kiro/steering'",
    "'./node_modules/@tailwindcss/browser/dist/index.global.js'",
  ]

  for (const literal of machine) {
    it(`stays quiet on ${literal}`, async () => {
      expect(await lint(`export const PROBE = [${literal}]`)).toEqual([])
    })
  }

  it('still reports copy that merely starts with a slash-free word', async () => {
    // The widened patterns must not swallow prose. `\S*` cannot match a space,
    // which is what keeps a sentence out.
    const messages = await lint(`export const PROBE = ['/ Delete this item', 'Save changes']`)
    expect(messages).toHaveLength(2)
  })
})

describe('CSS selector lists are exempt', () => {
  it('stays quiet on a list mixing type and attribute selectors', async () => {
    // Real site: `INTERACTIVE_SEL` in lib/dragGaps.ts, handed to
    // `header.querySelectorAll`. Translating it would stop the drag-gap
    // measurement finding any control, which silently widens a drag region over
    // a button and swallows its clicks.
    expect(await lint(
      `export const PROBE = ['a,button,input,select,textarea,[role="button"],[tabindex]']`,
    )).toEqual([])
  })

  it('stays quiet on an all-bracketed list', async () => {
    // The narrower form this shape grew out of; it must keep matching.
    expect(await lint(`export const PROBE = ['[role="dialog"],[data-x]']`)).toEqual([])
  })

  it('still reports comma-joined words with no bracket', async () => {
    // The bracket requirement is the whole tightness argument: without it this
    // entry would become a general "lowercase words joined by commas" exemption.
    expect(await lint(`export const PROBE = ['save,delete']`)).toHaveLength(1)
  })

  it('still reports a sentence that merely contains a bracket', async () => {
    // Every member must match end to end, and a prose member carries spaces.
    const messages = await lint(`export const PROBE = ['Select an item [optional]']`)
    expect(messages).toHaveLength(1)
  })
})

describe('string comparison is a position exemption', () => {
  it('stays quiet on a literal compared with startsWith', async () => {
    // The argument is the value being compared AGAINST, so the call cannot
    // render it — the same reason the plugin already exempts `x === 'lit'`.
    expect(await lint(`export const f = (e: string) => e.startsWith('backing off')`)).toEqual([])
  })

  it('stays quiet on includes, endsWith and indexOf', async () => {
    expect(await lint(`
      export const f = (e: string) => e.includes('building dist')
        || e.endsWith('.tar.gz') || e.indexOf('Running: ') === 0
    `)).toEqual([])
  })

  it('still reports the same string rendered as copy', async () => {
    // The exemption is scoped to the comparison position, not to the value. If
    // it leaked to the value, this would be the regression that hides real copy.
    const messages = await lint(`export const LABEL = 'backing off'`)
    expect(messages).toHaveLength(1)
  })

  it('still reports a literal passed to an unrelated single-word callee', async () => {
    expect(await lint(`export const f = (s: string) => s.padStart(4, 'Waiting for input')`))
      .toHaveLength(1)
  })

  // A callee exemption suppresses the WHOLE call subtree, receiver included — the
  // plugin pushes the verdict on `CallExpression` enter and tests it with `.some()`.
  // With bare method names these three passed at zero tolerance, because
  // `withDottedPrefix`'s `(?:.*\.)?` absorbs any receiver. The anchored pattern
  // requires an identifier/property-chain receiver, which reports them again.
  it('still reports an inline table of copy tested for membership', async () => {
    const messages = await lint(
      `export const f = (x: string) => ['Save changes', 'Delete item'].includes(x)`,
    )
    expect(messages).toHaveLength(2)
  })

  it('still reports copy in a parenthesised receiver', async () => {
    const messages = await lint(
      `export const f = (c: boolean) => (c ? 'Save changes' : 'Delete item').startsWith('S')`,
    )
    expect(messages).toHaveLength(2)
  })

  it('still reports copy passed to a call that becomes the receiver', async () => {
    const messages = await lint(
      `export const f = (g: (s: string) => string) => g('Save changes').includes('x')`,
    )
    expect(messages).toHaveLength(1)
  })

  it('keeps a zero-argument link in the receiver chain exempt', async () => {
    // `text.trim().startsWith('<svg')` is a real site in this codebase. Empty
    // parens cannot hold a literal, so admitting them costs no coverage.
    expect(await lint(`export const f = (t: string) => t.trim().startsWith('<svg')`)).toEqual([])
  })

  it('keeps an optional-chained property receiver exempt', async () => {
    expect(await lint(
      `export const f = (e: { types?: string[] }) => e.types?.includes('Files')`,
    )).toEqual([])
  })

  it('keeps a receiver chain the formatter broke across lines exempt', async () => {
    // The plugin matches the callee's SOURCE TEXT, so a wrapped chain carries
    // newlines. Without `\s*` in the pattern the gate would depend on line width.
    expect(await lint(`
      export const f = (o: { p: { q: string } }) => o
        .p
        .q
        .startsWith('backing off')
    `)).toEqual([])
  })

  it('keeps an inline array of snake_case wire tokens exempt', async () => {
    // Real sites: ChatInput.tsx:579 and pages/knowledge/helpers.ts:5. These are
    // an inline-array receiver, so the anchored callee pattern reports them —
    // the lowercase_snake shape is what exempts them, which is where the
    // exemption belongs: the argument is a machine token by its own shape, not
    // by where it sits.
    expect(await lint(
      `export const f = (d: string) =>`
      + ` ['trust_command', 'trust_base', 'trust', 'trust_reads'].includes(d)`,
    )).toEqual([])
    expect(await lint(`export const f = (t: string) => ['design_doc', 'code_doc'].includes(t)`))
      .toEqual([])
  })

  it('still reports snake_case-shaped copy that is really a sentence', async () => {
    // The snake exemption is narrow: one lowercase run per underscore, no spaces.
    expect(await lint(`export const LABEL = 'save changes_now'`)).toHaveLength(1)
  })
})
