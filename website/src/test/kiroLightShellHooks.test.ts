/**
 * The kiro-light shell rules in index.css select plain hook classes
 * (`.user-bubble`, `.focus-chrome-rail`, ...), not Tailwind utilities, so the
 * phantom-classes gate never sees them: a refactor that drops one of those
 * classes from its component leaves the rule matching nothing, and the surface
 * quietly falls back to the token it was scoped away from -- the user bubble
 * back to white-on-white, the rail back to a white card on a white page.
 *
 * This pins each hook to the source file that must carry it, in both
 * directions: every class the kiro-light rules select must appear in its
 * component's markup, and every hook the components advertise for kiro-light
 * must still be selected by a rule -- otherwise a dropped rule leaves a dead
 * class behind that reads as load-bearing.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const read = (...p: string[]) => readFileSync(join(__dirname, '..', ...p), 'utf8')
const css = read('index.css')

/** hook class -> the source that must render it, and the regex proving it does */
const HOOKS: Record<string, { file: string[]; carries: RegExp }> = {
  'focus-chrome-rail': { file: ['App.tsx'], carries: /className="focus-chrome-rail / },
  'sidebar-inner': { file: ['pages', 'ChatSidebar.tsx'], carries: /className="sidebar sidebar-inner / },
  'user-bubble': { file: ['pages', 'chat', 'UserMessage.tsx'], carries: /'user-bubble bg-card text-card-fg'/ },
  'tb-capsule': { file: ['App.tsx'], carries: /className={`tb-capsule / },
  'feedback-pill': { file: ['components', 'FeedbackPill.tsx'], carries: /className="feedback-pill / },
}

/**
 * What a failing assertion should tell the developer: the fix is to keep the
 * hook on the element, never to loosen the regex. The regexes are anchored on
 * the hook sitting FIRST in the class string so a reorder that pushes it
 * behind a utility still fails -- the hook is load-bearing, not decorative.
 */
const KEEP_HOOK = (hook: string, file: string[]) =>
  `\`.${hook}\` must stay on its element in ${file.join('/')} (leading position in the class string): ` +
  'it is what the kiro-light rules in index.css select. See website/docs/theming-contract.md, ' +
  '"A card must carry its own edge" -- keep the hook, do not widen this regex.'

/** every `.hook` selected under a kiro-light prefix in index.css */
function kiroLightHooks(): Set<string> {
  const out = new Set<string>()
  for (const m of css.matchAll(/\[data-theme="kiro-light"\] \.([a-z][a-z0-9-]*)/g)) out.add(m[1])
  return out
}

describe('kiro-light shell hooks', () => {
  const selected = kiroLightHooks()

  it.each(Object.keys(HOOKS))('the kiro-light rules select `.%s`', hook => {
    expect(selected.has(hook), `index.css lost the kiro-light rule for \`.${hook}\``).toBe(true)
  })

  it.each(Object.entries(HOOKS))('`.%s` is still rendered by its component', (hook, { file, carries }) => {
    expect(read(...file), KEEP_HOOK(hook, file)).toMatch(carries)
  })

  it('the user bubble tint is scoped to the non-steer branch only', () => {
    // The steer bubble keeps bg-accent-subtle; tinting it too would erase the
    // one cue that distinguishes a steer from an ordinary turn.
    const src = read('pages', 'chat', 'UserMessage.tsx')
    expect(src, KEEP_HOOK('user-bubble', ['pages', 'chat', 'UserMessage.tsx'])).toMatch(
      /isSteer \? 'bg-accent-subtle text-text' : 'user-bubble bg-card text-card-fg'/,
    )
  })

  it('the edit-mode box carries the same hook as the bubble it replaces', () => {
    // Editing swaps the bubble for an in-place textarea box; without the hook it
    // flips back to white under the caret while every other bubble stays grey.
    const src = read('pages', 'chat', 'UserMessage.tsx')
    expect(src, KEEP_HOOK('user-bubble', ['pages', 'chat', 'UserMessage.tsx'])).toMatch(/className="edit-grow user-bubble /)
  })

  it('the pinned-prompt card carries the same hook as the bubble it stands in for', () => {
    // The banner is a pixel-for-pixel copy of the bubble so the hand-off reads as
    // the bubble sticking, not being replaced; a theme that tints one must tint
    // the other or the swap becomes visible.
    const src = read('pages', 'chat', 'PinnedPrompt.tsx')
    expect(src, KEEP_HOOK('user-bubble', ['pages', 'chat', 'PinnedPrompt.tsx'])).toMatch(
      /className="user-bubble flex items-start gap-2 rounded-xl bg-card text-card-fg ring-1 /,
    )
  })

  it('kiro-light gives the borderless bg-card surfaces an edge the white canvas would otherwise erase', () => {
    expect(css).toMatch(/\[data-theme="kiro-light"\] \.user-bubble\{background-color:var\(--bg-hover\)\}/)
    expect(css).toMatch(
      /\[data-theme="kiro-light"\] \.tb-capsule,\[data-theme="kiro-light"\] \.feedback-pill\{box-shadow:inset 0 0 0 1px var\(--border\)\}/,
    )
    expect(css).toMatch(
      /\[data-theme="kiro-light"\] \.focus-chrome-rail,\[data-theme="kiro-light"\] \.sidebar-inner\{background-color:var\(--panel\)\}/,
    )
  })
})
