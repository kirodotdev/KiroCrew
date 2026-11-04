import { describe, it, expect, beforeAll } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { compile } from '@tailwindcss/node'
import { readIndexCss } from './indexCssSource'

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
 * The utilities are declared as `@utility` blocks in `src/tailwind-theme.css`.
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
  {
    file: 'src/pages/members/MembersPage.tsx',
    classes: 'fixed left-0 top-0 z-[60] pointer-events-none origin-top-left will-change-transform',
    reason:
      'The mate face in flight between the identity pill and the Profile card (CrewFaceFlight). '
      + 'left-0 top-0 is only the transform origin: every frame translates it onto a viewport box '
      + 'measured from a face that already sits inside the p-safe shell, so it never hugs an edge.',
  },
  {
    file: 'src/pages/members/MateEntrance.tsx',
    classes: 'fixed left-0 top-0 origin-top-left will-change-transform',
    reason:
      'The first Mate\'s face in flight from centre stage to the header pill (MateEntrance). '
      + 'Same shape as CrewFaceFlight: left-0 top-0 is only the transform origin, and the inline '
      + 'left/top place it on a box measured from the thread area and the pill slot, both inside '
      + 'the p-safe shell, so it never hugs an edge.',
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
  {
    file: 'src/pages/chat/BrowserLiveCardCanvas.tsx',
    contains: "position: 'fixed', left: 0, top: 0, width: 1, height: 1",
    reason:
      'The takeover typing proxy: a 1x1, opacity-0, pointer-events:none textarea the phone keyboard '
      + 'types into. It draws nothing and takes no tap, and each tap moves its top to the tap point, '
      + 'so no safe-area inset applies to it.',
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

/** `p-safe` / `p-safe-or-*` / `inset-safe` inset every edge at once, so any of
 *  them satisfies all. */
const hasBlanketInset = (literal: string) =>
  /(?:^|\s)p-safe(?:-or-\S+)?(?=\s|$)/.test(literal) || /(?:^|\s)inset-safe(?=\s|$)/.test(literal)

/** `inset-0` pins all four edges at once. The per-edge scan does not list it,
 *  because a bare full-viewport layer (a dim backdrop, a click-catcher, a
 *  decorative strip) is meant to bleed to every edge. */
const hasInsetZero = (literal: string) => /(?:^|\s)inset-0(?=\s|$)/.test(literal)

/** The literal lays out children against its own edges: a flex or grid
 *  container, or one with unprefixed padding (`p-4`, `px-6`, `p-[10px]`). That
 *  is what separates a container that holds a dialog or a sheet from a
 *  backdrop. Padding gated behind a breakpoint (`sm:p-6`) does not count: it is
 *  not the phone layout. */
const laysOutChildren = (literal: string) =>
  /(?:^|\s)(?:flex|inline-flex|grid)(?=\s|$)/.test(literal)
  || /(?:^|\s)p[xytrblse]?-(?:\d|\[)/.test(literal)

/**
 * Full-viewport containers whose content is meant to sit under the insets.
 * Same contract as ALLOWLIST: each entry is a checkable claim, with a reason.
 */
const FULL_VIEWPORT_ALLOWLIST: ReadonlyArray<{ file: string; classes: string; reason: string }> = [
  {
    file: 'src/components/SideSheet.tsx',
    classes: 'fixed inset-0 z-[90] flex justify-end',
    reason:
      'The row only places the sheet against the right edge. The sheet carries p-safe itself, so its '
      + 'background reaches the screen edge while its header, body and footer stay clear of the insets.',
  },
  {
    file: 'src/components/OnboardingChapterShell.tsx',
    classes: 'fixed inset-0 z-[120] flex min-h-0 overflow-y-auto',
    reason:
      'SCRIM_CLASS. Below sm the panel fills it and pads its own top and sides (PANEL_CLASS '
      + 'pt-safe px-safe, dropped from sm) while each footer pads the bottom inset; from sm up the scrim '
      + 'carries sm:p-safe-or-6. OnboardingChapterShell.test.tsx pins both halves.',
  },
  {
    file: 'src/pages/chat/quickAsk/QuickAskSheet.tsx',
    classes: 'fixed inset-0 z-50 flex flex-col justify-end bg-black/50',
    reason:
      'The column only places the bottom sheet against the bottom edge, under a tap-to-fold backdrop. The '
      + 'sheet pads its own sides (max(20px, --safe-area-x)) and the bottom inset, so its background '
      + 'reaches the screen edge while its content stays clear of the insets.',
  },
  {
    file: 'src/components/markdown/Lightbox.tsx',
    classes: 'fixed inset-0 z-[9999] bg-black/80 flex items-center justify-center overflow-hidden',
    reason:
      'Full-screen image viewer: the image is zoomable, pannable content sized from the viewport '
      + '(90vw/90vh) and is meant to fill the screen, as a photo viewer does. Every control on it '
      + '(close, zoom, counter, caption) is its own fixed surface with *-safe offsets.',
  },
]

/** A width or max-width taken from the viewport minus a fixed gutter:
 *  `w-[calc(100%-4rem)]`, `max-w-[min(22rem,calc(100vw-2rem))]`. */
const viewportDerivedWidth = (literal: string) =>
  /(?:^|\s)(?:min-|max-)?w-\[[^\]]*calc\(100(?:vw|%)\s*-/.test(literal)

/** Placed by its classes against a side edge or the horizontal centre. A
 *  popover or tooltip placed by script at an anchor (inline `left`, clamped to
 *  the viewport there) is not: its width cap is only an overflow guard. */
const anchoredHorizontally = (literal: string) =>
  /(?:^|\s)(?:left|right)-(?:0|1\/2|safe(?:-(?:offset|or)-\S+)?)(?=\s|$)/.test(literal)

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
    const theme = readFileSync(join(WEBSITE_ROOT, 'src', 'tailwind-theme.css'), 'utf8')
    expect(theme).toMatch(/@utility p-safe\s*\{/)
    expect(theme, 'the utilities must emit real env() values').toContain('env(safe-area-inset-')

    // A safe-area utility CANDIDATE: a whitespace-delimited token inside a string
    // literal whose prefix is a real Tailwind property family. The closed prefix
    // list is what makes this safe to run over source -- an earlier version
    // matched `[a-z-]*-safe` over raw file text and flagged prose and CSS
    // variables (`--safe-…`, `contrast-safe`, `type-safe`) as missing utilities.
    // The list deliberately spans every family the previous third-party plugin
    // offered, so a migration leftover like `me-safe` or `inset-x-safe-or-3` is
    // still caught even though this plugin never emits it.
    const CANDIDATE = new RegExp(
      '^-?(?:p|px|py|pt|pr|pb|pl|ps|pe|m|mx|my|mt|mr|mb|ml|ms|me'
      + '|inset|inset-x|inset-y|top|right|bottom|left|start|end'
      + '|h|min-h|max-h|h-screen|min-h-screen|max-h-screen'
      + ')-safe(?:-(?:offset|or)-(?:\\[[^\\]]+\\]|[\\d.]+))?$',
    )
    // What tailwind-theme.css actually emits, READ from its `@utility` names so
    // the list can never drift from the file: a static `name` matches exactly,
    // a functional `name-*` matches a spacing step or an arbitrary `[value]`.
    // Anything else compiles to NOTHING and fails silently -- the same
    // invisible-regression class the guard exists for. Only the families this
    // codebase uses are declared, so a `top-safe-or-3` or a negative
    // `-bottom-safe-offset-4` written tomorrow lands here, not in a blank inset.
    const utilities = [...theme.matchAll(/^@utility\s+(-?[a-z][a-z-]*)(\*)?\s*\{/gm)]
      .filter(m => m[1].includes('-safe'))
    expect(utilities.length, 'no safe-area @utility blocks found in tailwind-theme.css').toBeGreaterThan(0)
    const emitted = (token: string) => utilities.some(([, name, star]) =>
      star ? new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\[[^\\]]+\\]|[\\d.]+)$`).test(token) : token === name)
    const unsupported = new Set<string>()
    for (const file of [...walk(SRC), join(WEBSITE_ROOT, 'index.html')]) {
      for (const literal of stringLiterals(readFileSync(file, 'utf8'))) {
        for (const token of literal.split(/\s+/)) {
          if (CANDIDATE.test(token) && !emitted(token)) unsupported.add(token)
        }
      }
    }
    expect(
      [...unsupported].sort(),
      'these safe-area utilities are used but NOT emitted by tailwind-theme.css, so they compile to '
        + 'nothing. Add the family as an @utility or use one it emits.',
    ).toEqual([])
  })
})

/**
 * What the four-edge utilities actually compile to. The name scan above only
 * proves a utility exists; a block that drops an edge (a `p-safe-or-*` with no
 * padding-left) still exists and still compiles, and leaves that edge under the
 * inset on hardware only. So this asks Tailwind itself, the way
 * tailwindAlphaTokens.test.ts does.
 */
describe('four-edge safe-area utilities compile to every edge', () => {
  let css = ''
  beforeAll(async () => {
    const compiler = await compile(
      ['@import "tailwindcss/theme.css" layer(theme);', '@import "./src/tailwind-theme.css";',
        '@tailwind utilities source(none);'].join('\n'),
      { base: WEBSITE_ROOT, onDependency() {} },
    )
    css = compiler.build(['p-safe', 'p-safe-or-4', 'p-safe-or-[10px]', 'px-safe', 'pt-safe'])
  })
  const rule = (selector: string) => {
    const body = css.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`))?.[1]
    expect(body, `${selector} was not emitted`).toBeDefined()
    return body!
  }

  it('p-safe pads all four edges from the insets', () => {
    const body = rule('.p-safe ')
    expect(body).toContain('padding-top: var(--safe-area-top, env(safe-area-inset-top))')
    expect(body).toContain('padding-right: env(safe-area-inset-right)')
    expect(body).toContain('padding-bottom: env(safe-area-inset-bottom)')
    expect(body).toContain('padding-left: env(safe-area-inset-left)')
  })

  it.each([
    ['.p-safe-or-4', 'calc(var(--spacing) * 4)'],
    ['.p-safe-or-\\[10px\\]', '10px'],
  ])('%s keeps the gutter and widens it past each inset', (selector, gutter) => {
    const body = rule(selector)
    expect(body).toContain(`padding-top: max(var(--safe-area-top, env(safe-area-inset-top)), ${gutter})`)
    expect(body).toContain(`padding-right: max(env(safe-area-inset-right), ${gutter})`)
    expect(body).toContain(`padding-bottom: max(env(safe-area-inset-bottom), ${gutter})`)
    expect(body).toContain(`padding-left: max(env(safe-area-inset-left), ${gutter})`)
    expect(body.match(/padding-/g)).toHaveLength(4)
  })

  it('px-safe pads both sides and pt-safe the gated top', () => {
    const px = rule('.px-safe')
    expect(px).toContain('padding-right: env(safe-area-inset-right)')
    expect(px).toContain('padding-left: env(safe-area-inset-left)')
    expect(px.match(/padding-/g)).toHaveLength(2)
    expect(rule('.pt-safe')).toContain('padding-top: var(--safe-area-top, env(safe-area-inset-top))')
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
   * `fixed inset-0` pins all four edges, and the per-edge scan above skips it so
   * a dim backdrop can bleed to every edge. A container that LAYS OUT a dialog or
   * a sheet is different: `flex items-center justify-center p-4` lets the dialog
   * grow to 16px from the viewport edge, which is under the notch in iOS
   * landscape and under the side controls a foldable's outer display can show.
   * Apple's guidance for viewport-fit=cover pages is to inset all four edges;
   * this is the door top/bottom-only fixes left open. `p-safe-or-4` keeps the
   * 16px gutter and widens it only where an inset is wider.
   */
  it('insets every edge of a full-viewport container that lays out a surface', () => {
    const violations: string[] = []
    for (const file of walk(SRC)) {
      const rel = repoRelative(file)
      for (const literal of stringLiterals(readFileSync(file, 'utf8'))) {
        if (!hasBareFixed(literal) || !hasInsetZero(literal) || !laysOutChildren(literal)) continue
        if (hasBlanketInset(literal)) continue
        if (FULL_VIEWPORT_ALLOWLIST.some(e => rel === e.file && literal.includes(e.classes))) continue
        violations.push(`${rel}\n    in: ${literal.replace(/\s+/g, ' ').trim().slice(0, 160)}`)
      }
    }
    expect(
      violations,
      'A `fixed inset-0` container that lays out its children places them against the raw viewport '
        + 'edge, so a dialog or sheet inside it can sit under a side inset.\n'
        + '  had p-<n>      -> p-safe-or-<n>   (same gutter, widened where an inset is wider)\n'
        + '  had no padding -> p-safe\n'
        + 'If its content must bleed under the insets, add it to FULL_VIEWPORT_ALLOWLIST with a reason.\n\n'
        + `Violations:\n${violations.join('\n')}`,
    ).toEqual([])
  })

  /**
   * A centred fixed surface (`left-1/2 -translate-x-1/2`) sized from the
   * viewport keeps the same gutter on both sides, so `calc(100% - 4rem)` puts
   * its edges 2rem from the screen edge whatever the insets are. One pinned to
   * a side (`right-safe-offset-4`) has the same problem on its far side.
   * Subtracting twice the wider side inset (`--safe-area-x`, design-tokens.css)
   * keeps both edges clear; it is 0 wherever there is no side inset.
   */
  it('takes the side insets out of a viewport-derived width on a fixed surface', () => {
    const violations: string[] = []
    for (const file of walk(SRC)) {
      const rel = repoRelative(file)
      for (const literal of stringLiterals(readFileSync(file, 'utf8'))) {
        if (!hasBareFixed(literal) || !viewportDerivedWidth(literal) || !anchoredHorizontally(literal)) continue
        // Doubling is written as a sum (`x+x`), never `2*x`: a multiplied
        // inset is one more thing an older engine can reject, and a rejected
        // width declaration drops the whole inset with it.
        if (literal.includes('--safe-area-x') && !/w-\[[^\]]*\*[^\]]*--safe-area-x/.test(literal)) continue
        violations.push(`${rel}\n    in: ${literal.replace(/\s+/g, ' ').trim().slice(0, 160)}`)
      }
    }
    expect(
      violations,
      'A fixed surface whose width is the viewport minus a fixed gutter ignores the side insets.\n'
        + 'Subtract them: w-[calc(100%-max(4rem,var(--safe-area-x)+var(--safe-area-x)))] for calc(100%-4rem).\n\n'
        + `Violations:\n${violations.join('\n')}`,
    ).toEqual([])
  })

  it('declares --safe-area-x as the wider of the two side insets', () => {
    const tokens = readFileSync(join(SRC, 'styles', 'design-tokens.css'), 'utf8')
    expect(tokens).toMatch(
      /--safe-area-x:\s*max\(env\(safe-area-inset-left,\s*0px\),\s*env\(safe-area-inset-right,\s*0px\)\)/,
    )
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
        + "Wrap the offset — top: 'calc(var(--safe-area-top, 0px) + 18px)' (top goes through the "
        + "display-mode-gated variable), other edges: 'calc(env(safe-area-inset-bottom, 0px) + 18px)'.\n\n"
        + `Violations:\n${violations.join('\n')}`,
    ).toEqual([])
  })

  /**
   * The TOP inset is not a bare env() read: with browser chrome on screen the
   * UA's own bar already sits below the status bar / display cutout, so a
   * non-zero `env(safe-area-inset-top)` there is spurious — Android WebView
   * browsers report the cutout height regardless of where the web view
   * actually sits, which painted a dead band above the app header. Only when
   * the app IS the window (installed PWA: standalone/fullscreen display mode)
   * does the viewport really extend under the cutout.
   *
   * So the safe-area utilities route the top edge through `--safe-area-top`,
   * and index.css zeroes that variable outside standalone/fullscreen. Pinned
   * at the source, like the compositor test pins the drawer's left inset: jsdom
   * implements neither `env()` nor `display-mode`, so no behavioural assertion
   * can tell the gated form from a bare env() — but reverting EITHER half
   * (utilities back to env(), or the index.css gate deleted) silently reopens
   * the dead band, and only on physical hardware.
   */
  it('routes the top inset through the display-mode-gated variable', () => {
    const theme = readFileSync(join(WEBSITE_ROOT, 'src', 'tailwind-theme.css'), 'utf8')
    // The utility half: every top-edge utility resolves the variable (env()
    // only as a load-race fallback), the other edges stay bare env() — those
    // insets are real even in-browser (iOS landscape notch flanks, home
    // indicator). No top utility may read env() directly.
    expect(theme).toMatch(/@utility top-safe\s*\{\s*top:\s*var\(--safe-area-top, env\(safe-area-inset-top\)\)/)
    expect(theme).toMatch(/padding-top:\s*var\(--safe-area-top, env\(safe-area-inset-top\)\)/)
    for (const m of theme.matchAll(/^\s*(?:padding-)?top:\s*([^;]+);/gm)) {
      expect(m[1], `top-edge declaration must go through --safe-area-top: ${m[1]}`)
        .toContain('var(--safe-area-top, env(safe-area-inset-top))')
    }

    const indexCss = readIndexCss()
    // The stylesheet half: 0 by default, env() re-enabled only when the app is
    // the window. Both declarations must survive for the gate to mean anything.
    expect(indexCss).toMatch(/--safe-area-top:\s*0px/)
    expect(indexCss).toMatch(
      /@media\s*\(display-mode:\s*standalone\),\s*\(display-mode:\s*fullscreen\)\s*\{\s*:root\s*\{\s*--safe-area-top:\s*env\(safe-area-inset-top,\s*0px\)/,
    )
  })
})
