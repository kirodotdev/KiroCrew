/**
 * ChatScrollChrome — shared visual chrome for a chat transcript scroller:
 * the top/bottom edge fades and the jump-to-bottom pill. Extracted from the
 * main chat page so every chat surface (ChatPane split panes, the Crew
 * Members thread, embeds) wears the same edges instead of hand-rolling them.
 *
 * Layout contract (matches how the main chat mounts its own copies):
 *   - <EdgeFade side="top">  goes in a zero-height `relative` wrapper placed
 *     BETWEEN the header and the scroller; it overlays the scroller's first
 *     24px so content dissolves under the header edge instead of clipping.
 *   - <EdgeFade side="bottom"> goes directly AFTER the scroller; its in-flow
 *     height is cancelled with a negative top margin so it overlays the
 *     scroller's last 24px above the composer.
 *   - <JumpToBottomButton> goes inside a `relative` wrapper around the
 *     composer block; it floats 40px above it, centred, and is pointer-inert
 *     except for the pill itself.
 *
 */
import { useLayoutEffect, useRef } from 'react'
import { ArrowDown, ArrowUp } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { Glass } from '../components/Glass'

export function EdgeFade({ side, anchor = 'overlay' }: {
  side: 'top' | 'bottom'
  /** Top-fade anchoring: 'overlay' hangs 24px into a following scroller from a
   *  zero-height `relative` wrapper placed before it; 'below' hangs off the
   *  BOTTOM edge of the positioned element it is mounted inside (the main
   *  chat's opaque header row). Same gradient, two real mounting sites. */
  anchor?: 'overlay' | 'below'
}) {
  if (side === 'top') {
    return (
      <div aria-hidden className={`absolute ${anchor === 'below' ? 'top-full' : 'top-0'} inset-x-0 h-6 bg-gradient-to-b from-bg to-transparent pointer-events-none`} />
    )
  }
  return (
    <div aria-hidden className="h-6 -mt-6 bg-gradient-to-t from-bg to-transparent pointer-events-none relative z-[1]" />
  )
}

export function JumpToBottomButton({ visible, onClick, unreadCount = 0, direction = 'down' }: {
  visible: boolean
  onClick: () => void
  /** Messages below the reader they have not seen yet (the host's unread run).
   *  Above zero the pill widens to say so -- "N new · Jump to latest" -- and
   *  the same click still lands at the live end. Zero keeps the bare arrow. */
  unreadCount?: number
  /** 'up': the top-down chat layout, where the live edge is ABOVE the reader.
   *  The pill then hangs under the composer and points up. */
  direction?: 'down' | 'up'
}) {
  if (!visible) return null
  return <JumpPill onClick={onClick} unreadCount={unreadCount} up={direction === 'up'} />
}

/** The composer markers: the page and pane composers carry
 *  `data-composer-input` (`composerFocus.queryComposer`), the embed's editor
 *  `data-composer-editor`. A queued-message editor carries neither, so focus
 *  cannot land in a draft the person did not choose. */
const COMPOSER_FIELD_SELECTOR = '[data-composer-input], [data-composer-editor]'

/** A chat surface edge the walk below never climbs past: a grid pane, or the
 *  side chat, each with a composer of its own. */
const SURFACE_EDGE_SELECTOR = '[data-chat-pane], [data-side-chat-input]'
/** The side conversation's composer (`composerFocus.queryComposer` skips it). */
const SIDE_CHAT_SELECTOR = '[data-side-chat-input]'

/** The composer field nearest the pill: in the wrapper it floats in, else in
 *  the closest ancestor that holds one (the embed wraps the pill in its own
 *  box). The walk stops at the surface edge, and outside the side chat it
 *  skips the side chat's composer, so focus never lands in another
 *  conversation; a collapsed composer means no match, and nothing moves. */
function nearestComposerField(wrapper: Element | null): HTMLElement | null {
  const inSideChat = !!wrapper?.closest(SIDE_CHAT_SELECTOR)
  for (let el = wrapper; el; el = el.parentElement) {
    for (const field of el.querySelectorAll<HTMLElement>(COMPOSER_FIELD_SELECTOR)) {
      if (inSideChat || !field.closest(SIDE_CHAT_SELECTOR)) return field
    }
    if (el.matches(SURFACE_EDGE_SELECTOR)) return null
  }
  return null
}

function JumpPill({ onClick, unreadCount, up }: { onClick: () => void; unreadCount: number; up: boolean }) {
  const holderRef = useRef<HTMLDivElement | null>(null)
  // Set by a pointer press on the pill, cleared by a key press on it or by
  // focus leaving it, so it names the current press only. A tap or click is
  // not keyboard focus, and focusing the composer after a tap would raise the
  // on-screen keyboard over the messages the person just jumped to
  // (`composerFocus.focusComposer` skips touch for the same reason).
  const pointerPressedRef = useRef(false)
  // Reaching the bottom unmounts the pill, so the keyboard user on it would
  // land on <body>. Hand focus to the nearest composer instead; only while the
  // pill still holds keyboard focus, so a pill that leaves while focus is
  // elsewhere, or after a pointer press, moves nobody.
  useLayoutEffect(() => {
    const holder = holderRef.current
    const wrapper = holder?.parentElement ?? null
    return () => {
      if (pointerPressedRef.current) return
      if (holder && holder.contains(document.activeElement)) nearestComposerField(wrapper)?.focus()
    }
  }, [])
  const label = unreadCount > 0 ? i18nT('pages.chatPage.jump_to_new', { count: unreadCount }) : null
  return (
    <div ref={holderRef} className={`absolute ${up ? 'top-full mt-2' : '-top-10'} inset-x-0 z-10 pointer-events-none flex justify-center`}>
      {/* A glass pill (components/Glass.tsx, chip variant) rendered as the
          button: the same material as the composer it floats above. One
          element in both states: the label joins the arrow, it does not
          replace the button. */}
      <Glass
        as="button"
        variant="chip"
        radius={16}
        className={`${label ? 'h-8 pl-2.5 pr-3 gap-1.5 text-[12px] font-medium whitespace-nowrap' : 'w-8 h-8'} rounded-full flex items-center justify-center cursor-pointer pointer-events-auto transition-all duration-200 glass-hover text-text active:scale-95 active:duration-75 shadow-md`}
        onPointerDown={() => { pointerPressedRef.current = true }}
        onKeyDown={() => { pointerPressedRef.current = false }}
        onBlur={() => { pointerPressedRef.current = false }}
        onClick={onClick}
        aria-label={label ?? (up ? i18nT('pages.chatPage.scroll_to_latest') : i18nT('pages.chatPage.scroll_to_bottom'))}
        data-testid="jump-to-bottom"
      >{up ? <ArrowUp size={14} strokeWidth={2.5} /> : <ArrowDown size={14} strokeWidth={2.5} />}{label && <span>{label}</span>}</Glass>
    </div>
  )
}
