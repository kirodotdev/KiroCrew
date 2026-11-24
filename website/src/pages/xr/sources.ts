/**
 * Names for the things that can press in XR, by what is behind them.
 *
 * A hand is a person's hand. A puck, a controller or a head-aimed button is a
 * device: it can aim and click plain targets, and it can never complete a
 * guarded press (Approve, Reject, Send). `PressGuard` reads that from the name,
 * so the name must come from what the browser reports, never from an index.
 */
export interface XrSourceLike {
  handedness?: string
  targetRayMode?: string
  hand?: unknown
  profiles?: readonly string[]
}

export function sourceIdFor(s: XrSourceLike | null | undefined, index: number): string {
  const side = s?.handedness === 'left' || s?.handedness === 'right' ? s.handedness : String(index)
  // A source the browser has not described yet gets the least trust.
  if (!s) return `puck-${side}`
  if (s.hand || s.profiles?.some(p => p.includes('hand'))) return `hand-${side}`
  // A pinch the system aims for the person (eyes plus fingers) is still a hand acting.
  if (s.targetRayMode === 'transient-pointer') return `pinch-${side}`
  return `puck-${side}`
}

export const isPuck = (source: string): boolean => source.startsWith('puck')
