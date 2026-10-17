/**
 * useMood — Manages pet mood state with transient/persistent mood switching
 * and auto-reset timer for transient moods.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { PetMood } from '../../shared/types'

const MOOD_DURATION_MS = 3000
const TRANSIENT_MOODS: Set<PetMood> = new Set(['happy', 'scared', 'curious'])

import { api } from '../../mochiApi'

export interface UseMoodReturn {
  mood: PetMood
  moodRef: React.MutableRefObject<PetMood>
  clearPersistentMood: () => void
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

  useEffect(() => {
    const off = api?.onMood?.((m: string, _intensity: number) => { setMood(m as PetMood) })
    return () => { off?.(); if (moodTimerRef.current) clearTimeout(moodTimerRef.current) }
  }, [setMood])

  return { mood, moodRef, clearPersistentMood }
}
