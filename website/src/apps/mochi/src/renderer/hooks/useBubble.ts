/**
 * useBubble — Manages bubble notification state with auto-dismiss timer
 * and fade-out animation.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { BUBBLE_FADE_MS } from '../../shared/constants'

import { api } from '../../mochiApi'

const BUBBLE_AUTO_DISMISS_MS = 6000

export interface UseBubbleReturn {
  bubble: string | null
  bubbleFading: boolean
  dismissBubble: () => void
  /**
   * Raise a bubble locally, with the same fade / auto-dismiss / sticky rules as
   * one arriving over the event bus.
   *
   * Exposed so a caller can surface something the BACKEND did not publish (the
   * approval prompt — see `useApprovalBubble`) without this hook having to learn
   * that domain. Bubble transport lives here; what is worth a bubble does not.
   */
  showBubble: (text: string, sticky: boolean) => void
}

export function useBubble(): UseBubbleReturn {
  const [bubble, setBubble] = useState<string | null>(null)
  const [bubbleFading, setBubbleFading] = useState(false)
  const fadingRef = useRef(false)
  const bubbleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Stable callback — no dependency on bubbleFading state, uses ref instead.
  // This avoids stale-closure issues when drainNextBubble sends a new bubble
  // right after a dismiss (the old useCallback captured stale bubbleFading=true).
  const dismissBubble = useCallback(() => {
    if (fadingRef.current) return
    fadingRef.current = true
    setBubbleFading(true)
    if (bubbleTimer.current) { clearTimeout(bubbleTimer.current); bubbleTimer.current = null }
    setTimeout(() => { setBubble(null); setBubbleFading(false); fadingRef.current = false }, BUBBLE_FADE_MS)
    api?.dismissBubble?.()
  }, [])

  // ONE raise path, shared by the event bus and by local callers, so every
  // bubble gets identical timer/fade behaviour.
  const showBubble = useCallback((text: string, sticky: boolean) => {
    fadingRef.current = false
    setBubbleFading(false)
    setBubble(text)
    if (bubbleTimer.current) { clearTimeout(bubbleTimer.current); bubbleTimer.current = null }
    if (!sticky) {
      bubbleTimer.current = setTimeout(() => dismissBubble(), BUBBLE_AUTO_DISMISS_MS)
    }
  }, [dismissBubble])

  useEffect(() => {
    const off = api?.onBubble?.((text: string, sticky: boolean) => showBubble(text, sticky))
    return () => { off?.() }
  }, [showBubble])

  return { bubble, bubbleFading, dismissBubble, showBubble }
}
