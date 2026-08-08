/**
 * useRandomClips — spontaneous "life" for CUSTOM appearance packs, ported from the
 * desktop app.
 *
 * A custom pack has a static idle (no built-in bob/fidget). Instead, on the same
 * gentle timer the default ghost uses, the companion occasionally plays ONE of the
 * random behaviours the pack actually provides: a wander (if it ships walking art), a
 * mood, or an author-named "extra" clip. A pack that provides none stays perfectly
 * still — random motion is strictly opt-in content, never forced.
 *
 * Ported unchanged from src/renderer/hooks/useRandomClips.ts except that the wander
 * moves the DOM element via `walkPath` (see useWalking) rather than a window. The
 * calm cadence (every ~2.5–5 min by day, rare at night) and the 30–70px hop clamp are
 * inline and identical to the source and to useIdleFidget. Gated by `enabled`, which
 * the caller turns off while the companion is being dragged, docked, or otherwise
 * busy — and while the OS asks for reduced motion.
 */
import { useEffect, useRef } from 'react'
import type { PetMood } from './types'
import { PET_W, PET_H } from './constants'

export interface RandomBehaviors {
  walking: boolean
  moods: string[]
  extras: string[]
}

export interface UseRandomClipsOptions {
  enabled: boolean
  getBehaviors: () => RandomBehaviors
  getPos: () => { x: number; y: number }
  walkPath: (points: Array<{ x: number; y: number }>) => void
  setMood: (m: PetMood) => void
  playExtra: (name: string) => void
}

export function useRandomClips(opts: UseRandomClipsOptions): void {
  const enabledRef = useRef(opts.enabled); enabledRef.current = opts.enabled
  const getBehaviorsRef = useRef(opts.getBehaviors); getBehaviorsRef.current = opts.getBehaviors
  const getPosRef = useRef(opts.getPos); getPosRef.current = opts.getPos
  const walkPathRef = useRef(opts.walkPath); walkPathRef.current = opts.walkPath
  const setMoodRef = useRef(opts.setMood); setMoodRef.current = opts.setMood
  const playExtraRef = useRef(opts.playExtra); playExtraRef.current = opts.playExtra

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>

    const schedule = () => {
      const hour = new Date().getHours()
      const night = hour >= 23 || hour < 7
      const base = night ? 600_000 : 150_000
      const jitter = Math.random() * (night ? 600_000 : 150_000)
      timer = setTimeout(tick, base + jitter)
    }

    const tick = () => {
      if (enabledRef.current) {
        const b = getBehaviorsRef.current()
        const pool: Array<() => void> = []

        if (b.walking) {
          pool.push(() => {
            const home = getPosRef.current()
            const angle = Math.random() * Math.PI * 2
            const dist = 30 + Math.random() * 40
            const maxX = window.innerWidth - PET_W
            const maxY = window.innerHeight - PET_H - 40
            const ox = Math.max(0, Math.min(maxX, Math.round(home.x + Math.cos(angle) * dist)))
            const oy = Math.max(0, Math.min(maxY, Math.round(home.y + Math.sin(angle) * dist)))
            walkPathRef.current([{ x: ox, y: oy }, { x: home.x, y: home.y }])
          })
        }
        for (const m of b.moods) pool.push(() => setMoodRef.current(m as PetMood))
        for (const name of b.extras) pool.push(() => playExtraRef.current(name))

        if (pool.length > 0) pool[Math.floor(Math.random() * pool.length)]()
      }
      schedule()
    }

    schedule()
    return () => clearTimeout(timer)
  }, [])
}
