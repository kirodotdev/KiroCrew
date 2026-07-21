import { useCallback, useEffect, useRef, useState } from 'react'
import { Lightbulb, X } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { api } from '../api/client'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

export interface Tip {
  id: string
  feature: string
  title: string
  body: string
  why: string
  doc: string
  cta_prompt: string
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

  const tooltipText = tip.why ? `${tip.title} — ${tip.why}` : tip.title

  return (
    <motion.div
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
      aria-label="Feature tip"
      title={tooltipText}
    >
      <Lightbulb size={14} className="shrink-0 mt-0.5" aria-hidden="true" style={{ color: 'var(--accent)' }} />

      <span className="min-w-0 flex-1">
        <span className="block font-medium text-[12px] leading-tight" style={{ color: 'var(--text)' }}>{tip.title}</span>
        {/* Full multi-line body — no truncation (maintainer feedback: "it can
            be multiple lines, don't cut off"). A viewport-relative max-height
            with scroll (Codex round-23) keeps a very long body from pushing
            the bottom-anchored card past the viewport on narrow screens —
            every character stays reachable, nothing is clipped away. */}
        <span className="block text-[12px] leading-snug mt-0.5 break-words overflow-y-auto max-h-[30vh]" style={{ color: 'var(--muted)' }}>{tip.body}</span>
      </span>

      <div className="flex items-center shrink-0 ml-auto">
        <button
          onClick={handleDismiss}
          disabled={feedbackMutation.isPending}
          className="p-0.5 rounded transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-40 disabled:cursor-not-allowed"
          aria-label="Dismiss tip"
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

  // Reset ALL per-turn state when the active slot changes (Codex round-8):
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

  // Temporary sessions forbid memory reads (Codex round-22): tips are
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
  // it so valid low-cadence settings actually take effect (Codex round-10).
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
  // otherwise flash before the reset effect fires (Codex round-24).
  return { tip: visible && !suppressed && !blocked ? tip ?? null : null, dismiss }
}
