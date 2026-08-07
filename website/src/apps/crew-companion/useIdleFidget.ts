/**
 * useIdleFidget — calm, non-intrusive "aliveness".
 *
 * The ghost stays put at its home spot. On a gentle timer it does a SMALL nearby
 * fidget — a little hop a few dozen pixels away and right back home — or a brief
 * mood flicker. It never roams across the screen and always returns to where the
 * user left it, so it feels alive without being annoying (matches the calm
 * desktop-pet convention: subtle in-place motion, not wandering).
 *
 * Fully local — never touches Kiro Crew — so it keeps working when offline.
 * Caller gates it via `enabled` (off while dragging, walking, peeking, focusing,
 * or when the quick-menu is open). Much rarer overnight.
 *
 * Ported verbatim from the desktop app's src/renderer/hooks/useIdleFidget.ts —
 * every timing, distance, probability and the overnight rarity is unchanged. The
 * only adaptation is the two import paths (PetMood from ./types, PET_W/PET_H from
 * ./constants; ../../shared/* there).
 */
import { useEffect, useRef } from 'react'
import type { PetMood } from './types'
import { PET_W, PET_H } from './constants'

const AMBIENT_MOODS: PetMood[] = ['curious', 'happy']
/**
 * At night the expression is sleepiness rather than curiosity. This is the only
 * thing that sets `sleepy`, and it is what makes the sleeping body reachable —
 * otherwise that art is wired up and never shown.
 *
 * `sleepy` is a PERSISTENT mood (not in TRANSIENT_MOODS), so unlike the daytime
 * flickers it stays until the user interacts — the ghost dozes off and wakes when
 * touched, which is the behaviour we want overnight.
 */
const NIGHT_MOODS: PetMood[] = ['sleepy']
const HOP_MIN = 30   // px — smallest nudge
const HOP_MAX = 70   // px — largest nudge (still "nearby")

export function useIdleFidget(opts: {
  enabled: boolean
  getPos: () => { x: number; y: number }
  walkPath: (points: Array<{ x: number; y: number }>) => void
  setMood: (m: PetMood) => void
}) {
  const enabledRef = useRef(opts.enabled); enabledRef.current = opts.enabled
  const getPosRef = useRef(opts.getPos); getPosRef.current = opts.getPos
  const walkPathRef = useRef(opts.walkPath); walkPathRef.current = opts.walkPath
  const setMoodRef = useRef(opts.setMood); setMoodRef.current = opts.setMood

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>

    const schedule = () => {
      const hour = new Date().getHours()
      const night = hour >= 23 || hour < 7
      // Calm cadence: a small fidget every ~2.5–5 min in the day; rare at night.
      const base = night ? 600_000 : 150_000
      const jitter = Math.random() * (night ? 600_000 : 150_000)
      timer = setTimeout(tick, base + jitter)
    }

    const tick = () => {
      if (enabledRef.current) {
        const hour = new Date().getHours()
        const night = hour >= 23 || hour < 7
        if (Math.random() < 0.35) {
          // An ambient expression: a brief flicker by day, dozing off at night.
          const pool = night ? NIGHT_MOODS : AMBIENT_MOODS
          setMoodRef.current(pool[Math.floor(Math.random() * pool.length)])
        } else {
          // Small hop a few dozen px away, then straight back to home.
          const home = getPosRef.current()
          const angle = Math.random() * Math.PI * 2
          const dist = HOP_MIN + Math.random() * (HOP_MAX - HOP_MIN)
          const maxX = window.innerWidth - PET_W
          const maxY = window.innerHeight - PET_H - 40 // keep clear of the Dock
          const ox = Math.max(0, Math.min(maxX, Math.round(home.x + Math.cos(angle) * dist)))
          const oy = Math.max(0, Math.min(maxY, Math.round(home.y + Math.sin(angle) * dist)))
          walkPathRef.current([{ x: ox, y: oy }, { x: home.x, y: home.y }])
        }
      }
      schedule()
    }

    schedule()
    return () => clearTimeout(timer)
  }, [])
}
