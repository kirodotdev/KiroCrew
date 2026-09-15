import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { useAppSelector } from '../store'
import { useScrollEdges } from '../hooks/useScrollEdges'
import {
  TAB_STATUS_COLOR,
  tabStatus,
  tabStatusPulses,
  truncateTabTitle,
  type TabStatus,
} from '../lib/sessionTabs'
import { offlineProps } from '../utils/offline'
import { i18nT } from '../i18n/t'

/**
 * Status → catalog key. A STATIC table, not a computed `status_${s}` key: the
 * i18n gate resolves key references literally, and a template-literal key is
 * invisible to it — so a status whose string was never added would ship as the
 * raw key rather than failing CI.
 */
const STATUS_LABEL_KEY: Record<Exclude<TabStatus, 'idle'>, string> = {
  running: 'components.sessionTabStrip.status_running',
  unread: 'components.sessionTabStrip.status_unread',
  permission: 'components.sessionTabStrip.status_permission',
  question: 'components.sessionTabStrip.status_question',
}

/** How long the replacement outline is held at full strength, then faded. */
const FLASH_HOLD_MS = 700
const FLASH_FADE_MS = 400

/**
 * The open-sessions strip above the chat transcript: a working set the user
 * assembles, one visible at full width, switched in one click or one keystroke.
 *
 * WHY IT CAN BE ABSENT. It renders nothing below two tabs, and nothing is what
 * every user gets until they deliberately open a second session here. That is
 * the whole compatibility story for the surface: no new bar, no reclaimed
 * height, no moved transcript for anyone who does not use it — checked in one
 * place (the early return) rather than at each call site.
 *
 * WHY NOT REUSE `EmbedTabStrip`. That strip belongs to the embed shell: it
 * navigates `/embed/chat/:slug` routes, seeds a slug-less "sessions" tab, and
 * owns pointer-drag reordering against `sessionStorage`. Here the same visual
 * affordance has to drive Redux `switchSlot` on the host route and must not
 * carry the placeholder tab (this surface always has a session). The parts that
 * are genuinely the same — status precedence, its colour vocabulary, the
 * close-index arithmetic, title truncation — live in `lib/sessionTabs` and both
 * strips call them, so the shared behaviour has one owner and the two shells
 * stay separate.
 */
export default function SessionTabStrip({ tabs, activeKey, cue, connected = true, onSelect, onClose }: {
  tabs: readonly string[]
  activeKey: string | null
  /** A tab worth looking at — a swap, or a re-request. See `SessionTabsApi.cue`. */
  cue?: { key: string; at: number } | null
  /**
   * Gateway reachability. Offline, activating a tab is withheld (an offline
   * switch clears the transcript), so the tabs are marked aria-disabled and say
   * why rather than silently ignoring the click.
   */
  connected?: boolean
  onSelect: (key: string) => void
  onClose: (key: string) => void
}) {
  const slots = useAppSelector(s => s.dashboard.slots)
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  const [attachEdges, edges, remeasure] = useScrollEdges<HTMLDivElement>()
  const tabRefs = useRef<Record<string, HTMLDivElement | null>>({})

  /**
   * Brief outline on a tab whose session was swapped in place. A plain sidebar
   * click is the DEFAULT gesture, and the swap keeps the tab's position and
   * changes only its label — so without a cue the tab the user deliberately
   * opened is not perceived as consumed, it is discovered missing later.
   *
   * Outline + a colour transition to transparent, removed by JS afterwards —
   * the same treatment (and the same reason) as the sidebar's reveal flash: a
   * colour transition survives the global reduced-motion animation kill switch,
   * so the confirmation still lands for a user who suppresses motion.
   */
  const [flash, setFlash] = useState<{ key: string; fading: boolean } | null>(null)
  const cueKey = cue?.key
  const cueAt = cue?.at
  useEffect(() => {
    if (!cueKey) return
    setFlash({ key: cueKey, fading: false })
    const fade = setTimeout(() => setFlash(f => (f ? { ...f, fading: true } : f)), FLASH_HOLD_MS)
    const clear = setTimeout(() => setFlash(null), FLASH_HOLD_MS + FLASH_FADE_MS)
    return () => { clearTimeout(fade); clearTimeout(clear) }
  }, [cueKey, cueAt])

  // Opening, closing and auto-titling all change the strip's CONTENT width
  // while its own box is unchanged, so neither the ResizeObserver nor a scroll
  // event fires — only an explicit remeasure refreshes the overflow cues.
  // Same reasoning (and same `slots` dependency) as the embed strip.
  useEffect(() => { remeasure() }, [tabs, slots, remeasure])

  // Keep the active tab on screen: it can be scrolled out by opening tabs, and
  // a keyboard switch would otherwise move the selection somewhere invisible.
  useEffect(() => {
    if (!activeKey) return
    tabRefs.current[activeKey]?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [activeKey, tabs])

  /**
   * Roving arrow navigation, WAI-ARIA "tabs with automatic activation": the
   * arrows both move focus and switch session, so a keyboard user needs no
   * separate commit keystroke. Delete/Backspace closes the focused tab — the
   * pattern's answer to a close control that is a nested button, which a
   * screen-reader user reaches only by stepping into the tab.
   */
  const onKeyDown = useCallback((e: React.KeyboardEvent, key: string) => {
    const at = tabs.indexOf(key)
    if (at === -1) return
    let target: string | undefined
    if (e.key === 'ArrowLeft') target = tabs[at - 1]
    else if (e.key === 'ArrowRight') target = tabs[at + 1]
    else if (e.key === 'Home') target = tabs[0]
    else if (e.key === 'End') target = tabs[tabs.length - 1]
    else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault()
      // Hand focus on BEFORE unmounting the tab that holds it. Without this the
      // closed tab's node goes away with focus on it, focus falls to
      // document.body, and the keyboard user who just used the strip's own close
      // key has to Tab through the whole page to get back. Below two remaining
      // tabs the strip itself unmounts, so there is no tab to land on and the
      // caller's own post-close focus (the transcript) is the only sane target.
      const landing = tabs.filter(k => k !== key)
      if (landing.length >= 2) {
        const next = landing[Math.min(at, landing.length - 1)]
        tabRefs.current[next]?.focus()
      }
      onClose(key)
      return
    } else if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onSelect(key)
      return
    }
    if (!target) return
    e.preventDefault()
    onSelect(target)
    tabRefs.current[target]?.focus()
  }, [tabs, onSelect, onClose])

  if (tabs.length < 2) return null

  return (
    <div className="relative shrink-0 min-w-0 border-b border-border" style={{ background: 'var(--bg)' }}>
      <div
        ref={attachEdges}
        role="tablist"
        aria-label={i18nT('components.sessionTabStrip.open_sessions')}
        aria-orientation="horizontal"
        data-testid="session-tab-strip"
        className="flex items-center gap-1 overflow-x-auto px-1.5 py-1.5"
        style={{ scrollbarWidth: 'none' }}
      >
        {tabs.map(key => {
          const slot = slots.find(s => s.key === key)
          const title = slot?.title || key
          const active = key === activeKey
          const status = tabStatus(slot, unreadSlots, key)
          // The dot is decorative (aria-hidden) and colour is its only channel,
          // so the STATUS WORD has to ride the tab's own accessible name and
          // tooltip: without it a screen-reader user gets no status at all, and
          // "needs you" vs "idle" is two greys apart for a colourblind user —
          // which would defeat the point of ranking a blocked session above a
          // running one in the first place. 'idle' contributes nothing, so an
          // ordinary tab is still named by its session and nothing else.
          const statusWord = status === 'idle' ? '' : i18nT(STATUS_LABEL_KEY[status])
          const accessibleName = statusWord
            ? i18nT('components.sessionTabStrip.tab_name_with_status', { name: title, status: statusWord })
            : title
          return (
            <div
              key={key}
              ref={el => { tabRefs.current[key] = el }}
              role="tab"
              // Roving tabIndex: one stop for the whole strip, so Tab does not
              // walk N sessions before reaching the transcript.
              tabIndex={active ? 0 : -1}
              aria-selected={active}
              aria-label={accessibleName}
              data-testid={`session-tab-${key}`}
              data-cued={flash?.key === key ? 'true' : undefined}
              title={accessibleName}
              // Offline, activating a tab is withheld because the switch would
              // clear the transcript — so the tab says so instead of swallowing
              // the click. Spread AFTER the online title/aria-label, per this
              // helper's own contract: last prop wins, so spreading it first
              // would leave the refused click showing only the session name and
              // no reason. Closing still works: it is local.
              {...offlineProps(connected, 'switch sessions', accessibleName)}
              onClick={() => onSelect(key)}
              // Middle-click closes, the other half of the convention this PR
              // already imports for OPENING on a sidebar row. A user who learned
              // the open gesture will try it here, and getting nothing reads as
              // the strip being broken rather than the gesture being unbound.
              onAuxClick={e => {
                if (e.button !== 1) return
                e.preventDefault()
                onClose(key)
              }}
              onKeyDown={e => onKeyDown(e, key)}
              style={flash?.key === key ? {
                outline: '2px solid var(--accent)',
                outlineOffset: '-2px',
                transition: `outline-color ${FLASH_FADE_MS}ms`,
                ...(flash.fading ? { outlineColor: 'transparent' } : {}),
              } : undefined}
              className={`group/tab flex items-center gap-2 px-2 py-1.5 rounded-md select-none shrink-0 text-xs border ${connected ? 'cursor-pointer' : 'cursor-not-allowed'} ${
                active
                  ? 'bg-bg-elevated text-text border-border'
                  : 'text-muted hover:text-text hover:bg-bg-elevated/50 border-transparent'
              }`}
            >
              <span
                aria-hidden="true"
                data-testid={`session-tab-status-${status}`}
                className={`shrink-0 w-1.5 h-1.5 rounded-full ${tabStatusPulses(status) ? 'animate-pulse' : ''}`}
                style={{ background: TAB_STATUS_COLOR[status] }}
              />
              <span className="whitespace-nowrap">{truncateTabTitle(title)}</span>
              <button
                type="button"
                // Closing a TAB is not closing the session — the session stays
                // in the sidebar, so the label says so rather than reusing the
                // sidebar's own destructive "Close session".
                aria-label={i18nT('components.sessionTabStrip.close_tab_name', { name: title })}
                title={i18nT('components.sessionTabStrip.close_tab_hint')}
                onClick={e => { e.stopPropagation(); onClose(key) }}
                className={`bg-transparent border-none p-0 leading-none cursor-pointer transition-opacity ${
                  active
                    ? 'opacity-60 hover:opacity-100 hover:text-text'
                    : 'opacity-0 group-hover/tab:opacity-60 group-focus-within/tab:opacity-60 hover:!opacity-100 hover:text-text'
                }`}
              >
                <X size={11} />
              </button>
            </div>
          )
        })}
      </div>
      {/* The scroller hides its scrollbar, so a gradient is the only signal
          that tabs continue past the clipped edge. Same treatment as the
          sibling strips. */}
      {edges.left && (
        <div aria-hidden="true" data-testid="session-tab-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-r from-bg to-transparent" />
      )}
      {edges.right && (
        <div aria-hidden="true" data-testid="session-tab-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-l from-bg to-transparent" />
      )}
    </div>
  )
}
