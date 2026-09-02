import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

/**
 * Safe-area regression guard.
 *
 * The dashboard is a PWA (`manifest.json` -> display: standalone) whose viewport
 * declares `viewport-fit=cover`, so on a notched iPhone the web view spans the
 * ENTIRE screen — under the Dynamic Island and under the home indicator. The app
 * shell insets its in-flow content once via `p-safe`, but `position: fixed`
 * elements are positioned against the viewport and escape that padding
 * completely. Each one has to inset itself.
 *
 * Nothing else in the toolchain can catch a regression here: a missing inset is
 * not a type error, not a lint error, and renders perfectly on every desktop and
 * in jsdom (where `env(safe-area-inset-*)` is 0). It is only visible on physical
 * notched hardware, which CI does not have. Hence this static guard.
 *
 * The utilities come from a local Tailwind plugin — see `tailwind.config.js`.
 */

const WEBSITE_ROOT = join(__dirname, '..', '..')
const SRC = join(WEBSITE_ROOT, 'src')

/** Stable repository-relative path for allowlists and diagnostics. `relative()`
 *  uses the host separator, while the checked-in allowlists use POSIX paths. */
const repoRelative = (file: string) => relative(WEBSITE_ROOT, file).replaceAll('\\', '/')

/** Physical edges plus the two axis shorthands, mapped to the safe utility
 *  that satisfies each. `inset-{x,y}-*` are included deliberately: they pin two
 *  edges at once and are just as capable of hugging a notch as `left-*`. */
const EDGES = ['top', 'bottom', 'left', 'right', 'inset-x', 'inset-y'] as const

/**
 * A pin is any Tailwind offset on that edge: the `0` keyword, a numeric spacing
 * step (`top-14`), or an arbitrary value (`top-[42px]`). A `safe` variant is NOT
 * a pin — that is the fix, not the problem.
 */
const pinPattern = (edge: string) =>
  new RegExp(`(?:^|\\s)${edge}-(?:0|\\d+(?:\\.\\d+)?|\\[[^\\]]+\\])(?=\\s|$)`)

/** `top-safe`, `top-safe-offset-4`, `top-safe-or-[42px]`, … */
const safePattern = (edge: string) => new RegExp(`(?:^|\\s)${edge}-safe(?:-(?:offset|or)-\\S+)?(?=\\s|$)`)

/**
 * Surfaces that pin an edge and legitimately need no inset.
 *
 * An entry is a CLAIM that a screen-edge surface is safe, so it carries a
 * reason a reviewer can check — it is not a way to silence a failure. Every
 * other surface the guard found was a real defect and was fixed instead.
 */
const ALLOWLIST: ReadonlyArray<{ file: string; classes: string; reason: string }> = [
  {
    file: 'src/components/CommandPalette.tsx',
    classes: 'fixed left-0 right-0 z-[9999] flex items-start justify-center',
    reason:
      'Full-bleed dim backdrop. Insetting it would leave an unpainted strip beside the notch in '
      + 'landscape; the palette itself is centred by justify-center and needs no inset.',
  },
  {
    file: 'src/apps/command-bar/CommandBarOverlay.tsx',
    classes: 'fixed left-0 right-0 z-[9999] flex items-start justify-center',
    reason: 'Same full-bleed backdrop as CommandPalette — the bar it centres carries no edge pin.',
  },
]

/**
 * Same contract as ALLOWLIST, for the inline-style scan: each entry is a
 * checkable claim that a fixed surface is meant to be full-bleed, matched on the
 * file plus a distinctive fragment of the style object or its attributes.
 */
const INLINE_ALLOWLIST: ReadonlyArray<{ file: string; contains: string; reason: string }> = [
  {
    file: 'src/components/ThemeExperienceLayer.tsx',
    contains: "colorScheme: 'normal'",
    reason:
      'Decorative theme branding strip: pointerEvents:none, transparent background, height-clamped '
      + 'by data-theme-maxh. It paints ACROSS the top strip, so insetting it would leave an unpainted '
      + 'gap beside the notch, and being click-through it buries no control.',
  },
]

/** `.tsx` for components, `.ts` for style modules — a fixed surface styled from
 *  an exported style object (`apps/<name>/styles.ts`) is just as reachable on mobile
 *  as one styled inline, and was the coverage hole Design Review found. */
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap(entry => {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) return entry === 'test' ? [] : walk(full)
    return /\.tsx?$/.test(entry) ? [full] : []
  })
}

/**
 * Pull every string/template literal out of a source file.
 *
 * Scanning literals rather than raw lines is what makes the guard robust to how
 * a className is actually written here: a multi-line template literal, or a
 * `isMobile ? 'a b' : ''` ternary, each yield one literal carrying both the
 * `fixed` and its edge pins, so the two are never split across scan units.
 */
function stringLiterals(source: string): string[] {
  const out: string[] = []
  const re = /`(?:[^`\\]|\\[\s\S])*`|'(?:[^'\\\n]|\\[\s\S])*'|"(?:[^"\\\n]|\\[\s\S])*"/g
  for (const m of source.match(re) ?? []) out.push(m.slice(1, -1))
  return out
}

/** Unprefixed `fixed` only. A variant-gated `focus:fixed` (the keyboard-only
 *  skip link) or `sm:fixed` is not a mobile screen-edge surface. */
const hasBareFixed = (literal: string) => /(?:^|\s)fixed(?=\s|$)/.test(literal)

/** `p-safe` / `inset-safe` inset every edge at once, so either satisfies all. */
const hasBlanketInset = (literal: string) =>
  /(?:^|\s)p-safe(?=\s|$)/.test(literal) || /(?:^|\s)inset-safe(?=\s|$)/.test(literal)

describe('safe-area prerequisites', () => {
  it('keeps viewport-fit=cover in the viewport meta', () => {
    const html = readFileSync(join(WEBSITE_ROOT, 'index.html'), 'utf8')
    const meta = html.match(/<meta\s+name="viewport"[^>]*>/)?.[0] ?? ''
    // Without this, env(safe-area-inset-*) resolves to 0 on iOS and EVERY safe
    // utility in the app silently becomes a no-op — the failure mode this
    // assertion exists to make loud.
    expect(meta, 'viewport meta must opt into the safe-area insets').toContain('viewport-fit=cover')
  })

  it('emits the safe-area utilities locally, covering every family in use', () => {
    const config = readFileSync(join(WEBSITE_ROOT, 'tailwind.config.js'), 'utf8')
    expect(config).toMatch(/plugins:\s*\[[^\]]*safeArea/)
    expect(config, 'the plugin must emit real env() values').toContain('env(safe-area-inset-')

    // A safe-area utility CANDIDATE: a whitespace-delimited token inside a string
    // literal whose prefix is a real Tailwind property family. The closed prefix
    // list is what makes this safe to run over source -- an earlier version
    // matched `[a-z-]*-safe` over raw file text and flagged prose and CSS
    // variables (`--safe-…`, `contrast-safe`, `type-safe`) as missing utilities.
    // The list deliberately spans every family the previous third-party plugin
    // offered, so a migration leftover like `me-safe` or `inset-x-safe-or-3` is
    // still caught even though this plugin never emits it.
    const CANDIDATE = new RegExp(
      '^(?:p|px|py|pt|pr|pb|pl|ps|pe|m|mx|my|mt|mr|mb|ml|ms|me'
      + '|inset|inset-x|inset-y|top|right|bottom|left|start|end'
      + '|h|min-h|max-h|h-screen|min-h-screen|max-h-screen'
      + ')-safe(?:-(?:offset|or)-(?:\\[[^\\]]+\\]|[\\d.]+))?$',
    )
    // What this plugin actually emits. Anything else compiles to NOTHING and
    // fails silently -- the same invisible-regression class the guard exists for.
    // (This replaced an exact-version pin and a banned-utility list that only
    // existed to police the third-party package this plugin took over from.)
    const EMITTED = /^(?:p-safe|(?:top|right|bottom|left)-safe(?:-(?:offset|or)-(?:\[[^\]]+\]|[\d.]+))?)$/
    const unsupported = new Set<string>()
    for (const file of [...walk(SRC), join(WEBSITE_ROOT, 'index.html')]) {
      for (const literal of stringLiterals(readFileSync(file, 'utf8'))) {
        for (const token of literal.split(/\s+/)) {
          if (CANDIDATE.test(token) && !EMITTED.test(token)) unsupported.add(token)
        }
      }
    }
    expect(
      [...unsupported].sort(),
      'these safe-area utilities are used but NOT emitted by tailwind.config.js, so they compile to '
        + 'nothing. Add the family to the plugin or use one it emits.',
    ).toEqual([])
  })
})

describe('fixed surfaces inset themselves from the safe area', () => {
  it('has no screen-edge-pinned fixed element without a safe inset', () => {
    const violations: string[] = []

    for (const file of walk(SRC)) {
      const rel = repoRelative(file)
      for (const literal of stringLiterals(readFileSync(file, 'utf8'))) {
        if (!hasBareFixed(literal) || hasBlanketInset(literal)) continue

        for (const edge of EDGES) {
          if (!pinPattern(edge).test(literal)) continue
          if (safePattern(edge).test(literal)) continue
          const excused = ALLOWLIST.some(e => rel === e.file && literal.includes(e.classes))
          if (excused) continue
          violations.push(
            `${rel}\n    pinned "${edge}" with no ${edge}-safe* counterpart\n    in: ${literal.trim().slice(0, 160)}`,
          )
        }
      }
    }

    expect(
      violations,
      'A `fixed` element pinned to a screen edge escapes the shell\'s p-safe and will sit under the '
        + 'Dynamic Island or home indicator on a notched iPhone.\n'
        + 'Fix it with the matching plugin utility:\n'
        + '  edge-hugging     -> top-safe / bottom-safe / left-safe / right-safe\n'
        + '  offset from edge -> bottom-safe-offset-4   (env inset + 4)\n'
        + '  minimum gutter   -> left-safe-or-3         (max(env inset, 3))\n'
        + '  full-bleed chrome-> p-safe on the container\n'
        + 'If it genuinely needs no inset, add it to ALLOWLIST with a reason.\n\n'
        + `Violations:\n${violations.join('\n')}`,
    ).toEqual([])
  })

  /**
   * The className scan above cannot see a pin written as an inline style object
   * (`style={{ top: 48 }}`), and that is not a hypothetical gap: it is exactly
   * how the notifications popover was pinned before this guard existed, so the
   * defect class can walk straight back in through the one door the string scan
   * does not watch.
   *
   * Rather than parse JSX, this reads each element's attribute text — from the
   * `style={{` back to the opening `<` — and asks whether that same element also
   * carries a bare `fixed` class. An inline pin on a NON-fixed element is
   * irrelevant (it positions against an ancestor, which p-safe already inset).
   */
  it('has no fixed element pinned to an edge by an inline style object', () => {
    // Unitless / px / rem / em only. A PERCENTAGE offset is deliberately not a
    // pin: `left: '50%'` with `translateX(-50%)` is the canonical centering
    // idiom, and it hugs no edge -- treating it as one made this assertion fire
    // on a correctly-centred toast.
    const EDGE_KEY = /\b(top|bottom|left|right)\s*:\s*(?:-?\d+(?:\.\d+)?|['"`]-?\d+(?:\.\d+)?(?:px|rem|em)?['"`])/
    // Capture to the object's own closing brace and no further. Requiring a
    // literal `}}` would silently skip `style={{ … } as CSSProperties}`, which
    // is how several fixed surfaces in this codebase are actually written --
    // a hole that made this assertion pass while a real offender sat in it.
    const STYLE_OBJ = /style=\{\{([^}]*)\}/g
    const violations: string[] = []

    for (const file of walk(SRC)) {
      const rel = repoRelative(file)
      const src = readFileSync(file, 'utf8')

      for (const m of src.matchAll(STYLE_OBJ)) {
        const styleBody = m[1]
        const edge = styleBody.match(EDGE_KEY)
        if (!edge) continue

        // Attribute text of the element this style belongs to.
        const openTag = src.lastIndexOf('<', m.index)
        if (openTag < 0) continue
        const attrs = src.slice(openTag, m.index)
        // Only a bare `fixed` class matters; `position: 'fixed'` in the same
        // style object counts too, since it is the same escape from p-safe.
        const isFixed = /(?:^|\s|["'`])fixed(?=\s|["'`])/.test(attrs)
          || /position\s*:\s*['"`]fixed['"`]/.test(styleBody)
        if (!isFixed) continue

        const excused = INLINE_ALLOWLIST.some(e => rel === e.file && styleBody.includes(e.contains))
        if (excused) continue

        violations.push(
          `${rel}\n    inline style pins "${edge[1]}" on a fixed element\n    in: style={{${styleBody.trim().slice(0, 120)}}}`,
        )
      }
    }

    expect(
      violations,
      'A `fixed` element pinned by an inline style bypasses the className scan AND the shell\'s '
        + 'p-safe, so it lands under the Dynamic Island / home indicator with nothing to catch it.\n'
        + 'Prefer the safe utilities in className (top-safe-offset-[48px], bottom-safe, …). If the '
        + 'value must stay dynamic, add env(safe-area-inset-*) into the calc() yourself.\n'
        + 'If the surface is genuinely full-bleed, add it to INLINE_ALLOWLIST with a reason.\n\n'
        + `Violations:\n${violations.join('\n')}`,
    ).toEqual([])
  })

  /**
   * Third door: an exported STYLE MODULE (`apps/<name>/styles.ts`) whose object sets
   * `position: 'fixed'` plus a static edge offset, applied as `style={S.foo}`.
   * Neither scan above sees it — the className scan finds no utility and the
   * inline scan finds no `style={{`. Design Review found real offenders here
   * (a lightbox close button at top/right 18px and a toast stack at
   * bottom/right 18px), which is why `walk()` now covers `.ts` as well.
   *
   * A value that already contains `env(safe-area-inset-*)` is inset-aware and
   * correct, so only STATIC lengths count as a pin.
   */
  it('has no fixed style-module object pinned to an edge by a static offset', () => {
    const STATIC_EDGE = /\b(top|bottom|left|right)\s*:\s*(?:-?\d+(?:\.\d+)?|['"`]-?\d+(?:\.\d+)?(?:px|rem|em)?['"`])/
    const violations: string[] = []

    for (const file of walk(SRC)) {
      const rel = repoRelative(file)
      const src = readFileSync(file, 'utf8')
      for (const m of src.matchAll(/position\s*:\s*['"`]fixed['"`]/g)) {
        // The enclosing object literal: back to its opening brace, forward to
        // its close. These style entries are one-per-line, so this is exact.
        const open = src.lastIndexOf('{', m.index)
        const close = src.indexOf('}', m.index)
        if (open < 0 || close < 0) continue
        // An inline `style={{ … }}` attribute belongs to the scan above, which
        // owns INLINE_ALLOWLIST. Claiming it here too would re-flag a surface
        // that allowlist deliberately exempts, so skip it: this door's subject
        // is the exported style object, which no other scan can see.
        if (/style=\{\s*$/.test(src.slice(Math.max(0, open - 12), open))) continue
        const obj = src.slice(open, close)
        const edge = obj.match(STATIC_EDGE)
        if (!edge) continue
        violations.push(`${rel}\n    style object pins "${edge[1]}" statically on a fixed surface\n    in: ${obj.replace(/\s+/g, ' ').trim().slice(0, 150)}`)
      }
    }

    expect(
      violations,
      'A `fixed` style-module object pinned to a screen edge lands under the Dynamic Island / '
        + 'home indicator, and neither the className nor the inline-style scan can see it.\n'
        + "Wrap the offset: top: 'calc(env(safe-area-inset-top, 0px) + 18px)'.\n\n"
        + `Violations:\n${violations.join('\n')}`,
    ).toEqual([])
  })
})
