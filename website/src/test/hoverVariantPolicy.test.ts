import { describe, it, expect, beforeAll } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { compile } from '@tailwindcss/node'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { IconButtonGroup } from '../components/ui'

/** Hover variant policy, and the touch state every hover-revealed control needs.
 *
 *  `hover:` (and every `group-hover:` / `peer-hover:` derived from it) stays at
 *  Tailwind v4's default gate, `@media (hover: hover)`. It used to be redefined
 *  as a bare `&:hover`, so that tapping a hover-revealed control on a phone
 *  revealed it. On iOS that reveal is what cancels the tap: Safari dispatches a
 *  synthetic hover first, sees content become visible, and drops the click, so
 *  every such control needed two taps.
 *
 *  With the gate in place, a control that is hidden until hover is unreachable
 *  on touch unless it carries its own `(hover: none)` state. The second half of
 *  this file scans `src/` for hidden-until-hover markup and fails on any site
 *  that has neither a touch override nor an entry in `DECORATIVE` below. */

const WEBSITE = join(__dirname, '..', '..')
const SRC = join(WEBSITE, 'src')
const THEME = readFileSync(join(SRC, 'tailwind-theme.css'), 'utf8')
const THEME_DIRECTIVES = THEME.replace(/\/\*[\s\S]*?\*\//g, '')

const ORACLE_CSS = [
  '@import "tailwindcss/theme.css" layer(theme);',
  '@import "./src/tailwind-theme.css";',
  '@tailwind utilities source(none);',
].join('\n')

let css = ''
beforeAll(async () => {
  const compiler = await compile(ORACLE_CSS, { base: WEBSITE, onDependency() {} })
  css = compiler.build([
    'hover:opacity-100',
    'group-hover:opacity-100',
    'group-hover/row:opacity-100',
    'peer-hover:opacity-100',
    'hover:bg-bg-hover',
    'sm:opacity-0',
    'md:opacity-0',
    '[@media(hover:none)]:opacity-100',
    '[@media(hover:none)]:disabled:opacity-30',
    '[.group:hover_&]:block',
  ])
})

/** The CSS text from the start of the stylesheet to this class's rule. */
const before = (escapedSelector: string) => {
  const at = css.indexOf(escapedSelector)
  expect(at, `${escapedSelector} was not emitted`).toBeGreaterThan(-1)
  return css.slice(0, at)
}
/** True when the rule at `escapedSelector` sits inside an open `@media (hover: hover)`. */
const insideHoverMedia = (escapedSelector: string) => {
  const head = before(escapedSelector)
  const open = head.lastIndexOf('@media (hover: hover)')
  if (open === -1) return false
  // Balanced braces between the @media opener and the rule mean the block closed.
  const between = head.slice(open)
  const opens = (between.match(/\{/g) ?? []).length
  const closes = (between.match(/\}/g) ?? []).length
  return opens > closes
}

describe('hover variant policy (tailwind-theme.css)', () => {
  it('a (hover: none) disabled state sorts after the (hover: none) reveal it dims', () => {
    const reveal = css.indexOf('.\\[\\@media\\(hover\\:none\\)\\]\\:opacity-100')
    const dimmed = css.indexOf('.\\[\\@media\\(hover\\:none\\)\\]\\:disabled\\:opacity-30')
    expect(reveal).toBeGreaterThan(-1)
    expect(dimmed).toBeGreaterThan(reveal)
  })

  it('the explicit ungated tooltip selector compiles outside any hover media query', () => {
    expect(css).toContain('.group:hover .\\[\\.group\\:hover_\\&\\]\\:block')
    expect(insideHoverMedia('.\\[\\.group\\:hover_\\&\\]\\:block')).toBe(false)
  })

  it('does not redefine the hover variant', () => {
    expect(THEME_DIRECTIVES).not.toMatch(/@custom-variant\s+hover\b/)
  })

  it.each([
    ['hover:opacity-100', '.hover\\:opacity-100'],
    ['group-hover:opacity-100', '.group-hover\\:opacity-100'],
    ['group-hover/row:opacity-100', '.group-hover\\/row\\:opacity-100'],
    ['peer-hover:opacity-100', '.peer-hover\\:opacity-100'],
    ['hover:bg-bg-hover', '.hover\\:bg-bg-hover'],
  ])('%s is emitted only inside @media (hover: hover)', (_cls, selector) => {
    expect(insideHoverMedia(selector)).toBe(true)
  })

  it('a (hover: none) override sorts after the breakpoint that hides a control', () => {
    // `sm:opacity-0 sm:group-hover:opacity-100` hides a control on a wide
    // touch screen (an iPad, a phone in landscape). Its touch override only
    // works if it comes later in the stylesheet than `sm:opacity-0`.
    const touch = css.indexOf('.\\[\\@media\\(hover\\:none\\)\\]\\:opacity-100')
    expect(touch).toBeGreaterThan(css.indexOf('.sm\\:opacity-0'))
    expect(touch).toBeGreaterThan(css.indexOf('.md\\:opacity-0'))
    expect(css).toMatch(/@media \(hover:\s?none\)\s*\{\s*\.\\\[\\@media\\\(hover\\:none\\\)\\\]\\:opacity-100/)
  })
})

// ---------------------------------------------------------------------------
// Every hidden-until-hover control has a touch state
// ---------------------------------------------------------------------------

/** A hover variant that makes something visible. Colour/shadow/transform
 *  hovers are decoration and are fine to lose on touch. */
/** Variant prefixes may stack and include arbitrary values such as
 *  `[@media(min-width:640px)_and_(min-height:600px)]:`, so a site moved behind a
 *  custom breakpoint stays in the scan instead of silently dropping out. */
const REVEAL = /(?:^|[\s"'`{])(?:(?:[a-z]+|\[[^\]\s]+\]):)*(?:(?:group|peer)-hover(?:\/[\w-]+)?|hover):(?:opacity-\d+|visible|block|flex|inline-flex|inline-block|grid|pointer-events-auto|w-auto|line-clamp-none|text-muted)(?=[\s"'`}]|$)/
/** A resting state that hides the control, clips its text or makes its glyph transparent (whole class tokens only). */
const HIDDEN = /(?:^|[\s"'`{])(?:(?:sm|md|lg|\[[^\]\s]+\]):)?(?:opacity-0|invisible|hidden|w-0|line-clamp-\d+|text-transparent)(?=[\s"'`}]|$)/
/** A touch state: an explicit (hover: none) utility or a shared touch class. */
const TOUCH = /\[@media\(hover:none\)\]:|HOVER_NONE_ACTIONS_ROW_CLS|HOVER_NONE_ACTION_BTN_CLS|ICON_ACTION_ROW_CLS/

/** Hidden-until-hover sites that are decoration, not controls: a hint glyph
 *  inside a button that is itself always visible, or a hover tooltip. On touch
 *  these simply do not appear. Keyed by file and a snippet of the line. */
const DECORATIVE: Array<[file: string, snippet: string, why: string]> = [
  ['App.tsx', 'group-hover/nav:opacity-100 group-focus-visible/nav', 'keyboard-shortcut chord hint on a nav link'],
  ['apps/spec-builder/components/SpecDetail.tsx', '<Pencil size={11}', 'edit glyph inside the always-visible title button'],
  ['components/ChatInput.tsx', 'group-hover/drag:opacity-100', 'resize-grip bar; the drag handle itself stays in place'],
  ['components/CopyBranchButton.tsx', 'group-hover/branch:opacity-70', 'copy glyph inside the always-visible branch button'],
  ['components/FileChangeChips.tsx', 'group-hover/tip:opacity-100', 'hover tooltip on a clickable chip; tapping opens the diff view, which names the file'],
  ['components/crew/CrewAvatarButton.tsx', 'group-hover/avatar:opacity-100', 'hover overlay, explicitly hidden under (hover: none)'],
  ['pages/ArtifactDetailPage.tsx', '<Pencil size={14}', 'edit glyph inside the always-visible rename button'],
  ['pages/chat/SessionTitleControl.tsx', '<Pen size=', 'edit glyph beside the always-visible title'],
  ['pages/settings/SecurityPanel.tsx', '<ExternalLink size={11}', 'external-link glyph on an always-visible row'],
  ['components/WelcomeView.tsx', '[@media(min-width:640px)_and_(min-height:600px)]:group-hover:line-clamp-none', 'unclamps a suggestion\'s text on hover; one tap sends the full text to the composer'],
]

/** Files no route or component mounts, so no user can reach their hover-only
 *  controls. They are skipped by the scan; the check below fails the moment
 *  anything outside a test imports one, which is when it needs a touch state. */
const UNMOUNTED: Array<[file: string, symbol: string]> = [
  ['pages/overview/DisplayTab.tsx', 'DisplayTab'],
  ['pages/chat/NotificationItem.tsx', 'NotificationItem'],
]

const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) return name === 'test' ? [] : walk(full)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [full] : []
  })

type Site = { file: string; line: number; text: string }

/** Lines carrying a reveal whose surrounding class expression (the line and
 *  three either side, for class strings split across lines) also hides it.
 *  Limit: a hidden/reveal pair assembled from helpers or constants in another
 *  file or further away is not seen, so the `it.each` pins below cover the
 *  shared shapes (`IconButtonGroup`, `utils/touchActions.ts`) directly. */
const revealSites = (): Array<Site & { guarded: boolean }> =>
  walk(SRC).flatMap((full) => {
    const lines = readFileSync(full, 'utf8').split('\n')
    const file = relative(SRC, full).split('\\').join('/')
    if (UNMOUNTED.some(([f]) => f === file)) return []
    return lines.flatMap((text, i) => {
      if (!REVEAL.test(text)) return []
      const window = lines.slice(Math.max(0, i - 3), i + 4).join('\n')
      if (!HIDDEN.test(text) && !HIDDEN.test(window)) return []
      return [{ file, line: i + 1, text, guarded: TOUCH.test(window) }]
    })
  })

describe('hidden-until-hover controls stay reachable on touch', () => {
  const sites = revealSites()

  it('finds the reveal sites (the scan is not silently empty)', () => {
    expect(sites.length).toBeGreaterThan(40)
  })

  it('every hidden-until-hover site has a (hover: none) state or is listed as decorative', () => {
    const decorative = (s: Site) => DECORATIVE.some(([f, snip]) => f === s.file && s.text.includes(snip))
    const offenders = sites.filter((s) => !s.guarded && !decorative(s)).map((s) => `${s.file}:${s.line}  ${s.text.trim().slice(0, 140)}`)
    expect(
      offenders,
      'A control hidden until hover is unreachable on a touch screen. Add a `[@media(hover:none)]:opacity-*` ' +
        '(or a utils/touchActions.ts class) so it is visible there, or, if it is pure decoration, list it in DECORATIVE.\n' +
        offenders.join('\n'),
    ).toEqual([])
  })

  it.each(UNMOUNTED)('UNMOUNTED file is used by no source file: %s', (file, symbol) => {
    // A barrel's `export { default as X } from './X'` line is not a use; any other
    // line that names the symbol or imports the module path is.
    const stem = file.replace(/\.tsx?$/, '').split('/').pop() as string
    const reexport = new RegExp(`^\\s*export\\s*\\{\\s*default as ${symbol}\\s*\\}\\s*from\\s*['"]\\./${stem}['"]`)
    const uses = walk(SRC)
      .filter((full) => relative(SRC, full).split('\\').join('/') !== file)
      .flatMap((full) => readFileSync(full, 'utf8').split('\n')
        .filter((line) => !reexport.test(line))
        .filter((line) => new RegExp(`\\b${symbol}\\b|['"][^'"]*/${stem}['"]`).test(line) && !/^\s*(\/\/|\*|\/\*)/.test(line))
        .map((line) => `${relative(SRC, full)}: ${line.trim().slice(0, 120)}`))
    expect(uses, `${file} is mounted now; give its hover-only controls a (hover: none) state and drop it from UNMOUNTED`).toEqual([])
  })

  it.each(DECORATIVE)('DECORATIVE entry still matches a site: %s (%s)', (file, snippet) => {
    expect(sites.some((s) => s.file === file && s.text.includes(snippet))).toBe(true)
  })

  it.each([
    ['pages/ChatSidebar.tsx', 'absolute top-1/2 -translate-y-1/2 right-1.5 p-1 bg-card border border-border shadow-sm opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100'],
    ['pages/ChatSidebar.tsx', 'has-[[data-state=open]]:opacity-100'],
    ['components/ui.tsx', "'opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100"],
    ['components/chat-input/FilePreviewStrip.tsx', 'group-hover/preview:opacity-100 [@media(hover:none)]:opacity-100'],
    ['components/notifications/NotificationCard.tsx', 'group-hover:opacity-50 [@media(hover:none)]:opacity-60'],
    ['components/RegistryManager.tsx', 'sm:group-hover:opacity-100 [@media(hover:none)]:opacity-100'],
    // A pending registry action keeps its dimmed state on touch: the touch override lives in the idle branch only.
    ['components/RegistryManager.tsx', "refreshMutation.isPending ? 'pointer-events-none opacity-30 [@media(hover:none)]:opacity-30' : '[@media(hover:none)]:opacity-100'"],
    // A disabled remove button stays dimmed on touch.
    ['apps/personal-shopper/SitesTab.tsx', 'disabled:opacity-30 [@media(hover:none)]:disabled:opacity-30'],
    // The daily-usage tooltip is the only place a day's figures appear and the bar has no click to lose,
    // so it keeps an ungated :hover that a tap still triggers.
    ['pages/overview/TokenDailyChart.tsx', 'hidden [.group:hover_&]:block'],
    // An always-visible overlay control keeps the room its hover state reserved, so it covers no content on touch.
    ['apps/code-review-sage/components/RunCard.tsx', "(onDelete ? ' [@media(hover:none)]:pb-7' : '')"],
    // Every notification dismiss rests at the same weight on touch.
    ['components/notifications/NotificationFeed.tsx', 'group-hover:opacity-40 [@media(hover:none)]:opacity-60'],
    ['pages/ChatSidebar.tsx', "${remoteInstanceId ? '' : '[@media(hover:none)]:pr-[4.5rem] '}"],
    // A session row on any touch screen (an iPad too) gets the single ⋯ menu, not the overlay cluster.
    ['pages/ChatSidebar.tsx', 'isMobile: isMobile || isTouchDevice'],
    // On touch a non-empty folder row's cluster sits inline, the shape an empty folder row already uses.
    ['pages/ChatSidebar.tsx', '[@media(hover:none)]:opacity-100 [@media(hover:none)]:static [@media(hover:none)]:translate-y-0'],
    // The table view's folder row spans the whole sideways-scrolling table, so on touch its menu sits inline, not ml-auto past the viewport.
    ['components/library/LibraryTable.tsx', 'ml-auto [@media(hover:none)]:ml-0 opacity-0'],
  ])('%s carries its touch state', (file, snippet) => {
    expect(readFileSync(join(SRC, file), 'utf8')).toContain(snippet)
  })

  it('IconButtonGroup reveal keeps its touch state through twMerge, and a plain group gets none', () => {
    const revealed = renderToStaticMarkup(createElement(IconButtonGroup, { reveal: true, className: 'absolute right-1.5' }, 'x'))
    expect(revealed).toContain('opacity-0')
    expect(revealed).toContain('[@media(hover:none)]:opacity-100')
    const plain = renderToStaticMarkup(createElement(IconButtonGroup, {}, 'x'))
    expect(plain).not.toContain('opacity-0')
    expect(plain).not.toContain('hover:none')
  })
})
