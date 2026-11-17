import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * Opening a menu must not restyle the whole app.
 *
 * Every Radix layer (menu, popover, dialog, select) inserts a focus guard at
 * the start of `<body>` when it opens, and removes it when it closes. Three
 * things in a stylesheet turn that one node into a style recalc of every
 * element. Each is pinned here at source level, because happy-dom has no style
 * invalidation to measure:
 *
 * 1. A sibling combinator between two compounds with no class, id, tag or
 *    attribute (`* + *`, `:not(…) ~ :not(…)`). Chrome has nothing to key such
 *    a rule on, so a node inserted anywhere restyles the whole subtree of the
 *    elements after it.
 * 2. `#root` as the first child of `<body>`: the focus guard takes its
 *    `:first-child`, and a rule that continues past `:first-child` restyles
 *    the subtree of the element that lost it.
 * 3. The custom property the scroll lock writes on `<body>`: unregistered, it
 *    inherits, so the write reaches every element.
 */
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')

function sourceFiles(dir: string, keep: (name: string) => boolean): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) out.push(...sourceFiles(path, keep))
    else if (keep(name)) out.push(path)
  }
  return out
}

/** Selectors of every rule in a stylesheet, comments removed, lists split. */
function cssSelectors(css: string): string[] {
  const source = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: string[] = []
  for (const m of source.matchAll(/([^{}]*)\{/g)) {
    const prelude = (m[1].split(';').pop() ?? '').trim()
    if (prelude && !prelude.startsWith('@')) out.push(...splitTopLevel(prelude, ','))
  }
  return out
}

/** Split on `sep` where it is not inside `()` or `[]`. */
function splitTopLevel(text: string, sep: string): string[] {
  const out: string[] = []
  let depth = 0
  let start = 0
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === '(' || c === '[') depth++
    else if (c === ')' || c === ']') depth--
    else if (c === sep && depth === 0) { out.push(text.slice(start, i).trim()); start = i + 1 }
  }
  out.push(text.slice(start).trim())
  return out
}

/** The compounds of one selector and the combinator before each (the first has none). */
function compounds(selector: string): { compound: string; combinator: string }[] {
  const out: { compound: string; combinator: string }[] = []
  let depth = 0
  let current = ''
  let combinator = ''
  for (const c of selector.trim()) {
    if (c === '(' || c === '[') depth++
    else if (c === ')' || c === ']') depth--
    if (depth === 0 && /[\s>+~]/.test(c)) {
      if (current) { out.push({ compound: current, combinator }); current = ''; combinator = ' ' }
      if (c.trim()) combinator = c
    } else current += c
  }
  if (current) out.push({ compound: current, combinator })
  return out
}

/** Whether a compound names something Chrome can key a rule on. A negation names nothing. */
const hasFeature = (compound: string) => /[.#[&]|^[a-zA-Z]/.test(compound.replace(/:not\([^()]*(?:\([^()]*\)[^()]*)*\)/g, ''))

/** A `+` or `~` whose two sides both lack a class, id, tag or attribute. */
function bareSiblingPair(selector: string): boolean {
  const parts = compounds(selector)
  return parts.some((p, i) => (p.combinator === '+' || p.combinator === '~') && !hasFeature(p.compound) && !hasFeature(parts[i - 1].compound))
}

/** Tailwind arbitrary variants (`[&>li+li]:…`) in a source file, as the selector they compile to. */
function arbitraryVariants(source: string): string[] {
  const out: string[] = []
  for (let at = source.indexOf('[&'); at !== -1; at = source.indexOf('[&', at + 1)) {
    let depth = 0
    for (let i = at; i < source.length && !/[\s'"`]/.test(source[i]); i++) {
      if (source[i] === '[') depth++
      else if (source[i] === ']' && --depth === 0) { out.push(source.slice(at + 1, i).replace(/_/g, ' ')); break }
    }
  }
  return out
}

describe('opening a layer restyles only what changed', () => {
  it('finds no sibling combinator between two compounds without a class, id, tag or attribute', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(SRC, (n) => n.endsWith('.css'))) {
      for (const sel of cssSelectors(readFileSync(file, 'utf-8'))) {
        if (bareSiblingPair(sel)) offenders.push(`${relative(SRC, file)}: ${sel}`)
      }
    }
    // Test files too: Tailwind scans them for class names, so a literal variant
    // in a test string ships in the built stylesheet like any other.
    for (const file of sourceFiles(SRC, (n) => /\.tsx?$/.test(n))) {
      for (const sel of arbitraryVariants(readFileSync(file, 'utf-8'))) {
        if (bareSiblingPair(sel)) offenders.push(`${relative(SRC, file)}: [${sel}]`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('still recognises the shapes it guards against', () => {
    expect(bareSiblingPair('.box > * + *')).toBe(true)
    expect(bareSiblingPair('.tb-capsule > :not([data-liquid-glass-layer]) ~ :not([data-liquid-glass-layer])')).toBe(true)
    // Joined, so this file holds no class name for Tailwind to emit.
    const classes = ['p-2 [&>*', '+*]:border-t [&_[data-x]]:mb-0'].join('')
    expect(arbitraryVariants(classes).map(bareSiblingPair)).toEqual([true, false])
    expect(bareSiblingPair('.tb-capsule > .tb-capsule-keep ~ *')).toBe(false)
    expect(bareSiblingPair('.list > li + li')).toBe(false)
    expect(bareSiblingPair('.box > :where(:not(:first-child))')).toBe(false)
  })

  it('keeps #root from being the first element of <body>', () => {
    const html = readFileSync(resolve(SRC, '..', 'index.html'), 'utf-8')
    // Walk past whitespace and comments after the <body> tag (alone on its line) to its first element.
    const body = /^<body[^>]*>$/m.exec(html)
    expect(body, 'no <body> tag on a line of its own').not.toBeNull()
    let at = body!.index + body![0].length
    for (;;) {
      while (/\s/.test(html[at] ?? '')) at++
      if (!html.startsWith('<!--', at)) break
      const end = html.indexOf('-->', at)
      expect(end, 'unterminated comment after <body>').toBeGreaterThan(-1)
      at = end + 3
    }
    const first = /^<[a-z]+([^>]*)>/.exec(html.slice(at))
    expect(first, 'no element found after <body>').not.toBeNull()
    expect(first![1]).not.toMatch(/id="root"/)
    expect(first![1], 'the element before #root must take no space').toMatch(/\bhidden\b/)
  })

  it('registers the scroll-lock property as non-inherited', () => {
    const css = readFileSync(resolve(SRC, 'index.css'), 'utf-8').replace(/\/\*[\s\S]*?\*\//g, '')
    const rule = /@property\s+--removed-body-scroll-bar-size\s*\{([^}]*)\}/.exec(css)
    expect(rule, 'index.css must register --removed-body-scroll-bar-size with @property').not.toBeNull()
    expect(rule![1].replace(/\s/g, '')).toContain('inherits:false')
  })
})

/**
 * A `::highlight(name)` rule with nothing before the pseudo-element matches
 * every element, so each style recalc builds a highlight style for the whole
 * page: the sidebar, the menus, everything that never holds a highlight. A rule
 * is either attached to a class, or limited to `[data-hl-scope]`, the attribute
 * on each element a highlight registrar walks.
 */
const HIGHLIGHT_SCOPE = '[data-hl-scope]'
const pageWideHighlight = (selector: string) => {
  if (!selector.includes('::highlight(') || selector.startsWith(`${HIGHLIGHT_SCOPE} `)) return false
  const subject = compounds(selector).pop()!.compound
  return !hasFeature(subject.slice(0, subject.indexOf('::highlight(')))
}

/** The element each registrar walks for text, by the ref that element takes. */
const HIGHLIGHT_ROOTS: [file: string, ref: string, what: string][] = [
  ['pages/chat/AssistantMessage.tsx', 'contentRef', 'the reply bubble the transcript find walks'],
  ['pages/chat/UserMessage.tsx', 'userRef', 'the user bubble the transcript find walks'],
  ['components/ContentRenderer.tsx', 'previewRef', 'the preview the preview find, the comment underline and the composer passage walk'],
  ['pages/RemoteArtifactDetailPage.tsx', 'mdPreviewRef', 'the remote artifact body the composer passage walks'],
]

describe('highlight rules reach only the elements that can hold a highlight', () => {
  it('finds no ::highlight() rule that matches every element of the page', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(SRC, (n) => n.endsWith('.css'))) {
      for (const sel of cssSelectors(readFileSync(file, 'utf-8'))) {
        if (pageWideHighlight(sel)) offenders.push(`${relative(SRC, file)}: ${sel}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('still recognises the shapes it guards against', () => {
    expect(pageWideHighlight('::highlight(mc-find)')).toBe(true)
    expect(pageWideHighlight('.panel ::highlight(mc-find)')).toBe(true)
    expect(pageWideHighlight(':root::highlight(mc-find)')).toBe(true)
    expect(pageWideHighlight('[data-hl-scope] ::highlight(mc-find)')).toBe(false)
    expect(pageWideHighlight('[data-hl-scope]::highlight(mc-find)')).toBe(false)
    expect(pageWideHighlight('.code-block::highlight(mc-find-current)')).toBe(false)
    expect(pageWideHighlight('.code-block')).toBe(false)
  })

  it('limits each scoped rule to the scope element and its descendants, both', () => {
    // Firefox and Safari do not pass a highlight style down to descendants, so
    // a rule on the scope element alone paints nothing inside it.
    const css = readFileSync(resolve(SRC, 'styles/highlights-and-page-zoom.css'), 'utf-8')
    const selectors = cssSelectors(css).filter((s) => s.includes('::highlight(') && s.startsWith(HIGHLIGHT_SCOPE))
    const names = new Set(selectors.map((s) => /::highlight\(([\w-]+)\)/.exec(s)![1]))
    expect(names.size).toBeGreaterThan(0)
    for (const name of names) {
      expect(selectors, name).toContain(`${HIGHLIGHT_SCOPE}::highlight(${name})`)
      expect(selectors, name).toContain(`${HIGHLIGHT_SCOPE} ::highlight(${name})`)
    }
  })

  it.each(HIGHLIGHT_ROOTS)('%s: every element taking %s carries data-hl-scope (%s)', (file, ref) => {
    const source = readFileSync(resolve(SRC, file), 'utf-8')
    const taken = source.match(new RegExp(`ref=\\{${ref}\\b[^}]*\\}`, 'g')) ?? []
    const scoped = source.match(new RegExp(`ref=\\{${ref}\\b[^}]*\\}\\s+data-hl-scope=""`, 'g')) ?? []
    expect(taken.length, `${file} no longer attaches ${ref}`).toBeGreaterThan(0)
    expect(scoped.length, `${file}: an element takes ${ref} without data-hl-scope right after it`).toBe(taken.length)
  })
})
