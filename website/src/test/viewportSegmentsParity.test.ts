import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, posix, relative, sep, win32 } from 'node:path'
import { compile } from '@tailwindcss/node'
import ts from 'typescript'
import { FOLD_TWO_PANE_MIN, FOLD_TWO_PANE_QUERY, FOLD_X_QUERY } from '../hooks/useViewportSegments'

/** CSS and script must agree on "folded". FOLD_X_QUERY is the one fold query;
 *  the `md` variant and the top bar ladder repeat FOLD_TWO_PANE_QUERY verbatim,
 *  and no other file under src/ may write a segments query. */

const WEBSITE = join(__dirname, '..', '..')
const SRC = join(WEBSITE, 'src')
const THEME_FILE = 'tailwind-theme.css'
const HOOK_FILE = 'hooks/useViewportSegments.ts'
const CHROME_FILE = 'styles/shell-chrome.css'
/** The desktop top bar's collapse ladder: 768px, or the folded two-pane form. */
const LADDER_PRELUDE = `(min-width:768px), ${FOLD_TWO_PANE_QUERY}`

// CSS: string literals are matched first and kept, so a `/*` inside one cannot
// swallow the rule after it. CSS has no `//` comments, so an unquoted
// `url(https://...)` survives.
const CSS_STRING = String.raw`'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"`
const CSS_COMMENTS = new RegExp(`(${CSS_STRING})|\\/\\*[\\s\\S]*?\\*\\/`, 'g')

/** Every comment range the TypeScript parser itself sees: a regex can't tell a
 *  `//` or a backtick inside a regex literal, a URL in JSX text or a nested
 *  template from the real thing. */
function tsCommentRanges(text: string): ts.CommentRange[] {
  const file = ts.createSourceFile('f.tsx', text, ts.ScriptTarget.Latest, false, ts.ScriptKind.TSX)
  const seen = new Map<number, ts.CommentRange>()
  const add = (ranges: ts.CommentRange[] | undefined) => { for (const r of ranges ?? []) seen.set(r.pos, r) }
  const visit = (node: ts.Node) => {
    add(ts.getLeadingCommentRanges(text, node.getFullStart()))
    add(ts.getTrailingCommentRanges(text, node.getEnd()))
    for (const child of node.getChildren(file)) visit(child)
  }
  visit(file)
  return [...seen.values()].sort((a, b) => a.pos - b.pos)
}

export function stripComments(text: string, css = false): string {
  if (css) return text.replace(CSS_COMMENTS, (_all, str: string | undefined) => str ?? '')
  let out = ''
  let at = 0
  for (const r of tsCommentRanges(text)) { out += text.slice(at, r.pos); at = r.end }
  return out + text.slice(at)
}

// Removing comments only removes text, so a file whose raw text names none of
// these can produce no finding; only the rest are parsed.
const FOLD_TERMS = /viewport-segment|FOLD_X_QUERY/

let scanned: { path: string; text: string }[] | null = null
function sources(): { path: string; text: string }[] {
  if (scanned) return scanned
  const out: { path: string; text: string }[] = []
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name)
      if (statSync(full).isDirectory()) { if (name !== 'test' && name !== 'node_modules') walk(full); continue }
      if (!/\.(css|tsx?)$/.test(name) || /\.test\.tsx?$/.test(name)) continue
      const raw = readFileSync(full, 'utf8')
      if (!FOLD_TERMS.test(raw)) continue
      out.push({ path: srcPath(SRC, full), text: stripComments(raw, name.endsWith('.css')) })
    }
  }
  walk(SRC)
  return (scanned = out)
}

/** `full` relative to `root` with forward slashes on every OS, so it compares equal
 *  to the pinned `CHROME_FILE` and `HOOK_FILE` on Windows too. */
export const srcPath = (root: string, full: string, path: { relative: typeof relative; sep: string } = { relative, sep }) =>
  path.relative(root, full).split(path.sep).join('/')

const read = (p: string) => stripComments(readFileSync(join(SRC, p), 'utf8'), p.endsWith('.css'))

describe('the guard reads every line of code', () => {
  it('drops only real comments, whatever a string, regex or JSX text holds', () => {
    const src = [
      'const a = "image/*"',
      'const re = /^https?:\\/\\//i; const c = var1(--fold-start) // gone',
      'const tick = /[`"]?@/; const d = 1 /* gone */',
      'const e = `a ${`nested`} b`; const f = 2',
      'const g = <code>curl https://example.test/install</code>; const h = 3',
    ].join('\n')
    const out = stripComments(src)
    for (const kept of ['"image/*"', 'const c = var1(--fold-start)', 'const d = 1', 'const f = 2', 'https://example.test/install', 'const h = 3']) {
      expect(out).toContain(kept)
    }
    expect(out).not.toContain('gone')
    expect(stripComments('a { b: url(https://x); content: "/*"; c: var(--fold-end) } /* gone */', true)).toContain('var(--fold-end)')
  })
})

describe('fold query parity', () => {
  it('compares scanned paths with forward slashes on Windows and POSIX alike', () => {
    expect(srcPath('C:\\w\\src', 'C:\\w\\src\\styles\\shell-chrome.css', win32)).toBe(CHROME_FILE)
    expect(srcPath('/w/src', '/w/src/hooks/useViewportSegments.ts', posix)).toBe(HOOK_FILE)
  })

  it('the md variant reads FOLD_TWO_PANE_QUERY verbatim, at the 640px floor', () => {
    expect(FOLD_TWO_PANE_MIN).toBe(640)
    expect(FOLD_TWO_PANE_QUERY).toBe(`(min-width: 640px) and ${FOLD_X_QUERY}`)
    const form = '{ &:where(:root:not([data-layout-form="phone"]) *) { @slot; } }'
    expect(read(THEME_FILE)).toContain(`@custom-variant md {\n  @media (width >= 48rem) ${form}\n  @media ${FOLD_TWO_PANE_QUERY} ${form}\n}`)
  })

  it('the top bar collapse ladder follows the folded two-pane form', () => {
    expect(read(CHROME_FILE)).toContain(`@media ${LADDER_PRELUDE}{\n  .topbar.tb-measured > .tb-right`)
  })

  it('no file under src/ writes any other segments query', () => {
    const drift: string[] = []
    for (const { path, text } of sources()) {
      // Every @media prelude that names a segments feature must be
      // FOLD_TWO_PANE_QUERY (the md variant's fold block) or the top bar
      // ladder's LADDER_PRELUDE whole, so a `(min-width: 600px) and (...)`
      // cannot hide behind its FOLD_X_QUERY part. Both are checked above.
      for (const m of text.matchAll(/@media\s+([^{;]*viewport-segments[^{;]*?)\s*(?:\{|;|$)/g)) {
        if (![FOLD_TWO_PANE_QUERY, LADDER_PRELUDE].includes(m[1].trim())) drift.push(`${path}: @media ${m[1].trim()}`)
      }
      if (path !== HOOK_FILE && text.includes('${FOLD_X_QUERY}')) {
        drift.push(`${path}: builds a query from FOLD_X_QUERY; add it to the hook file instead`)
      }
      for (const m of text.matchAll(/\(\s*(?:horizontal|vertical)-viewport-segments\s*:[^)]*\)/g)) {
        if (m[0] !== FOLD_X_QUERY) drift.push(`${path}: ${m[0]}`)
      }
      if (text.includes(FOLD_X_QUERY) && ![THEME_FILE, HOOK_FILE, CHROME_FILE].includes(path)) {
        drift.push(`${path}: spells the fold query out; import FOLD_X_QUERY or FOLD_TWO_PANE_QUERY instead`)
      }
    }
    expect(drift).toEqual([])
  })

  it('an md: class compiles inside the FOLD_TWO_PANE_QUERY media rule', async () => {
    const compiler = await compile(
      ['@import "tailwindcss/theme.css" layer(theme);', `@import "./src/${THEME_FILE}";`, '@tailwind utilities source(none);'].join('\n'),
      { base: WEBSITE, onDependency() {} },
    )
    // Joined at run time: a literal class here would be scanned into the app's own CSS.
    const selector = '.md\\:block'
    const css = compiler.build([['md', 'block'].join(':')])
    const at = css.lastIndexOf(selector)
    expect(at, `${selector} was not emitted`).toBeGreaterThan(-1)
    const head = css.slice(0, at)
    expect(head.slice(head.lastIndexOf('@media')), selector).toMatch(new RegExp(`^@media ${FOLD_TWO_PANE_QUERY.replace(/[()]/g, '\\$&')}`))
  })
})
