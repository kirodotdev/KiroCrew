/**
 * How a crew chip marks a remote crew's tunnel state: the dot the desktop chip
 * draws beside the name, and the letter badge a phone chip draws in its place.
 *
 * The phone bar has no room for crew names (it shares one row with the page's
 * own controls and the Bot's identity stack), so there the chip is icon-only.
 * A bare dot would then say only "a crew", so the badge carries the crew's
 * initial, and the dot's state colour with it. The full name stays in the
 * chip's accessible name, its hint and the switcher menu.
 */

import type { ReactNode } from 'react'
import { CircleAlert, Square } from 'lucide-react'
import { i18nT } from '../i18n/t'

/**
 * Localized name for a live tunnel state. The raw values are backend enum
 * words, so they are mapped rather than interpolated: they reach the user in a
 * row's accessible name and its tooltip.
 */
export function stateLabel(state?: string): string {
  if (state === 'connected') return i18nT('components.instanceTabBar.connected')
  if (state === 'connecting') return i18nT('components.instanceTabBar.connecting')
  if (state === 'error') return i18nT('components.instanceTabBar.tunnel_error')
  // A stopped tunnel is not the same as one that was never opened: the crew's
  // own machine may be running while the forward is down.
  if (state === 'stopped') return i18nT('components.instanceTabBar.stopped')
  return i18nT('components.instanceTabBar.disconnected')
}

/** Map a live tunnel state to its status-dot color. */
export function stateDotCls(state?: string): string {
  return state === 'connected'
    ? 'bg-[var(--ok)]'
    : state === 'error'
      ? 'bg-[var(--danger)]'
      : state === 'connecting'
        ? 'bg-[var(--warn)]'
        : 'bg-[var(--muted)]'
}

/**
 * Text colour for the visible state word, mirroring :func:`stateDotCls`. The word
 * carries the state on its own — the colour is reinforcement, which is the point:
 * remove the colour and the row still says what it is.
 */
export function stateTextCls(state?: string): string {
  return state === 'connected'
    ? 'text-[var(--ok)]'
    : state === 'error'
      ? 'text-[var(--danger)]'
      : state === 'connecting'
        ? 'text-[var(--warn)]'
        : 'text-muted'
}

/** A badge never grows past two characters: it has to fit a 16px disc. */
const BADGE_MAX_CHARS = 2

/** Punctuation and spaces a crew name may open with (`@prod`, `-dev`). */
const LEADING_NON_ALNUM = /^[^\p{L}\p{N}]+/u

/**
 * The badge text for each name, in the same order: the name's first letter, or,
 * when another name shares it, the shortest prefix that tells them apart, capped
 * at two characters. Comparison ignores case; the badge shows the first
 * character upper-case and the second lower-case (`D`, `St`, `Sa`). Two names
 * that still match after two characters both get the same two (`Pr`, `Pr`).
 * A name with no letter or digit gets `''`, and the chip keeps its plain dot.
 *
 * Pass every remote crew the switcher lists, not only the connected ones, so a
 * badge does not change under the reader when another crew connects.
 *
 * shortcut: splits on code points, so a name opening with a combined emoji
 * shows its first code point; switch to `Intl.Segmenter` if such names appear.
 */
export function crewBadgeLetters(names: readonly string[]): string[] {
  const chars = names.map(name => Array.from(name.trim().replace(LEADING_NON_ALNUM, '')))
  const prefix = (c: string[], k: number) => c.slice(0, k).join('').toLowerCase()
  return chars.map((c, i) => {
    let k = 1
    while (k < BADGE_MAX_CHARS && chars.some((other, j) => j !== i && prefix(other, k) === prefix(c, k))) k++
    const [first = '', ...rest] = c.slice(0, k)
    // `ß` upper-cases to `SS`: keep a letter whose capital is longer than it.
    const upper = first.toUpperCase()
    return (Array.from(upper).length === 1 ? upper : first) + rest.join('').toLowerCase()
  })
}

/**
 * Outline style of a badge whose tunnel is not live: dashed for an error, dotted
 * while connecting, solid when stopped, disconnected or unknown.
 */
function badgeOutlineCls(state?: string): string {
  return state === 'error' ? 'border-dashed' : state === 'connecting' ? 'border-dotted' : 'border-solid'
}

/**
 * The pip a down tunnel's badge carries in its bottom-right corner, so the two
 * states a reader acts on can be told apart without the 1px outline and without
 * colour: a disc with `!` for an error, a filled square (the "stop" symbol) when
 * stopped. A slash was the other candidate for stopped; at this size its 1px
 * stroke is lost, and it reads as "disabled". The pip is centred on the
 * badge's outline at its lower right, so it covers as little of the letter as
 * it can. It is positioned absolutely, so the chip is no wider with it, and it
 * stays inside the chip's padding. Both are Lucide icons with a filled body,
 * and the icon's stroke rings the body so it stays apart from the outline and
 * the letter. `CircleAlert` strokes its `!` and ring in the `--danger-fg` ink
 * each theme pairs with `--danger`, and `Square` rings its body in the page
 * colour.
 */
function BadgeStateMark({ state }: { state?: string }) {
  if (state !== 'error' && state !== 'stopped') return null
  const error = state === 'error'
  const Icon = error ? CircleAlert : Square
  return (
    <Icon
      aria-hidden
      data-testid="crew-badge-mark"
      data-mark={state}
      size={9}
      strokeWidth={2.5}
      fill={error ? 'var(--danger)' : 'var(--muted)'}
      color={error ? 'var(--danger-fg)' : 'var(--bg)'}
      className="absolute -right-[3px] -bottom-[3px] pointer-events-none"
    />
  )
}

/**
 * The phone chip's mark for a remote crew. A live tunnel fills the disc with the
 * state colour. Any other state outlines it in that colour with a 1px border
 * whose style names the state (see `badgeOutlineCls`), so each state reads from
 * the shape as well as the colour. An error or a stopped tunnel also carries a
 * corner pip (see `BadgeStateMark`). Every style keeps the same 1px width, so the
 * badge is the same size in every state. Decorative: the chip's `aria-label`
 * names the crew and its state.
 *
 * It holds the crew's initials, or, passed as `children`, the crew's icon
 * (`crewIcons.tsx`), which then takes the letters' place in the same disc.
 */
export function CrewLetterBadge({ letters = '', state, children }: { letters?: string; state?: string; children?: ReactNode }) {
  const live = state === 'connected'
  return (
    <span
      aria-hidden
      data-testid="crew-letter-badge"
      data-state={state || 'disconnected'}
      className={
        'relative inline-flex items-center justify-center min-w-[16px] h-4 rounded-full text-[10px] leading-4 font-bold shrink-0 ' +
        (children ? 'px-0 ' : 'px-1 ') +
        (live
          ? `${stateDotCls(state)} text-[var(--ok-fg)]`
          : `border border-current ${badgeOutlineCls(state)} ${stateTextCls(state)}`)
      }
    >
      {children ?? letters}
      <BadgeStateMark state={state} />
    </span>
  )
}
