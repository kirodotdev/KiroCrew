/**
 * useMood — pet mood state with transient/persistent switching and an auto-reset
 * timer for transient moods.
 *
 * Ported verbatim from the desktop app's src/renderer/hooks/useMood.ts — the
 * 3-second transient duration and the transient-mood set are unchanged. The only
 * adaptation is the import path for PetMood (./types here, ../../shared/types there).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { PetMood } from './types'

const MOOD_DURATION_MS = 3000
const TRANSIENT_MOODS: Set<PetMood> = new Set(['happy', 'scared', 'curious'])

export interface UseMoodReturn {
  mood: PetMood
  moodRef: React.MutableRefObject<PetMood>
  clearPersistentMood: () => void
  /** Set a mood directly (used by the idle wander for small ambient changes). */
  setMood: (m: PetMood) => void
}

export function useMood(): UseMoodReturn {
  const [mood, setMoodState] = useState<PetMood>('neutral')
  const moodRef = useRef<PetMood>('neutral')
  const moodTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const setMood = useCallback((m: PetMood) => {
    moodRef.current = m
    setMoodState(m)
    if (moodTimerRef.current) { clearTimeout(moodTimerRef.current); moodTimerRef.current = null }
    if (m !== 'neutral' && TRANSIENT_MOODS.has(m)) {
      moodTimerRef.current = setTimeout(() => { moodRef.current = 'neutral'; setMoodState('neutral') }, MOOD_DURATION_MS)
    }
  }, [])

  const clearPersistentMood = useCallback(() => {
    setMoodState(prev => {
      if (prev !== 'neutral' && !TRANSIENT_MOODS.has(prev)) {
        moodRef.current = 'neutral'
        return 'neutral'
      }
      return prev
    })
  }, [])

  // Nothing external drives the mood any more — the agent path is gone. The random
  // idle clips still call setMood, so the timer cleanup stays.
  useEffect(() => () => {
    if (moodTimerRef.current) clearTimeout(moodTimerRef.current)
  }, [])

  return { mood, moodRef, clearPersistentMood, setMood }
}
