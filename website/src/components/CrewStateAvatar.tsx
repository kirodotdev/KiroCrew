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

import CrewAvatar, { packAvatarFrom } from './CrewAvatar'
import { soundsFrom } from '../lib/crewAvatarState'
import { BUILTIN_PACK_ID } from '../lib/appearancePacks/library'
import { useCrewAvatarState } from '../hooks/useCrewAvatarState'
import { usePackDetail } from '../hooks/usePackDetail'
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
  // Ghost-only, and `soundsFrom` is what enforces that: a picture is silent by
  // design, and a pack carries its own audio files rather than a preset name.
  const sounds = useMemo(() => soundsFrom(avatar), [avatar])
  // The built-in pack IS the seeded ghost and ships inside this bundle, so it has
  // no served cue — the sound route answers `builtin_no_content` for it, the same
  // way the slot route does.
  const pack = useMemo(() => packAvatarFrom(avatar), [avatar])
  const packId = pack && pack.id !== BUILTIN_PACK_ID ? pack.id : null
  // Read HERE rather than inside the renderer, because the cue fires on the
  // transition and this is the component that observes it. Same query key the
  // renderer already uses, so a pack-wearing roster row shares one request; and
  // disabled outright for a crew that wears no pack, so the ghost tier fetches
  // nothing (`usePackDetail`).
  const { data: detail, isError: packUnreadable } = usePackDetail(packId)
  const packCue = useMemo(
    () => (packId && detail?.sounds ? { id: packId, states: detail.sounds } : null),
    [packId, detail],
  )
  // "Wears a pack, does not yet know what it sounds like" — read off `detail`
  // rather than the query's own status, because a DISABLED query also reports
  // pending and a ghost crew would then hold its edge forever.
  //
  // A FAILED read ends the hold: "in flight" and "cannot be known" are different
  // answers, and only the first is worth waiting for. React Query retries once
  // and re-reads on the next mount, focus or reconnect, so the hold would
  // otherwise outlive every attempt and suppress this crew's cues for the rest of
  // the session — silently, because a suppressed cue looks like a pack that
  // declares none. The failure itself is already on screen: `PackAvatar` reads
  // the SAME query, and its `onError` reaches this component's `onImageError`,
  // which the crew editor renders through `ErrorNotice` (`crew-sheet-error`).
  const packPending = !!packId && !detail && !packUnreadable
  const state = useCrewAvatarState({
    slotKey,
    agentName: seed,
    running,
    sounds,
    packCue,
    packPending,
  })
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
