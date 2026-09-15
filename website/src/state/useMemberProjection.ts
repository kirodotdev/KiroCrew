/**
 * React hooks over the per-member projection store.
 *
 * Built on useSyncExternalStore via faceOf(), which returns a referentially
 * stable snapshot until the underlying row changes. A null/undefined slug
 * returns undefined and subscribes to nothing.
 */
import { useMemo, useRef, useSyncExternalStore } from 'react'

import { memberProjectionStore, type ProjectionFace } from './memberProjectionStore'
import type {
  ActivityView,
  ContributedView,
  DrivingView,
  RosterView,
  WakeView,
} from './memberProjectionTypes'

/** A face that is always empty and holds no listeners, for a null slug. */
const EMPTY_FACE: ProjectionFace = {
  subscribe: () => () => {},
  getSnapshot: () => undefined,
}

/**
 * Read one projection value for a member, re-rendering only when that exact
 * (slug, key) row changes. Returns undefined for a null/undefined slug or a
 * key with no held value. T is the caller's expected shape; the store holds
 * `unknown`, so this is a cast, not a runtime guarantee.
 */
export function useMemberProjection<T = unknown>(
  slug: string | null | undefined,
  key: string,
): T | undefined {
  const face = useMemo<ProjectionFace>(
    () => (slug ? memberProjectionStore.faceOf(slug, key) : EMPTY_FACE),
    [slug, key],
  )
  const value = useSyncExternalStore(face.subscribe, face.getSnapshot, face.getSnapshot)
  return value as T | undefined
}

/** All four projections for a member, each typed loosely (may be undefined). */
export interface MemberProjections {
  roster: RosterView | undefined
  activity: ActivityView | undefined
  wake: WakeView | undefined
  driving: DrivingView | undefined
}

/** Convenience hook returning every projection for a member. */
export function useMemberProjections(slug: string | null | undefined): MemberProjections {
  return {
    roster: useMemberProjection<RosterView>(slug, 'roster'),
    activity: useMemberProjection<ActivityView>(slug, 'activity'),
    wake: useMemberProjection<WakeView>(slug, 'wake'),
    driving: useMemberProjection<DrivingView>(slug, 'driving'),
  }
}

/** Nothing held, for a null slug — a module constant so the memo below is stable. */
const NO_CONTRIBUTED: ContributedView[] = []

/**
 * Every contributed `<app>/<key>` view held for a member, sorted by key.
 *
 * Subscribes to the slug's KEY SET rather than to each key, so a card appearing
 * for the first time re-renders the list. Value changes to a card already in the
 * list arrive through the same subscription: the store bumps the keyset version
 * only on a set change, but the list is rebuilt from the store on every bump AND
 * on any re-render of the consumer, and each card reads its own value through
 * `useMemberProjection` so a pushed value moves that card alone.
 */
export function useMemberContributedViews(
  slug: string | null | undefined,
): readonly ContributedView[] {
  const face = useMemo<ProjectionFace>(
    () => (slug ? memberProjectionStore.contributedFace(slug) : EMPTY_FACE),
    [slug],
  )
  const version = useSyncExternalStore(face.subscribe, face.getSnapshot, face.getSnapshot)
  return useMemo(
    () => (slug ? memberProjectionStore.contributedViews(slug) : NO_CONTRIBUTED),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slug, version],
  )
}

/**
 * Read the `roster` projection for MANY members at once, for a page-level
 * merged list (starred count, filters, sort). Subscribes to every slug's
 * `roster` face and returns a referentially STABLE Map that only changes
 * identity when some subscribed view actually changed OR the slug list
 * changed — so a consumer memo keyed on the Map recomputes exactly then, not
 * on every render.
 *
 * getSnapshot must be O(1) referentially stable (useSyncExternalStore compares
 * the returned snapshot by identity), so it returns a version NUMBER, not the
 * Map. The Map is rebuilt in a memo keyed on that version and the slug key —
 * the version is bumped by the subscription only when a face fires.
 */
export function useMemberRosterViews(
  slugs: readonly string[],
): ReadonlyMap<string, RosterView | undefined> {
  // A stable key for the slug set: identity of `slugs` is not stable across
  // renders (a fresh array each parent render), so subscribe/getSnapshot key
  // on the JOINED string, and the Map memo depends on it too.
  const slugsKey = slugs.join('\u0000')
  const slugList = useMemo(() => slugsKey.split('\u0000').filter((s) => s !== ''), [slugsKey])

  const versionRef = useRef(0)
  // The faces are rebuilt only when the slug set changes; each render's
  // subscribe closes over the current face list.
  const faces = useMemo<ProjectionFace[]>(
    () => slugList.map((slug) => memberProjectionStore.faceOf(slug, 'roster')),
    [slugList],
  )

  const subscribe = useMemo(
    () =>
      (onStoreChange: () => void): (() => void) => {
        const bump = () => {
          versionRef.current += 1
          onStoreChange()
        }
        const unsubs = faces.map((f) => f.subscribe(bump))
        return () => {
          for (const u of unsubs) u()
        }
      },
    [faces],
  )

  // Snapshot is the version counter: stable between changes, so
  // useSyncExternalStore does not loop. A slug-set change remounts the
  // subscription (new `subscribe` identity) and the Map memo below rebuilds
  // on `slugsKey` regardless.
  const version = useSyncExternalStore(subscribe, () => versionRef.current, () => versionRef.current)

  return useMemo<ReadonlyMap<string, RosterView | undefined>>(() => {
    const map = new Map<string, RosterView | undefined>()
    for (const slug of slugList) {
      map.set(slug, memberProjectionStore.get(slug, 'roster') as RosterView | undefined)
    }
    return map
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, slugsKey])
}
