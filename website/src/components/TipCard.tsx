import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight, ExternalLink, Lightbulb, Settings, X } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import MarkdownRenderer from './MarkdownRenderer'

import { i18nT } from '../i18n/t'
// The Feature Tips toggle lives in Settings → Chat.
export const TIPS_SETTINGS_PATH = '/settings?tab=chat'

// Tip docs live in the repo at src/kiro_crew/docs/ (same base the Security and
// Discord settings panels link to).
const DOCS_BASE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs'
// Tips can be LLM-generated: only link a doc value shaped like a plain
// markdown filename so an invented value can't produce a weird URL.
const DOC_FILENAME_RE = /^[a-z0-9][a-z0-9._-]*\.md$/i

export function tipDocHref(doc: string | undefined): string | null {
  if (!doc || !DOC_FILENAME_RE.test(doc)) return null
  return `${DOCS_BASE}/${doc}`
}

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
  const docHref = tipDocHref(tip.doc)
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

  return (
    <motion.div
      data-testid="tip-card"
      className="w-full flex items-start gap-2.5 px-4 py-2 rounded-md text-xs shadow-lg"
      style={{
        background: 'color-mix(in srgb, var(--accent) 6%, var(--bg-elevated))',
        border: '1px solid color-mix(in srgb, var(--accent) 12%, transparent)',
      }}
      initial={{ y: 6, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      exit={{ y: 4, opacity: 0 }}
      transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
      role="complementary"
      aria-label={i18nT('components.tipCard.feature_tip')}
      title={tooltipText}
    >
      <Lightbulb size={14} className="shrink-0 mt-0.5" aria-hidden="true" style={{ color: 'var(--accent)' }} />

      <div className="min-w-0 flex-1">
        <span className="block font-medium text-[12px] leading-tight" style={{ color: 'var(--text)' }}>{tip.title}</span>
        {/* Full multi-line body — no truncation. A viewport-relative max-height
            with scroll keeps a very long body from pushing
            the bottom-anchored card past the viewport on narrow screens —
            every character stays reachable, nothing is clipped away.
            Rendered through the sanitized markdown pipeline so inline
            `code` / **bold** in generated bodies display styled instead of
            as literal asterisks and backticks; the arbitrary variants strip
            block margins to keep the strip compact. */}
        <div
          data-testid="tip-body"
          className="text-[12px] leading-snug mt-0.5 break-words overflow-y-auto max-h-[30vh] [&_p]:m-0 [&_pre]:my-1 [&_ul]:my-0 [&_ol]:my-0"
          style={{ color: 'var(--muted)' }}
        >
          <MarkdownRenderer content={tip.body} />
        </div>
        <div className="flex items-center gap-3 mt-1">
          <div className="flex items-center gap-3 min-w-0">
            {actionRoute && (
              <button
                onClick={handleAction}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium hover:brightness-110 transition"
                style={{
                  color: 'var(--accent)',
                  background: 'color-mix(in srgb, var(--accent) 12%, transparent)',
                  border: '1px solid color-mix(in srgb, var(--accent) 30%, transparent)',
                }}
              >
                {tip.action?.label}
                <ArrowRight size={11} aria-hidden="true" />
              </button>
            )}
            {docHref && (
              <a
                href={docHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-[11px] text-accent hover:underline hover:brightness-110 transition"
              >
                <ExternalLink size={10} aria-hidden="true" />
                {i18nT('components.tipCard.learn_more')}
              </a>
            )}
          </div>
          <div className="flex items-center gap-2 ml-auto shrink-0">
            <button
              onClick={handleOptOut}
              disabled={optOutMutation.isPending}
              className="text-[11px] hover:underline disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ color: 'var(--muted)' }}
              aria-label={i18nT('components.tipCard.turn_off_tips')}
            >
              {i18nT('components.tipCard.turn_off_tips')}
            </button>
            <Link
              to={TIPS_SETTINGS_PATH}
              className="inline-flex items-center rounded p-0.5 transition-colors hover:bg-[var(--bg-hover)]"
              style={{ color: 'var(--muted)' }}
              aria-label={i18nT('components.tipCard.tip_settings')}
              title={i18nT('components.tipCard.tip_settings')}
            >
              <Settings size={12} aria-hidden="true" />
            </Link>
          </div>
        </div>
      </div>

      <div className="flex items-center shrink-0 ml-auto">
        <button
          onClick={handleDismiss}
          disabled={feedbackMutation.isPending}
          className="p-0.5 rounded transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-40 disabled:cursor-not-allowed"
          aria-label={i18nT('components.tipCard.dismiss_tip')}
        >
          <X size={12} style={{ color: 'var(--muted)' }} />
        </button>
      </div>
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
