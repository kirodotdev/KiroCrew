import { describe, it, expect } from 'vitest'
import { readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'
import { readSource } from './readSource'

/**
 * Hover-transform regression guard.
 *
 * The dashboard's rule is "hover is a HOVER": pointing at something may repaint
 * it (background, border, text colour) but must not change its SIZE or its
 * POSITION. A `hover:scale-*` grows a row under the cursor and nudges its
 * neighbours, which reads as a layout change rather than as "you are pointing at
 * this"; a `hover:-translate-y-*` lift does the same vertically. One sweep
 * removed 22 such call sites across 18 files, and every one of them was a single
 * class or a single framer-motion prop — the cheapest possible thing to
 * reintroduce by copying a neighbouring component.
 *
 * Nothing else in the toolchain catches it. A hover transform is not a type
 * error and not a lint error, and jsdom applies no `:hover` state and computes
 * no layout, so a behavioural test renders identically with or without it. It is
 * only visible to a human moving a real cursor. Hence this static guard.
 *
 * The rule this guard enforces is exactly the sweep's claim, no wider: hovering
 * must not change an element's SIZE or its POSITION. So `scale` on any axis and
 * `translate` on any axis fail, and the two positional effects that are
 * deliberately kept are named in ALLOWLIST with a reason a reviewer can check.
 *
 * A ROTATION is not in scope and needs no entry. A rotate about the element's
 * centre leaves its box where it was and moves no neighbour, which is why the
 * sweep kept the three it found. Demanding paperwork for every future hover
 * rotate would impose a rule this change never argued for.
 *
 * NOT scanned, by design: `whileTap` / `active:scale-*` / `active:translate-*`
 * (press feedback for an action the user actually took, deliberately kept), the
 * static selected-state `scale-110` on colour swatches (applied by selection
 * state, not by hover — an indicator, not a hover effect), test files, and the
 * public marketing site under `site/src`, which is separate scope and lives
 * outside this tree entirely.
 *
 * HOW sites are found in `.ts`/`.tsx`: by walking the TypeScript AST, not by
 * scanning raw text with a regex. The raw-text scanner had a fatal blind spot —
 * a regex cannot tell code from a string from a comment, so its `//` comment
 * stripper erased the REAL class in
 * `const marker = "//"; const cls = "hover:scale-110"`, a false NEGATIVE worse
 * than the false positive it fixed. Three review rounds each found another hole
 * of that one shape. The AST inverts the approach: it inspects only what IS a
 * class string (string / template literals) and what IS a framer-motion hover
 * prop (a JSX attribute named `whileHover`). Comments are TRIVIA a node walk
 * never visits, so a documented rule can never masquerade as a live one — the
 * false-positive class is closed structurally, with no stripping. `.css` keeps
 * its regex path (never faulted), and every path shares ONE identity helper.
 */

const WEBSITE_ROOT = join(__dirname, '..', '..')
const SRC = join(WEBSITE_ROOT, 'src')

/** Stable repository-relative path for the allowlist and for diagnostics.
 *  `relative()` uses the host separator; the checked-in allowlist uses POSIX. */
const repoRelative = (file: string) => relative(WEBSITE_ROOT, file).replaceAll('\\', '/')

/**
 * Positional hover effects that are deliberately kept.
 *
 * An entry is a CLAIM that a hover transform moves something the reader does not
 * experience as the hovered element changing place, so it carries a reason a
 * reviewer can check — it is not a way to silence a failure. `match` is a
 * substring of the offending text, so an entry stays pinned to the specific
 * utility it excuses and cannot blanket-excuse a whole file. Stale entries fail
 * (see the self-check below), so this list cannot rot into a widened guard.
 */
const ALLOWLIST: ReadonlyArray<{ file: string; match: string; reason: string }> = [
  {
    file: 'src/components/appstore/FeaturedSpotlight.tsx',
    match: 'group-hover:translate-x-0.5',
    reason:
      'App-store chevron nudged 2px to the RIGHT, the standard "this navigates onward" affordance. '
      + 'Horizontal, inside a fixed-width shrink-0 slot, so no size change and no reflow.',
  },
  {
    file: 'src/index.css',
    match: '.btn-sweep:hover::after',
    reason:
      'Send-button shine: translates a ::after gradient overlay ACROSS the button. The overlay is '
      + 'absolutely positioned and clipped by overflow-hidden, so the button never changes size or place.',
  },
  {
    file: 'src/apps/issue-radar/WelcomeCarousel.tsx',
    match: '.wc-planet:hover',
    reason:
      'Decorative orbiting-planets background: each planet is absolutely positioned inside its own '
      + 'animating ring, so scaling moves no neighbour and changes no layout. The scale is the affordance '
      + 'that reveals the planet name via .wc-planet:hover::after; this is not list or panel chrome.',
  },
]

/** One hover transform found in the source tree. */
type Site = {
  /** Repo-relative POSIX path. */
  file: string
  /** The matched text — what the failure message prints and what ALLOWLIST matches on. */
  text: string
  /** True when the transform changes the element's SIZE or POSITION: the swept class. */
  changesGeometry: boolean
  /** One-line human description of what the transform does. */
  detail: string
}

/**
 * Source files that ship UI: components and style sheets.
 *
 * Test files are excluded because they name these very classes as DATA — the
 * framer-motion prop mocks list `whileHover`, and this file's own allowlist
 * quotes each kept utility — so scanning them would make the guard flag itself.
 * Generated output (`dist`, `node_modules`) never lives under `src`.
 */
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      return entry === 'test' || entry === '__mocks__' || entry === 'node_modules' ? [] : walk(full)
    }
    if (/\.test\.tsx?$/.test(entry)) return []
    return /\.(?:tsx?|css)$/.test(entry) ? [full] : []
  })
}

/**
 * A Tailwind transform utility under a hover variant.
 *
 * Written to survive how these are actually authored here: a stacked variant
 * (`md:hover:scale-105`), a named group (`group-hover/nav:translate-x-1`), the
 * negative form where the minus follows the colon (`hover:-translate-y-0.5`),
 * and an arbitrary value (`group-hover:rotate-[-8deg]`).
 */
const HOVER_UTILITY =
  /(?:group-)?hover(?:\/[A-Za-z0-9_-]+)?:-?(scale|translate|rotate|skew)(?:-([xyz]))?-(\[[^\]\s]+\]|[A-Za-z0-9./%-]+)/g

/**
 * An ARBITRARY-PROPERTY hover utility: `hover:[transform:scale(1.1)]`, or the
 * individual-property form `hover:[scale:1.1]`.
 *
 * Tailwind accepts this alongside the named utilities above, and it renders the
 * same growth — so a guard that reads only `hover:scale-*` is a guard with a
 * documented bypass. Matched separately because the payload is CSS, not a
 * Tailwind scale step, and is classified by the CSS helpers below.
 */
const HOVER_ARBITRARY = /(?:group-)?hover(?:\/[A-Za-z0-9_-]+)?:\[([^\]\s]+)\]/g

/**
 * The ONE identity test, shared by every path (Tailwind named + arbitrary, CSS
 * `transform` function args, CSS individual `scale:`/`translate:`).
 *
 * A transform of identity adds no displacement, so the hovered geometry equals
 * the untransformed geometry: `scale(1)` / `scale-[100%]` is the same size,
 * `translateY(0)` / `translate: 0 0` is the same place. These must all pass or
 * the guard cries wolf on the reveal idiom (a tooltip resting at `translate-y-0`)
 * and on code that renders nothing — which teaches the next reader the guard is
 * noise. They previously disagreed: three near-identical regexes each accepted a
 * slightly different identity set, so a `0.0px` or a bare `100%` counted as rest
 * on one path and as growth on another. Centralising removes that whole class.
 *
 * Identity for SIZE (`scale`): `1`, `1.0`, `100%`, `100.0%`. Identity for
 * POSITION (`translate` and any zero): `0`, `0.0`, `0px`, `0.0px`, `0%`, `-0`.
 * `none` and the empty value are identity for either. A multi-token value
 * (`0 0`, `1 1`) is identity iff EVERY token is.
 */
const SCALE_IDENTITY = /^(?:1(?:\.0+)?|100(?:\.0+)?%)$/
const ZERO_IDENTITY = /^-?0(?:\.0+)?(?:[a-z%]+)?$/
function isIdentityValue(family: string, raw: string): boolean {
  // A trailing `!important` is presentation, not geometry — drop it before judging.
  const value = raw.replace(/\s*!important\s*$/i, '').trim()
  if (value === '' || /^none$/i.test(value)) return true
  const token = family === 'scale' ? SCALE_IDENTITY : ZERO_IDENTITY
  return value.split(/\s+/).every(t => token.test(t))
}

/**
 * Judge a Tailwind NAMED-utility step for identity.
 *
 * A bracketed value carries a raw CSS value (`scale-[1]`, `scale-[100%]`) and is
 * tested as-is. A bare scale step is a PERCENTAGE — `scale-100` compiles to
 * `scale(1)`, which is identity — so it is normalised to `100%` before the
 * shared test. Translate steps are spacing units; a bare `0` is identity, any
 * non-zero step moves the element.
 */
function tailwindStepIsIdentity(family: string, rawValue: string): boolean {
  const bracketed = rawValue.startsWith('[')
  const inner = rawValue.replace(/^\[|\]$/g, '')
  const value = family === 'scale' && !bracketed && /^[0-9.]+$/.test(inner) ? `${inner}%` : inner
  return isIdentityValue(family, value)
}

/** `scale` changes size on any axis; `translate` changes position on any axis,
 *  including x — a horizontal nudge is still the element leaving its place, so it
 *  needs a written reason rather than a silent pass. `rotate`/`skew` do neither. */
const utilityChangesGeometry = (family: string) =>
  family === 'scale' || family === 'translate'

/** CSS comments are stripped before scanning: a commented-out rule is not a
 *  rule, and prose about hover states would otherwise be read as selectors.
 *  Reused on TS/TSX string DATA too — a `/* … *\/` quoted inside a CSS-in-JS
 *  template literal is documentation, not a live class. */
const CSS_COMMENT = /\/\*[\s\S]*?\*\//g
/** Innermost rule whose selector carries `:hover` — nesting inside `@media`
 *  falls out for free because neither half may span a brace. */
const CSS_HOVER_RULE = /([^{}]*:hover[^{}]*)\{([^{}]*)\}/g
const CSS_TRANSFORM = /(?:^|[;\s])transform\s*:\s*([^;}]+)/
/**
 * The INDIVIDUAL transform properties, which are plain CSS and not a `transform`
 * value at all: `scale: 1.1`, `translate: 0 -2px`. They compose with `transform`
 * rather than replacing it, so a rule can grow an element without the substring
 * `transform` appearing anywhere in it — invisible to CSS_TRANSFORM above.
 */
const CSS_INDIVIDUAL = /(?:^|[;\s])(scale|translate)\s*:\s*([^;}]+)/g

/** Split a CSS `transform` value into its `fn(args)` calls, once. */
const transformCalls = (value: string) => [...value.matchAll(/([A-Za-z0-9]+)\s*\(([^)]*)\)/g)]

/** Does a CSS `transform` value change size or position? Judged per function
 *  through the shared identity helper, so `scale(100%)` and `translateX(0.0px)`
 *  read as rest exactly as they do on every other path. */
function cssChangesGeometry(value: string): boolean {
  return transformCalls(value).some(([, fn, args]) => {
    const family = /^scale/i.test(fn) ? 'scale' : /^translate/i.test(fn) ? 'translate' : null
    if (family === null) return false // rotate / skew / perspective leave the box where it is
    return args.split(',').some(arg => !isIdentityValue(family, arg))
  })
}

/**
 * A CSS `transform` whose every function is an identity — `translateY(0)`,
 * `scale(1)`, `scale(100%)`, `none`. Skipped for the same reason as the Tailwind
 * rest values: it moves nothing. Kept consistent with `isIdentityValue`
 * deliberately, so the same authored intent does not fail in CSS while passing
 * in Tailwind.
 */
function cssTransformIsRest(value: string): boolean {
  const calls = transformCalls(value)
  if (!calls.length) return isIdentityValue('translate', value)
  return calls.every(([, fn, args]) => {
    const family = /^scale/i.test(fn) ? 'scale' : 'translate'
    return args.split(',').every(arg => isIdentityValue(family, arg))
  })
}

/** Collect geometry-changing declarations from CSS, whether a stylesheet or CSS-in-JS string. */
function scanCssText(text: string, file: string, found: Site[]) {
  const stylesheet = text.replace(CSS_COMMENT, '')
  for (const rule of stylesheet.matchAll(CSS_HOVER_RULE)) {
    const selector = rule[1].trim()
    const value = rule[2].match(CSS_TRANSFORM)?.[1]
    if (value && !cssTransformIsRest(value)) {
      found.push({
        file,
        text: `${selector} { transform: ${value.trim()} }`,
        changesGeometry: cssChangesGeometry(value),
        detail: `CSS :hover rule sets transform: ${value.trim()}`,
      })
    }
    // The individual properties compose with `transform` instead of replacing
    // it, so they are collected independently rather than as an else-branch.
    for (const [, prop, propValue] of rule[2].matchAll(CSS_INDIVIDUAL)) {
      if (isIdentityValue(prop, propValue)) continue
      found.push({
        file,
        text: `${selector} { ${prop}: ${propValue.trim()} }`,
        changesGeometry: true,
        detail: `CSS :hover rule sets the individual property ${prop}: ${propValue.trim()}`,
      })
    }
  }
}

/** Collapse an unbounded snippet to one printable line. */
const oneLine = (text: string, max = 150) => {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max)}…` : flat
}

/** How a `whileHover` value reads under the AST — the three outcomes drive the push. */
type Verdict = 'geometry' | 'safe' | 'unresolvable'

/** The framer-motion keys that change an element's SIZE or POSITION. `x`/`y` are
 *  a shorthand for a translate; `scale*` is a size change on some axis. */
const GEOMETRY_MOTION_KEYS = new Set(['scale', 'scaleX', 'scaleY', 'scaleZ', 'x', 'y'])

/** The declared name of an object-literal property, or null when it cannot be
 *  read statically (a computed key like `[k]: …`, which we must fail closed on). */
function staticPropName(p: ts.ObjectLiteralElementLike): string | null {
  if (!('name' in p) || !p.name) return null
  if (ts.isIdentifier(p.name) || ts.isStringLiteral(p.name)) return p.name.text
  return null
}

/**
 * Can a `whileHover` value be judged by READING it, and if so does it move?
 *
 * Only an inline object (or `undefined`, or a conditional over such) can be read
 * here. `whileHover="grow"` and `whileHover={hoverVariant}` name a variant
 * defined elsewhere that framer-motion resolves at RUNTIME — so `{ grow: { scale:
 * 1.1 } }` grows the element while its scale lives nowhere near the hover site.
 * Treating "cannot tell" as "fine" is the exact bypass that fails the guard open,
 * and this tree already uses `variants=` elsewhere, so the referenced form is one
 * edit away, not hypothetical. Hence: fail CLOSED on anything not inlined.
 *
 * `undefined` is the one exception, and it is genuinely resolved: it is how the
 * reduce-motion branches here disable a hover entirely.
 */
function classifyMotionExpr(expr: ts.Expression | undefined): Verdict {
  if (!expr) return 'unresolvable' // `whileHover` bare, or `whileHover={}`
  if (ts.isIdentifier(expr)) return expr.text === 'undefined' ? 'safe' : 'unresolvable'
  if (ts.isObjectLiteralExpression(expr)) {
    for (const p of expr.properties) {
      // A spread merges a variant resolved elsewhere; a computed key hides its
      // own name. Either means the geometry may live outside what we can read.
      if (ts.isSpreadAssignment(p)) return 'unresolvable'
      if ('name' in p && p.name && ts.isComputedPropertyName(p.name)) return 'unresolvable'
    }
    const names = expr.properties.map(staticPropName)
    return names.some(n => n !== null && GEOMETRY_MOTION_KEYS.has(n)) ? 'geometry' : 'safe'
  }
  if (ts.isConditionalExpression(expr)) {
    // Resolvable only if EVERY branch is; moves if ANY branch moves.
    const branches = [classifyMotionExpr(expr.whenTrue), classifyMotionExpr(expr.whenFalse)]
    if (branches.includes('unresolvable')) return 'unresolvable'
    return branches.includes('geometry') ? 'geometry' : 'safe'
  }
  // A string literal, property access, call, template, etc. is not an inline
  // variant we can read — fail closed.
  return 'unresolvable'
}

/** Every hover-triggered transform in the shipped source, scanned once. */
const SITES: Site[] = walk(SRC).flatMap(file => {
  const rel = repoRelative(file)
  const raw = readSource(file)
  const found: Site[] = []

  if (file.endsWith('.css')) {
    scanCssText(raw, rel, found)
    return found
  }

  // TS/TSX: an AST walk. Only string-bearing nodes carry class names and only a
  // JSX attribute named `whileHover` is a framer-motion hover prop — everything
  // else, comments included, is invisible to the walk by construction.
  const sf = ts.createSourceFile(rel, raw, ts.ScriptTarget.Latest, /* setParentNodes */ true, ts.ScriptKind.TSX)

  const scanClassText = (text: string) => {
    // A string-bearing node may be either class text or embedded CSS; run both
    // source detectors over the same AST-selected text so comments in TS/TSX
    // remain invisible while CSS-in-JS cannot bypass the stylesheet rule.
    scanCssText(text, rel, found)

    // No comment stripping here, deliberately. A `/* … */` inside a class string
    // is NOT inert: Tailwind's JIT generates the rule from the source text, and
    // the browser splits a class attribute on whitespace, so
    // `className="/* hover:scale-110 */"` yields a LIVE `hover:scale-110` token
    // between two junk ones. Stripping it would let a real hover scale hide
    // inside comment syntax. Line and block comments in the CODE never reach
    // here at all — they are trivia the node walk skips, which is what lets this
    // scanner keep the real class in `const marker = "//"` that the old raw-text
    // stripper erased.
    for (const util of text.matchAll(HOVER_UTILITY)) {
      const [matched, family, , value] = util
      if (tailwindStepIsIdentity(family, value)) continue
      found.push({
        file: rel,
        text: matched,
        changesGeometry: utilityChangesGeometry(family),
        detail: `Tailwind hover utility ${matched}`,
      })
    }
    // `hover:[transform:scale(1.1)]` / `hover:[scale:1.1]` — same rendered effect,
    // different syntax, judged by the CSS helpers since the payload is CSS.
    for (const arb of text.matchAll(HOVER_ARBITRARY)) {
      const payload = arb[1].replaceAll('_', ' ')
      const transform = payload.match(/^transform:\s*(.+)$/)
      if (transform) {
        if (cssTransformIsRest(transform[1])) continue
        found.push({
          file: rel,
          text: arb[0],
          changesGeometry: cssChangesGeometry(transform[1]),
          detail: `Tailwind arbitrary hover transform ${arb[0]}`,
        })
        continue
      }
      const individual = payload.match(/^(scale|translate):\s*(.+)$/)
      if (individual && !isIdentityValue(individual[1], individual[2])) {
        found.push({
          file: rel,
          text: arb[0],
          changesGeometry: true,
          detail: `Tailwind arbitrary hover property ${arb[0]}`,
        })
      }
    }
  }

  const visit = (node: ts.Node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      scanClassText(node.text)
    } else if (ts.isTemplateExpression(node)) {
      // Every literal chunk of a template — the head and each span's tail — is
      // class text; the `${…}` holes between them are expressions, visited on
      // their own as the walk descends.
      scanClassText(node.head.text)
      for (const span of node.templateSpans) scanClassText(span.literal.text)
    } else if (ts.isJsxAttribute(node) && ts.isIdentifier(node.name) && node.name.text === 'whileHover') {
      // `whileHover` is a hover prop ONLY as a JSX attribute name. A bare string
      // `'whileHover'` in a list (e.g. `supportedMotionProps`) is data and is
      // never reached here.
      const init = node.initializer
      const value: ts.Expression | undefined =
        init === undefined ? undefined : ts.isJsxExpression(init) ? init.expression : init
      const verdict = classifyMotionExpr(value)
      const printed = `whileHover=${oneLine(
        init === undefined ? '{true}' : init.getText(sf).replace(/^whileHover\s*=\s*/, ''),
        110,
      )}`
      if (verdict === 'unresolvable') {
        // Refuse rather than skip: a named or referenced variant resolves at
        // runtime, so reading this site proves nothing about its geometry.
        found.push({
          file: rel,
          text: printed,
          changesGeometry: true,
          detail: 'framer-motion whileHover whose value is not an inline object — the guard cannot '
            + 'see whether it changes geometry. Inline the variant at the hover site, or allowlist it '
            + 'with a reason.',
        })
      } else {
        found.push({
          file: rel,
          text: printed,
          changesGeometry: verdict === 'geometry',
          detail: 'framer-motion whileHover variant',
        })
      }
    }
    node.forEachChild(visit)
  }
  visit(sf)

  return found
})

const excusedBy = (site: Site) =>
  ALLOWLIST.find(entry => entry.file === site.file && site.text.includes(entry.match))

const report = (sites: Site[]) =>
  sites.map(s => `  ${s.file}\n    ${s.detail}\n    matched: ${oneLine(s.text)}`).join('\n')

describe('hover never changes an element\'s size or position', () => {
  it('has no hover-triggered scale or move under website/src', () => {
    const violations = SITES.filter(s => s.changesGeometry && !excusedBy(s))

    expect(
      violations.map(s => `${s.file} :: ${oneLine(s.text, 80)}`),
      'A hover transform that SCALES or MOVES makes an element grow or jump under the cursor, '
        + 'nudging its neighbours and reading as a layout change rather than as "you are pointing at '
        + 'this". A sweep removed every one of these; these are back.\n'
        + 'Fix each one:\n'
        + '  - Delete the hover transform. Say "you are pointing at this" by PAINTING instead: '
        + 'hover:bg-bg-hover, hover:border-border-strong, hover:text-text, hover:shadow-md.\n'
        + '  - Press feedback is fine and needs no change: whileTap / active:scale-95 respond to an '
        + 'action the user actually took.\n'
        + '  - A selected-state scale applied by STATE (not by hover) is an indicator, not a hover '
        + 'effect, and this guard does not see it.\n'
        + '  - A hover ROTATION is not in scope: it leaves the box where it is.\n'
        + '  - If this one genuinely must stay, add it to ALLOWLIST in this file with a one-line reason '
        + 'a reviewer can check.\n\n'
        + `Offending sites:\n${report(violations)}`,
    ).toEqual([])
  })

  /**
   * Self-check, in both directions. Without it the allowlist only ever widens.
   *
   * An entry matching NOTHING is dead: the code it was written for was deleted or
   * renamed, and its `match` substring now sits ready to excuse a FUTURE
   * transform nobody reviewed. An entry matching MORE THAN ONE site is worse,
   * because it grants an unreviewed pass silently: matching is by file plus
   * substring, so a second `group-hover:translate-x-0.5` pasted anywhere in the
   * same file would inherit the first one's written reason, and the
   * matches-something half above would still be satisfied by the original. Each
   * entry must therefore account for exactly one site.
   */
  it('has one live site per ALLOWLIST entry', () => {
    const wrong = ALLOWLIST.map(entry => ({
      entry,
      hits: SITES.filter(site => site.file === entry.file && site.text.includes(entry.match)).length,
    }))
      .filter(({ hits }) => hits !== 1)
      .map(({ entry, hits }) => `  ${entry.file} :: ${entry.match} — matches ${hits} site(s), expected 1`)

    expect(
      wrong,
      'Every ALLOWLIST entry must excuse exactly ONE hover transform.\n'
        + '  0 matches: the entry is dead (its code was deleted, renamed or moved). Delete it, or '
        + 'update its file/match to where the code now lives — a dead entry is a hole, because its '
        + 'substring would excuse a future transform nobody reviewed.\n'
        + '  2+ matches: one written reason is silently covering several sites. The extra site was '
        + 'never reviewed. Remove the new transform, or give it its own entry with its own reason.\n\n'
        + `Entries:\n${wrong.join('\n')}`,
    ).toEqual([])
  })

  it('scans the shipped source and nothing else', () => {
    const scanned = walk(SRC).map(repoRelative)
    // A guard that scans nothing passes forever. Pin the corpus so an over-eager
    // exclusion in walk() fails here instead of quietly disarming everything.
    expect(scanned.length).toBeGreaterThan(500)
    expect(scanned.every(f => f.startsWith('src/'))).toBe(true)
    // The public marketing site is separate scope with its own card lifts.
    expect(scanned.some(f => f.startsWith('site/'))).toBe(false)
    // Test files name these classes as data; scanning them would flag this file.
    expect(scanned.some(f => /\.test\.tsx?$|(?:^|\/)test\//.test(f))).toBe(false)
    // The three doors the sweep's removals came through are all covered.
    expect(scanned).toContain('src/index.css')
    expect(scanned).toContain('src/components/ui.tsx')
    expect(scanned).toContain('src/pages/overview/MemoryRecordsEditor.tsx')
  })
})
