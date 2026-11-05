import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, ExternalLink, Lightbulb, X } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { useReducedMotion } from '../hooks/useReducedMotion'
import MarkdownRenderer from './MarkdownRenderer'

import { i18nT } from '../i18n/t'
import { Glass } from './Glass'
import { Btn, IconButton } from './ui'

// The resolver lives in `utils/docsLink`: a pure function must not live behind a
// component module, or importing it drags this file's router and markdown
// dependencies along with it.
import { tipDocHref } from '../utils/docsLink'

// Optional one-click action button on a tip. A single 'route' kind for now:
// navigate to an internal dashboard path (an exact settings tab/control via
// ?highlight=, a page, etc.).
export interface TipAction {
  kind: 'route'
  label: string
  route: string
}

// Tips can be LLM-authored, so validate a nav target the same defensive way the
// doc link is validated (tipDocHref): only an INTERNAL path (leading '/', not
// '//', no scheme) is allowed, so an action can never drive an off-origin or
// open-redirect navigation. Returns the safe route, or null (no button).
export function tipActionRoute(action: TipAction | null | undefined): string | null {
  if (!action || action.kind !== 'route') return null
  const { route, label } = action
  if (typeof route !== 'string' || typeof label !== 'string' || !label.trim()) return null
  if (!route.startsWith('/') || route.startsWith('//') || route.includes('://')) return null
  return route
}

export interface Tip {
  id: string
  feature: string
  title: string
  body: string
  why: string
  doc: string
  // Rendering-only "learn more" target. `doc` doubles as the tip's dismissal
  // identity on the backend, so a tip that links a doc it does not own for
  // dismissal purposes carries the link here instead.
  doc_link?: string
  cta_prompt: string
  action?: TipAction | null
}

interface TipCardProps {
  tip: Tip
  onDismiss: () => void
}

export function TipCard({ tip, onDismiss }: TipCardProps) {
  const queryClient = useQueryClient()
  const feedbackMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'dismiss' }) =>
      api.tipsFeedback(id, action),
    onSuccess: () => {
      queryClient.setQueryData(['tips-next'], null)
      onDismiss()
    },
  })

  const handleDismiss = useCallback(() => {
    if (feedbackMutation.isPending) return
    feedbackMutation.mutate({ id: tip.id, action: 'dismiss' })
  }, [tip.id, feedbackMutation])

  // Permanent opt-out (same action the Settings → Chat toggle uses). The
  // backend accepts an empty id for optout; on success the toggle's cached
  // status is refreshed so Settings reflects the change immediately.
  const optOutMutation = useMutation({
    mutationFn: () => api.tipsFeedback('', 'optout'),
    onSuccess: () => {
      queryClient.setQueryData(['tips-next'], null)
      queryClient.invalidateQueries({ queryKey: ['tipsStatus'] })
      onDismiss()
    },
  })

  const handleOptOut = useCallback(() => {
    if (optOutMutation.isPending) return
    optOutMutation.mutate()
  }, [optOutMutation])

  const navigate = useNavigate()
  const tooltipText = tip.why ? `${tip.title} — ${tip.why}` : tip.title
  // Prefer the rendering-only doc_link; fall back to doc for tips that still
  // legitimately use it for both jobs (catalog fallback tips). Whichever value
  // is used goes through tipDocHref's filename-shape validation.
  const docHref = tipDocHref(tip.doc_link || tip.doc)
  const actionRoute = tipActionRoute(tip.action)

  const handleAction = useCallback(() => {
    if (!actionRoute) return
    // Count the click as engagement (distinct from dismiss) so cadence /
    // analytics can tell "acted" from "closed". Fire-and-forget — navigation
    // must not depend on the request.
    api.tipsFeedback(tip.id, 'ack').catch(() => {})
    queryClient.setQueryData(['tips-next'], null)
    navigate(actionRoute)
    onDismiss()
  }, [actionRoute, tip.id, navigate, onDismiss, queryClient])

  // A tip opens as a one-line pill (lightbulb, title, chevron, close) so it
  // never crowds the composer; the full card is one click away. The open state
  // is keyed by tip id, so the next tip starts closed again.
  const [openTipId, setOpenTipId] = useState<string | null>(null)
  const open = openTipId === tip.id

  // The pressed toggle leaves the DOM when the state flips (the pill's title
  // button becomes the card's collapse button and back), so keyboard and
  // screen-reader focus would fall to <body>. Move it to the other state's
  // toggle, but only after a user toggle: a first render or a new tip never
  // takes focus away from the composer.
  const pillToggleRef = useRef<HTMLButtonElement>(null)
  const collapseRef = useRef<HTMLButtonElement>(null)
  const focusAfterToggleRef = useRef<'card' | 'pill' | null>(null)
  const openCard = useCallback(() => {
    focusAfterToggleRef.current = 'card'
    setOpenTipId(tip.id)
  }, [tip.id])
  const collapseCard = useCallback(() => {
    focusAfterToggleRef.current = 'pill'
    setOpenTipId(null)
  }, [])
  useEffect(() => {
    const target = focusAfterToggleRef.current
    focusAfterToggleRef.current = null
    if (target === 'card' && open) collapseRef.current?.focus()
    else if (target === 'pill' && !open) pillToggleRef.current?.focus()
  }, [open])

  // The pill grows into the card: one persistent pane animated with a layout
  // transition (measured only when `open` flips), so the user sees the same
  // element change shape. Reduced motion drops the animation, not the single
  // element.
  //
  // A layout animation is a scale transform on the pane, so anything inside it
  // would be squashed while the box grows. Each state's content therefore sits
  // in its own `layout="position"` node: framer counter-scales it, so text,
  // icons and buttons render at true size throughout, and it fades in so the
  // incoming state never reflows in view. The glass surface itself is not
  // corrected (Glass owns its radius inline on the host and on every layer),
  // so its corners stretch slightly for the 0.25s of the transition.
  const reduceMotion = useReducedMotion()
  const paneTransition = reduceMotion
    ? { duration: 0 }
    : { duration: 0.25, ease: [0.2, 0.8, 0.2, 1] as const }
  const contentMotion = reduceMotion
    ? { layout: false as const, initial: false as const }
    : {
        layout: 'position' as const,
        layoutDependency: open,
        initial: { opacity: 0 },
        animate: { opacity: 1 },
        transition: { opacity: { duration: 0.15, ease: 'easeOut' as const }, layout: paneTransition },
      }

  const dismissButton = (
    <IconButton
      onClick={handleDismiss}
      disabled={feedbackMutation.isPending}
      aria-label={i18nT('components.tipCard.dismiss_tip')}
    >
      <X size={13} aria-hidden="true" />
    </IconButton>
  )

  return (
    <motion.div
      className="w-full"
      initial={{ y: 6, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: 4, opacity: 0 }}
      transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
    >
    {/* Neutral glass (components/Glass.tsx, chip variant, the composer's own
        primitive) with `.glass-shadow` for the same depth as the composer it
        sits on. The accent stays on the lightbulb and the action button, so the
        tip does not out-shout the composer. */}
    <motion.div
      layout={!reduceMotion}
      layoutDependency={open}
      transition={paneTransition}
      className={open ? 'flex w-full' : 'flex min-w-0 w-fit max-w-full'}
    >
    {!open ? (
        <Glass
          variant="chip"
          radius={999}
          data-testid="tip-card"
          className="glass-shadow inline-flex items-center max-w-full pl-1 pr-1 py-0.5 text-[12px]"
          role="complementary"
          aria-label={i18nT('components.tipCard.feature_tip')}
          title={tooltipText}
        >
          <motion.div key="pill" {...contentMotion} className="flex items-center gap-1 min-w-0 max-w-full">
          <button
            ref={pillToggleRef}
            type="button"
            onClick={openCard}
            aria-expanded={false}
            className="inline-flex items-center gap-2 min-w-0 pl-2 pr-1 py-1 rounded-full bg-transparent border-none cursor-pointer text-text hover:bg-bg-hover transition-colors"
          >
            <Lightbulb size={13} className="shrink-0 text-accent" aria-hidden="true" />
            <span className="font-medium truncate">{tip.title}</span>
            <ChevronDown size={13} className="shrink-0 text-muted" aria-hidden="true" />
          </button>
          <span className="w-px h-3.5 bg-border shrink-0" aria-hidden="true" />
          {dismissButton}
          </motion.div>
        </Glass>
    ) : (
      <Glass
        variant="chip"
        radius={12}
        data-testid="tip-card"
        className="glass-shadow w-full flex"
        role="complementary"
        aria-label={i18nT('components.tipCard.feature_tip')}
        title={tooltipText}
      >
        {/* Not counter-scaled, so it clips to the growing pane: while the box
            opens from pill size, the true-size card content is revealed by the
            pane's edge instead of spilling past it. The padding lives here so
            focus rings near the edge stay inside the clip. */}
        <div className="flex-1 min-w-0 overflow-hidden px-4 py-3.5">
        <motion.div key="card" {...contentMotion} className="flex items-start gap-3 w-full">
        <span
          className="shrink-0 grid place-items-center w-7 h-7 rounded-md"
          style={{ background: 'color-mix(in srgb, var(--accent) 14%, transparent)' }}
          aria-hidden="true"
        >
          <Lightbulb size={15} className="text-accent" />
        </span>

        <div className="min-w-0 flex-1 max-w-[720px]">
          <span className="block text-[14px] font-semibold leading-5 py-1 text-text">{tip.title}</span>
          {/* Full multi-line body, no truncation. A viewport-relative
              max-height with scroll keeps a very long body from pushing the
              bottom-anchored card past the viewport on narrow screens. Rendered
              through the sanitized markdown pipeline so inline `code` and
              **bold** display styled; the arbitrary variants strip block
              margins to keep the card compact. */}
          <div
            data-testid="tip-body"
            className="text-[13px] leading-relaxed text-muted break-words overflow-y-auto max-h-[30vh] [&_p]:m-0 [&_pre]:my-1 [&_ul]:my-0 [&_ol]:my-0"
          >
            <MarkdownRenderer content={tip.body} />
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mt-2.5">
            {actionRoute && (
              <Btn primary onClick={handleAction} className="text-[12px]">
                {tip.action?.label}
              </Btn>
            )}
            {docHref && (
              <a
                href={docHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline"
              >
                <ExternalLink size={11} aria-hidden="true" />
                {i18nT('components.tipCard.learn_more')}
              </a>
            )}
            <button
              type="button"
              onClick={handleOptOut}
              disabled={optOutMutation.isPending}
              className="text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {i18nT('components.tipCard.turn_off_tips')}
            </button>
          </div>
        </div>

        <div className="flex items-center gap-0.5 shrink-0 ml-auto -mr-1 -mt-0.5">
          <IconButton
            ref={collapseRef}
            onClick={collapseCard}
            aria-expanded={true}
            aria-label={i18nT('components.tipCard.collapse_tip')}
          >
            <ChevronUp size={13} aria-hidden="true" />
          </IconButton>
          {dismissButton}
        </div>
        </motion.div>
        </div>
      </Glass>
    )}
    </motion.div>
    </motion.div>
  )
}

/**
 * Hook: manages tip fetching and display logic for the chat view.
 *
 * `suppressed` — true while a functional surface (queued-message stack,
 * question card, knowledge picker…) occupies the above-composer band.
 */
export function useTipTrigger(isRunning: boolean, suppressed = false, slotKey: string | null = null, blocked = false) {
  const [visible, setVisible] = useState(false)
  const shownThisTurnRef = useRef(false)
  const [enabled, setEnabled] = useState(false)
  const queryClient = useQueryClient()

  // Reset ALL per-turn state when the active slot changes:
  // switching between two running slots keeps isRunning=true, so without this
  // the visible strip, the armed 10s gate, and shownThisTurnRef would leak
  // into the newly selected slot and a tip could appear there instantly.
  useEffect(() => {
    setVisible(false)
    setEnabled(false)
    shownThisTurnRef.current = false
    queryClient.removeQueries({ queryKey: ['tips-next'] })
  }, [slotKey, queryClient])

  useEffect(() => {
    if (isRunning) {
      shownThisTurnRef.current = false
      setEnabled(false)
    } else {
      setVisible(false)
      setEnabled(false)
      shownThisTurnRef.current = false
      queryClient.removeQueries({ queryKey: ['tips-next'] })
    }
  }, [isRunning, queryClient])

  useEffect(() => {
    if (suppressed) setVisible(false)
  }, [suppressed])

  // Temporary sessions forbid memory reads: tips are
  // memory-personalized, so fetching or displaying one would leak persistent
  // memory into a blank-slate session. Hard-block everything while blocked.
  useEffect(() => {
    if (blocked) {
      setVisible(false)
      setEnabled(false)
      queryClient.removeQueries({ queryKey: ['tips-next'] })
    }
  }, [blocked, queryClient])

  // Client polling gate: min(20min UI floor, configured server cadence).
  // Default posture (cadence 6h) keeps the 20-minute floor; explicitly
  // configuring tips_cadence_hours below 20 minutes makes the client follow
  // it so valid low-cadence settings actually take effect.
  const { data: tipsStatus } = useQuery({
    queryKey: ['tipsStatus'],
    queryFn: api.tipsStatus,
    staleTime: 20 * 60 * 1000,
    retry: false,
  })
  const clientGateMs = Math.min(
    20 * 60 * 1000,
    Math.max(0, (tipsStatus?.cadence_hours ?? 20 / 60) * 60 * 60 * 1000),
  )

  useEffect(() => {
    if (!isRunning || blocked) return
    const timer = setTimeout(() => {
      if (shownThisTurnRef.current) return
      const lastShown = safeGetItem('kirocrew.tips.lastShownAt')
      if (lastShown && Date.now() - parseInt(lastShown, 10) < clientGateMs) return
      setEnabled(true)
    }, 10000)
    return () => clearTimeout(timer)
  }, [isRunning, slotKey, clientGateMs, blocked])

  const { data: tipResponse } = useQuery({
    queryKey: ['tips-next'],
    queryFn: api.tipsNext,
    enabled: enabled && isRunning && !suppressed && !blocked && !shownThisTurnRef.current,
    staleTime: 20 * 60 * 1000,
    retry: false,
  })

  // Unwrap fork's {tip, glow} response shape
  const tip = tipResponse?.tip ?? null

  useEffect(() => {
    if (tip && enabled && isRunning && !suppressed && !blocked && !shownThisTurnRef.current) {
      setVisible(true)
      shownThisTurnRef.current = true
      safeSetItem('kirocrew.tips.lastShownAt', String(Date.now()))
      // Tell the backend the tip was actually displayed: starts the server-side
      // cadence gate and releases the offered slot (without dismissing), so
      // passive users who never click ✕ don't get the same tip re-served every
      // turn. Fire-and-forget — display must not depend on this call.
      api.tipsFeedback(tip.id, 'shown').catch(() => {})
    }
  }, [tip, enabled, isRunning, suppressed, blocked])

  // Drop any cached tip when the hook unmounts (navigating away from Chat):
  // a tip cached mid-turn must not survive to a remount where the 10s gate
  // and the user's current opt-out preference haven't been re-evaluated.
  useEffect(() => {
    return () => {
      queryClient.removeQueries({ queryKey: ['tips-next'] })
    }
  }, [queryClient])

  const dismiss = useCallback(() => {
    setVisible(false)
  }, [])

  // `blocked` is checked synchronously here (not only via the reset effect):
  // effects run after render, so on the first frame after switching from a
  // running persistent slot to a running temporary slot the stale tip would
  // otherwise flash before the reset effect fires.
  return { tip: visible && !suppressed && !blocked ? tip ?? null : null, dismiss }
}
