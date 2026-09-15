/**
 * Custom appearance packs replace Kiro's idle-motion pool only when they provide
 * random moments of their own. The replacement pool is flat and uniform: Idle plus
 * every uploaded moment, so one uploaded clip produces [Idle, clip].
 */
import { useEffect, useRef } from 'react'
import { PET_W, PET_H } from './constants'

export type CustomRandomEntry = string | null

/** `null` is the explicit Idle entry; duplicate names must not gain extra weight. */
export function customRandomPool(clips: readonly string[]): CustomRandomEntry[] {
  return [null, ...new Set(clips)]
}

export function normaliseCustomRandomNames(
  animations: Record<string, unknown>,
  randomNames: unknown,
): string[] {
  const listed = Array.isArray(randomNames)
    ? randomNames.filter((name): name is string => typeof name === 'string' && name in animations)
    : []
  return [...new Set([...(animations.walking ? ['walking'] : []), ...listed])]
}

export interface UseRandomClipsOptions {
  enabled: boolean
  getClips: () => string[]
  getPos: () => { x: number; y: number }
  walkPath: (points: Array<{ x: number; y: number }>) => void
  showIdle: () => void
  playClip: (name: string) => void
}

export function useRandomClips(opts: UseRandomClipsOptions): void {
  const enabledRef = useRef(opts.enabled); enabledRef.current = opts.enabled
  const getClipsRef = useRef(opts.getClips); getClipsRef.current = opts.getClips
  const getPosRef = useRef(opts.getPos); getPosRef.current = opts.getPos
  const walkPathRef = useRef(opts.walkPath); walkPathRef.current = opts.walkPath
  const showIdleRef = useRef(opts.showIdle); showIdleRef.current = opts.showIdle
  const playClipRef = useRef(opts.playClip); playClipRef.current = opts.playClip

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
        const pool = customRandomPool(getClipsRef.current())
        const selected = pool[Math.floor(Math.random() * pool.length)]
        if (selected === null) {
          showIdleRef.current()
        } else if (selected === 'walking') {
          const home = getPosRef.current()
          const angle = Math.random() * Math.PI * 2
          const dist = 30 + Math.random() * 40
          const maxX = window.innerWidth - PET_W
          const maxY = window.innerHeight - PET_H - 40
          const ox = Math.max(0, Math.min(maxX, Math.round(home.x + Math.cos(angle) * dist)))
          const oy = Math.max(0, Math.min(maxY, Math.round(home.y + Math.sin(angle) * dist)))
          walkPathRef.current([{ x: ox, y: oy }, { x: home.x, y: home.y }])
        } else {
          playClipRef.current(selected)
        }
      }
      schedule()
    }

    schedule()
    return () => clearTimeout(timer)
  }, [])
}
