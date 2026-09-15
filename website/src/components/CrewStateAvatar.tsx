/**
 * A crew avatar that reacts on its own.
 *
 * `CrewAvatar` is deliberately stateless — it renders the state it is told.
 * This wrapper is the pairing of it with `useCrewAvatarState`, and it exists as
 * a component rather than as two lines at each call site because the roster
 * renders its avatars inside a `map`, where a hook cannot go. One component per
 * row is the only shape that gives each row its own observation.
 */
import { useMemo } from 'react'

import CrewAvatar from './CrewAvatar'
import { soundsFrom } from '../lib/crewAvatarState'
import { useCrewAvatarState } from '../hooks/useCrewAvatarState'
import type { WorkingIntensity } from '../lib/kiroGhostAvatar'

export interface CrewStateAvatarProps {
  /** Crew name — the seed of the default face, and the fallback way to find
   *  the crew's slot when the caller has no key. */
  seed: string
  /** The crew record's `avatar` field, verbatim (see `CrewAvatar`). */
  avatar?: unknown
  /** The crew's session slot, when the caller already resolved it. */
  slotKey?: string | null
  /** Authoritative running flag, when the caller has a better one than the
   *  live slot alone (a roster snapshot before the first slots frame). */
  running?: boolean
  size?: number
  /** Intensity of the working animation — `subtle` in a dense list, `full` on
   *  a single-avatar surface. */
  working?: WorkingIntensity
  onImageError?: () => void
  className?: string
}

export default function CrewStateAvatar({
  seed,
  avatar,
  slotKey,
  running,
  size,
  working = 'subtle',
  onImageError,
  className,
}: CrewStateAvatarProps) {
  // A picture keeps its sounds: it has no face to change, but "this crew just
  // finished" is worth hearing whatever it is wearing.
  const sounds = useMemo(() => soundsFrom(avatar), [avatar])
  const state = useCrewAvatarState({ slotKey, agentName: seed, running, sounds })
  return (
    <CrewAvatar
      seed={seed}
      avatar={avatar}
      size={size}
      state={state}
      working={working}
      onImageError={onImageError}
      className={className}
    />
  )
}
