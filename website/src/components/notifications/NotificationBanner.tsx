import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { AnimatePresence, motion, useReducedMotion, type TargetAndTransition } from 'framer-motion'
import { i18nT } from '../../i18n/t'
import { useAppDispatch, useAppSelector } from '../../store'
import { ackNotification } from '../../store/notificationsSlice'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useGuardedLeave } from '../NavigationLeaveGuard'
import Clickable from '../Clickable'
import ErrorNotice from '../ErrorNotice'
import { MC_LIVE_NOTIFICATION_EVENT, type McLiveNotificationDetail } from '../../hooks/notificationEvent'
import {
  BANNER_AUTO_HIDE_MS, BANNER_DECK_DEPTH, BANNER_EXPANDED_MAX, MC_BANNER_SETTING_CHANGED_EVENT,
  loadBannerEnabled, shouldBannerNote,
} from '../../hooks/notificationBanner'
import type { Notification } from '../../types'
import { notePriority, safeInternalUrl } from './notifMeta'
import NotificationCard, { CARD_MATERIAL, type NotificationCardAction } from './NotificationCard'

/** Where a leaving card travels: the vector from its own top-right corner to
 *  the bell's centre, so with `transform-origin: top right` the card shrinks
 *  INTO the bell rather than merely fading near it. */
export interface ExitDelta { dx: number; dy: number }

export function computeExitDelta(card: DOMRect, bell: DOMRect): ExitDelta {
  return {
    dx: bell.left + bell.width / 2 - card.right,
    dy: bell.top + bell.height / 2 - card.top,
  }
}

/** The exit target for one card. Under reduced motion the relocation is a plain
 *  fade — the note is still in the bell, the continuity is the unread dot that
 *  lights as the card goes — and a card with no measured delta (bell unmounted,
 *  first paint) fades too rather than flying to a guessed point. */
export function exitTarget(delta: ExitDelta | undefined, reduced: boolean): TargetAndTransition {
  if (reduced || !delta) return { opacity: 0, transition: { duration: 0.18 } }
  return {
    x: delta.dx, y: delta.dy, scale: 0.15, opacity: 0, originX: 1, originY: 0,
    transition: { duration: 0.26, ease: [0.4, 0, 1, 1] },
  }
}

/** Deck geometry per depth behind the top card: offset, scale, opacity. */
const DECK_Y = [0, 4, 8]
const DECK_SCALE = [1, 0.98, 0.96]
const DECK_OPACITY = [1, 0.8, 0.55]

type ExitDeltas = Record<string, ExitDelta | undefined>

/**
 * macOS Notification Center-style banner for a LIVE notification: one card
 * under the top bar, right-aligned with the bell, that auto-hides by
 * travelling into the bell (leaving the note unread there) or stays until acted
 * on when critical. Several pending cards stack newest-on-top as a deck the
 * user can expand.
 *
 * Arrival comes from `MC_LIVE_NOTIFICATION_EVENT`, never from the store: the
 * store also fills from the boot snapshot and reconnect replays, which are
 * history, not news. Every other suppression (preference off, silenced or
 * passive note, popover open, on /notifications, the note describes the view
 * already on screen) is `shouldBannerNote`, evaluated at arrival against refs
 * so the listener never re-subscribes.
 *
 * The host (the bell button) lends its `bellRef` for the exit vector and its
 * open-on-note mechanics; the banner owns no selection state of its own.
 */
export default function NotificationBanner({ bellRef, popoverOpen, onOpenNote }: {
  bellRef: RefObject<HTMLButtonElement | null>
  /** The bell popover is on screen (open or animating shut). */
  popoverOpen: boolean
  /** Open the bell popover with this note selected (the host acks it). */
  onOpenNote: (ts: string) => void
}) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const location = useLocation()
  const isMobile = useIsMobile()
  const reduced = !!useReducedMotion()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)

  const [pending, setPending] = useState<Notification[]>([])
  const [expanded, setExpanded] = useState(false)
  const [enabled, setEnabled] = useState(loadBannerEnabled)
  // ts → a failed mark-as-read the card is still showing. Keyed by note so a
  // second card's success never clears the first card's notice.
  const [ackFailed, setAckFailed] = useState<Record<string, true>>({})

  // Arrival-time context, read by the event listener through refs so the
  // listener subscribes once and still judges each note against the CURRENT
  // route, popover phase, and preference.
  const ctxRef = useRef({ enabled, popoverOpen, pathname: location.pathname, activeSlot })
  ctxRef.current = { enabled, popoverOpen, pathname: location.pathname, activeSlot }

  const cardEls = useRef(new Map<string, HTMLElement>())
  // Mutated in place right before a removal and handed to AnimatePresence as
  // `custom`, so the exit variant of each leaving card reads the vector that
  // was measured while the card was still on screen.
  const exitDeltas = useRef<ExitDeltas>({})

  // ---- shared auto-hide timer ------------------------------------------------
  const timer = useRef<number | null>(null)
  const deadline = useRef(0)
  const remaining = useRef<number | null>(null)
  const paused = useRef(false)
  const pendingRef = useRef(pending)
  pendingRef.current = pending

  const measureExit = useCallback((tsList: string[]) => {
    const bell = bellRef.current?.getBoundingClientRect()
    for (const ts of tsList) {
      const el = cardEls.current.get(ts)
      exitDeltas.current[ts] = bell && el ? computeExitDelta(el.getBoundingClientRect(), bell) : undefined
    }
  }, [bellRef])

  const clearTimer = useCallback(() => {
    if (timer.current !== null) { window.clearTimeout(timer.current); timer.current = null }
  }, [])

  const removeNotes = useCallback((tsList: string[]) => {
    if (tsList.length === 0) return
    measureExit(tsList)
    const gone = new Set(tsList)
    // Derived from the ref, and written back to it, so two removals in one
    // tick (a click that also opens the popover) compose instead of the
    // second one reading the first one's stale list.
    const next = pendingRef.current.filter(n => !gone.has(n.ts))
    pendingRef.current = next
    setPending(next)
    if (next.length <= 1) setExpanded(false)
  }, [measureExit])

  const fireAutoHide = useCallback(() => {
    timer.current = null
    remaining.current = null
    removeNotes(pendingRef.current.filter(n => notePriority(n) !== 'critical').map(n => n.ts))
  }, [removeNotes])

  const armTimer = useCallback((ms: number) => {
    clearTimer()
    deadline.current = Date.now() + ms
    timer.current = window.setTimeout(fireAutoHide, ms)
  }, [clearTimer, fireAutoHide])

  // One timer for every default-priority card, restarted by each arrival.
  const restartAutoHide = useCallback(() => {
    if (paused.current) { remaining.current = BANNER_AUTO_HIDE_MS; return }
    remaining.current = null
    armTimer(BANNER_AUTO_HIDE_MS)
  }, [armTimer])

  const pause = useCallback(() => {
    if (paused.current) return
    paused.current = true
    if (timer.current !== null) {
      remaining.current = Math.max(0, deadline.current - Date.now())
      clearTimer()
    }
  }, [clearTimer])

  const resume = useCallback(() => {
    if (!paused.current) return
    paused.current = false
    const hasDefault = pendingRef.current.some(n => notePriority(n) !== 'critical')
    if (hasDefault && remaining.current !== null) {
      armTimer(remaining.current)
      remaining.current = null
    }
  }, [armTimer])

  useEffect(() => clearTimer, [clearTimer])

  // ---- arrival -----------------------------------------------------------------
  useEffect(() => {
    const onLive = (e: Event) => {
      const note = (e as CustomEvent<McLiveNotificationDetail>).detail?.note
      if (!note || !note.ts) return
      const ctx = ctxRef.current
      const ok = shouldBannerNote(note, {
        ...ctx,
        windowFocused: typeof document !== 'undefined' && document.hasFocus() && !document.hidden,
      })
      if (!ok) return
      const next = [note, ...pendingRef.current.filter(n => n.ts !== note.ts)]
      pendingRef.current = next
      setPending(next)
      if (notePriority(note) !== 'critical') restartAutoHide()
    }
    window.addEventListener(MC_LIVE_NOTIFICATION_EVENT, onLive)
    return () => window.removeEventListener(MC_LIVE_NOTIFICATION_EVENT, onLive)
  }, [restartAutoHide])

  // ---- preference, live -------------------------------------------------------
  useEffect(() => {
    const reload = () => setEnabled(loadBannerEnabled())
    const onStorage = (e: StorageEvent) => {
      try { if (e.storageArea && e.storageArea !== localStorage) return } catch { /* locked-down storage */ }
      reload()
    }
    window.addEventListener(MC_BANNER_SETTING_CHANGED_EVENT, reload)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(MC_BANNER_SETTING_CHANGED_EVENT, reload)
      window.removeEventListener('storage', onStorage)
    }
  }, [])

  // ---- surfaces that already show the notes ----------------------------------
  // Opening the bell, landing on the inbox page, or switching the banner off
  // retires every pending card: each is now visible elsewhere (or unwanted).
  const onInbox = location.pathname === '/notifications' || location.pathname.startsWith('/notifications/')
  useEffect(() => {
    if ((popoverOpen || onInbox || !enabled) && pendingRef.current.length > 0) {
      clearTimer()
      remaining.current = null
      removeNotes(pendingRef.current.map(n => n.ts))
    }
  }, [popoverOpen, onInbox, enabled, clearTimer, removeNotes])

  // ---- Escape dismisses the topmost card --------------------------------------
  useEffect(() => {
    if (pending.length === 0) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      removeNotes([pending[0].ts])
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [pending, removeNotes])

  // ---- actions ----------------------------------------------------------------
  const openNote = useCallback((n: Notification) => {
    removeNotes([n.ts])
    onOpenNote(n.ts)
  }, [removeNotes, onOpenNote])

  // Everything happens INSIDE the leave guard: a user who answers "stay" to
  // the unsaved-draft prompt keeps an unread note and a card still on screen.
  // The ack is awaited before the card goes, so a rejected ack leaves the
  // card in place with a notice instead of a vanished card and a note the
  // server still holds unread.
  const runAction = useCallback((n: Notification, url: string) => {
    leave(async () => {
      const result = await dispatch(ackNotification(n.ts))
      if (ackNotification.rejected.match(result)) {
        setAckFailed(prev => ({ ...prev, [n.ts]: true }))
        return
      }
      setAckFailed(prev => { const { [n.ts]: _drop, ...rest } = prev; void _drop; return rest })
      removeNotes([n.ts])
      navigate(url)
    }, url)
  }, [dispatch, removeNotes, leave, navigate])

  // "+N more in your inbox" goes where the popover's own "Open inbox" goes --
  // the /notifications page -- so the word names one place. Guarded like every
  // other navigation out of a floating surface.
  const openInbox = useCallback(() => {
    leave(() => {
      removeNotes(pendingRef.current.map(n => n.ts))
      navigate('/notifications')
    }, '/notifications')
  }, [removeNotes, leave, navigate])

  const anyCritical = pending.some(n => notePriority(n) === 'critical')
  // Mobile shows the newest card alone: there is no room for a deck, and the
  // bell is one tap away for the rest.
  const visible = isMobile ? pending.slice(0, 1) : expanded ? pending.slice(0, BANNER_EXPANDED_MAX) : pending.slice(0, 1 + BANNER_DECK_DEPTH)
  const overflow = expanded && !isMobile ? pending.length - visible.length : 0
  const deckHidden = !expanded && !isMobile ? pending.length - 1 : 0
  const moreLabel = i18nT('components.notifications.notificationBanner.show_more_notifications_count', { count: deckHidden })

  const setCardEl = (ts: string) => (el: HTMLElement | null) => {
    if (el) cardEls.current.set(ts, el); else cardEls.current.delete(ts)
  }

  const enterInitial = reduced ? { opacity: 0 } : { x: 40, opacity: 0 }

  return (
    // The live region exists whether or not a card is showing, so assistive
    // tech has a region to announce INTO when the first card lands. `alert`
    // (assertive) only while a critical card is pending; otherwise `status`.
    <div
      role={anyCritical ? 'alert' : 'status'}
      aria-live={anyCritical ? 'assertive' : 'polite'}
      data-testid="notification-banner-region"
      data-banner-count={pending.length}
      className={`fixed z-[59] pointer-events-none top-safe-offset-[50px] ${isMobile ? 'left-safe-offset-3 right-safe-offset-3' : 'right-safe-offset-3 w-[340px]'}`}
    >
      <div
        className={`relative pointer-events-auto ${expanded ? 'flex flex-col gap-2' : ''}`}
        onPointerEnter={pause}
        onPointerLeave={resume}
        onFocusCapture={pause}
        onBlurCapture={e => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) resume() }}
      >
        <AnimatePresence custom={exitDeltas.current} initial={false}>
          {visible.map((n, idx) => {
            const prio = notePriority(n)
            const deck = !expanded && !isMobile && idx > 0
            const urlActions = (Array.isArray(n.actions) ? n.actions : [])
              .filter(a => typeof a?.id === 'string' && typeof a?.label === 'string' && typeof a?.url === 'string')
              .map(a => ({ ...a, safeUrl: safeInternalUrl(a.url) }))
              .filter(a => a.safeUrl)
              .slice(0, 2)
            const isApproval = n.kind === 'approval' && !n.acked
            // At most two quiet capsules; the last (primary) one accent-tinted.
            // An approval offers a single "Review" that opens the bell on the
            // note rather than inventing an approval path of its own.
            const actions: NotificationCardAction[] = isApproval
              ? [{ id: 'review', label: i18nT('components.notifications.notificationBanner.review_action'), tone: 'accent', onClick: () => openNote(n) }]
              : urlActions.map((a, i) => ({
                id: a.id, label: a.label, tone: i === urlActions.length - 1 ? 'accent' : 'text',
                onClick: () => runAction(n, a.safeUrl!),
              }))
            const variants = {
              exit: (custom: ExitDeltas | undefined) => exitTarget(custom?.[n.ts], reduced),
            }
            return (
              <motion.div
                key={n.ts}
                ref={setCardEl(n.ts)}
                layout={!reduced}
                initial={enterInitial}
                animate={reduced
                  ? { opacity: DECK_OPACITY[deck ? idx : 0] }
                  // Deck cards shrink about the top centre so both side edges
                  // recede evenly; the exit re-anchors to the top-right corner
                  // the travel vector was measured from.
                  : { x: 0, y: deck ? DECK_Y[idx] : 0, scale: deck ? DECK_SCALE[idx] : 1, opacity: DECK_OPACITY[deck ? idx : 0], originX: deck ? 0.5 : 1, originY: 0 }}
                variants={variants}
                exit="exit"
                transition={{ duration: 0.22, ease: 'easeOut' }}
                // A deck card is pinned to the top card's box (inset 0 on the
                // relative stack), so the blank shell always matches its height.
                style={{ zIndex: 10 - idx, ...(deck ? { position: 'absolute', left: 0, right: 0, top: 0, bottom: 0 } : {}) }}
                data-testid="notification-banner-card"
                data-priority={prio}
                data-deck={deck ? 'true' : undefined}
                className="rounded-2xl"
              >
                {deck ? (
                  // A peeking deck card is a BLANK shell -- the card material
                  // and nothing else, as in the chosen mockup -- so no text,
                  // icon or time can print through the translucent top card.
                  // It is one control: "show me the rest".
                  <Clickable
                    aria-label={moreLabel}
                    data-testid="notification-banner-deck-shell"
                    className={`notif-material h-full cursor-pointer rounded-2xl ${CARD_MATERIAL.banner}`}
                    onClick={() => setExpanded(true)}
                  />
                ) : (
                  <NotificationCard
                    n={n}
                    elevation="banner"
                    onOpen={() => openNote(n)}
                    openLabel={i18nT('components.notifications.notificationBanner.open_notification', { title: n.title })}
                    onDismiss={() => removeNotes([n.ts])}
                    dismissLabel={i18nT('components.notifications.notificationBanner.dismiss_notification')}
                    dismissTestId="notification-banner-dismiss"
                    dismissVisible={isMobile}
                    actions={actions}
                    actionsAlign="end"
                    footer={ackFailed[n.ts] ? (
                      /* No hand-off: this card floats over whatever page the
                         user is on, which may hold an unsaved draft (a chat
                         composer, a settings form) that the hand-off's
                         navigation to the chat would destroy. The action
                         button on the card is the retry. */
                      <ErrorNotice
                        variant="inline"
                        testId="notification-banner-ack-failed"
                        message={i18nT('components.notifications.notificationBanner.mark_read_failed')}
                        onDismiss={() => setAckFailed(prev => { const { [n.ts]: _drop, ...rest } = prev; void _drop; return rest })}
                      />
                    ) : null}
                  />
                )}
              </motion.div>
            )
          })}
        </AnimatePresence>
        {overflow > 0 && (
          <Clickable
            className={`notif-material rounded-xl ${CARD_MATERIAL.banner} px-3 py-1.5 text-[12px] text-accent text-center cursor-pointer`}
            onClick={openInbox}
          >{i18nT('components.notifications.notificationBanner.more_in_inbox_count', { count: overflow })}</Clickable>
        )}
        {deckHidden > 0 && (
          // The deck's peeking edges are a few pixels tall, so the "N more"
          // pill on the top card's corner is the deck's discoverable,
          // finger-sized expand control, and the only trace of cards deeper
          // than the deck can show.
          <Clickable
            aria-label={moreLabel}
            data-testid="notification-banner-count"
            className="absolute -top-2 -left-1.5 z-20 h-[18px] px-2 rounded-full bg-accent text-accent-fg text-[10px] font-semibold flex items-center justify-center shadow-sm cursor-pointer whitespace-nowrap"
            onClick={() => setExpanded(true)}
          >{i18nT('components.notifications.notificationBanner.more_count', { count: deckHidden })}</Clickable>
        )}
      </div>
    </div>
  )
}
