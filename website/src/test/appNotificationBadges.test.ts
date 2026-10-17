import { describe, it, expect } from 'vitest'
import { appNavTarget } from '../appNav'
import {
  APP_NAV_ID_PREFIX,
  APP_SOURCE_PREFIX,
  MAX_APP_BADGE_COUNT,
  appNotificationBadges,
  isAppNavId,
  mergeAppBadges,
  type BadgeNote,
} from '../appNotificationBadges'

/**
 * The rail badge for a third-party app is DERIVED from notifications the app
 * already pushed, so these tests pin the derivation rather than any manifest
 * field -- there is deliberately no new declared field to pin.
 *
 * Counts are asserted by WHOLE-MAP equality, never per key. With one app every
 * candidate key expression coincides, so a loop-variable slip that stamped the
 * first note's app onto later ones would be invisible; three apps with THREE
 * DIFFERENT counts make the pairing itself the assertion.
 */
describe('appNotificationBadges', () => {
  it('counts unacknowledged notes per producing app, pairing each count to its own app', () => {
    const notes: BadgeNote[] = [
      { source: 'app:ledger', acked: false },
      { source: 'app:ledger', acked: false },
      { source: 'app:ledger', acked: false },
      { source: 'app:watchtower', acked: false },
      { source: 'app:watchtower', acked: false },
      { source: 'app:quiet-app', acked: false },
    ]

    // Whole-map equality: catches a miscount AND a cross-attributed count.
    expect(appNotificationBadges(notes)).toEqual({
      ledger: 3,
      watchtower: 2,
      'quiet-app': 1,
    })
  })

  it('ignores acknowledged notes, and omits an app whose notes are all acknowledged', () => {
    const notes: BadgeNote[] = [
      { source: 'app:ledger', acked: true },
      { source: 'app:ledger', acked: false },
      { source: 'app:done', acked: true },
      { source: 'app:done', acked: true },
    ]

    const counts = appNotificationBadges(notes)

    expect(counts).toEqual({ ledger: 1 })
    // Complement: an app with nothing waiting must be ABSENT, not zero. A zero
    // entry would survive a merge and clear a count another source had set.
    expect('done' in counts).toBe(false)
  })

  it('ignores notes that no app produced', () => {
    const notes: BadgeNote[] = [
      { source: 'system', acked: false },
      { source: 'cron', acked: false },
      { acked: false },
      { source: '', acked: false },
      // A bare prefix names no app: it must not become an empty-string key.
      { source: APP_SOURCE_PREFIX, acked: false },
    ]

    const counts = appNotificationBadges(notes)

    expect(counts).toEqual({})
    expect('' in counts).toBe(false)
  })

  it('treats a note with no acked field as unacknowledged', () => {
    // Records written before the flag existed rehydrate without it; absent must
    // read as "not yet acknowledged" rather than being silently dropped.
    expect(appNotificationBadges([{ source: 'app:ledger' }])).toEqual({ ledger: 1 })
  })

  it('clamps a count at MAX_APP_BADGE_COUNT', () => {
    const notes: BadgeNote[] = Array.from(
      { length: MAX_APP_BADGE_COUNT + 40 },
      () => ({ source: 'app:noisy', acked: false }),
    )

    // Exact equality, not a bound: the point is that the clamp fires at the cap
    // rather than merely that the number is smaller than the input.
    expect(appNotificationBadges(notes)).toEqual({ noisy: MAX_APP_BADGE_COUNT })
  })

  it('returns an empty map for no notes', () => {
    expect(appNotificationBadges([])).toEqual({})
  })
})

describe('mergeAppBadges', () => {
  it('fills only rows that had no pushed badge', () => {
    expect(mergeAppBadges({ pushy: 7 }, { derived: 2 })).toEqual({ pushy: 7, derived: 2 })
  })

  it('lets an explicitly pushed count win over a derived one', () => {
    // An app using useNavBadge() today must see NO change from this derivation.
    expect(mergeAppBadges({ ledger: 7 }, { ledger: 3 })).toEqual({ ledger: 7 })
  })

  it('honours an explicit pushed zero, so an app can still clear its own badge', () => {
    // The direction that matters: 0 is a statement ("no badge"), not an absence.
    // A spread that dropped falsy values would let the derived 3 resurrect a
    // badge the app deliberately cleared.
    expect(mergeAppBadges({ ledger: 0 }, { ledger: 3 })).toEqual({ ledger: 0 })
  })

  it('does not mutate either input', () => {
    const pushed = { ledger: 7 }
    const derived = { ledger: 3, other: 1 }

    mergeAppBadges(pushed, derived)

    expect(pushed).toEqual({ ledger: 7 })
    expect(derived).toEqual({ ledger: 3, other: 1 })
  })
})

/**
 * Regressions for the two blocking review findings on the first revision.
 *
 * An app name is attacker-chosen and the manifest reserves only a namespace
 * list, so `constructor`, `toString` and `__proto__` are all valid app names,
 * and an app may also pick a name that equals a HOST nav row's id.
 */
describe('appNotificationBadges: an app name cannot reach Object.prototype', () => {
  // Three inherited names, not one: at N=1 a fix that special-cased a single
  // name would pass while the class stayed open.
  const inherited = ['constructor', 'toString', 'hasOwnProperty']

  for (const name of inherited) {
    it(`counts an app named ${name} as a number`, () => {
      const counts = appNotificationBadges([{ source: `app:${name}`, acked: false }])

      // The defect produced a STRING (Object's function body concatenated with
      // 1), so the type assertion is the load-bearing one, not just the value.
      expect(typeof counts[name]).toBe('number')
      expect(counts).toEqual({ [name]: 1 })
    })
  }

  it('does not resolve an inherited name for an app with no notes', () => {
    const counts = appNotificationBadges([{ source: 'app:ledger', acked: false }])

    // On a plain object this read returns Object's constructor -- truthy, so
    // neither NavBadge's `|| 0` nor BadgeIndicator's `count <= 0` filters it.
    expect(counts['constructor']).toBeUndefined()
    expect(counts['__proto__']).toBeUndefined()
  })
})

describe('mergeAppBadges: the merged map is also prototype-less', () => {
  it('does not resolve an inherited name for an app absent from both inputs', () => {
    // The read site, one level out from the counter: a merge that spread into an
    // object literal would hand NavBadge a function for this lookup even though
    // the accumulator itself was safe.
    const merged = mergeAppBadges({ pushy: 1 }, { ledger: 2 })

    expect(merged['constructor']).toBeUndefined()
    expect(merged['toString']).toBeUndefined()
  })

  it('still carries real keys and precedence after the prototype change', () => {
    expect(mergeAppBadges({ ledger: 7 }, { ledger: 3, other: 1 })).toEqual({ ledger: 7, other: 1 })
  })
})

describe('isAppNavId', () => {
  it('accepts an installed app row', () => {
    expect(isAppNavId(`${APP_NAV_ID_PREFIX}my-ledger`)).toBe(true)
  })

  it('rejects a host surface row whose id could equal an app name', () => {
    // The finding: `schedule` is a real host nav id AND a valid app name, and
    // NavBadge keys its fallback on the bare id for an unprefixed row. If this
    // returns true, an app named `schedule` badges the host Schedule row.
    expect(isAppNavId('schedule')).toBe(false)
    expect(isAppNavId('projects')).toBe(false)
    expect(isAppNavId('chat')).toBe(false)
  })

  it('rejects a name that merely contains the prefix later on', () => {
    expect(isAppNavId('my-app-thing')).toBe(false)
  })
})

describe('appNotificationBadges: passive notes are not attention', () => {
  it('excludes a passive note, matching the host unread definition', () => {
    // notification_coordinator.py increments _unread_count only when
    // priority != "passive", and the feed dims a passive note as non-attention.
    // Counting one here would contradict the subsystem this derives from.
    const notes: BadgeNote[] = [
      { source: 'app:ledger', acked: false, priority: 'passive' },
      { source: 'app:ledger', acked: false, priority: 'default' },
      { source: 'app:ledger', acked: false, priority: 'critical' },
    ]

    // Exactly 2, not 3: the two attention priorities count, passive does not.
    expect(appNotificationBadges(notes)).toEqual({ ledger: 2 })
  })

  it('still counts a note with no priority, since the default is not passive', () => {
    expect(appNotificationBadges([{ source: 'app:ledger', acked: false }])).toEqual({ ledger: 1 })
  })

  it('omits an app whose only notes are passive', () => {
    const counts = appNotificationBadges([
      { source: 'app:quiet', acked: false, priority: 'passive' },
    ])

    expect(counts).toEqual({})
    expect('quiet' in counts).toBe(false)
  })
})

describe('appNotificationBadges: muted channels are not attention', () => {
  it('excludes a silenced note, completing the host three-clause definition', () => {
    // The host's own predicate is `!acked && priority !== 'passive' && !silenced`
    // (App.tsx, "mirroring the backend's _unread_count semantics"; the same
    // expression again in useWebSocket.ts, and an existing test pins it).
    // Omitting any one clause invents a second definition of unread.
    const notes: BadgeNote[] = [
      { source: 'app:ledger', acked: false, silenced: true },
      { source: 'app:ledger', acked: false, silenced: false },
      { source: 'app:ledger', acked: false },
    ]

    expect(appNotificationBadges(notes)).toEqual({ ledger: 2 })
  })

  it('lets muting a channel switch the badge off entirely', () => {
    // The property that matters to a user: muting is the only opt-out available
    // while the app's page is unmounted, which is the regime this badge covers.
    const counts = appNotificationBadges([
      { source: 'app:noisy', acked: false, silenced: true },
      { source: 'app:noisy', acked: false, silenced: true },
    ])

    expect(counts).toEqual({})
    expect('noisy' in counts).toBe(false)
  })

  it('excludes on ANY one clause, so no clause can be quietly dropped', () => {
    // Three notes, each failing a different clause: all three must be excluded,
    // so removing any single clause reddens this.
    const notes: BadgeNote[] = [
      { source: 'app:x', acked: true },
      { source: 'app:x', acked: false, priority: 'passive' },
      { source: 'app:x', acked: false, silenced: true },
    ]

    expect(appNotificationBadges(notes)).toEqual({})
  })
})

describe('isAppNavId agrees with the id appNav actually builds', () => {
  // The prefix is spelled in appNav.ts (which BUILDS the id) and in NavBadge
  // (which slices it). Rather than refactor either, pin the RELATIONSHIP: if
  // the id shape drifts from this predicate, these reds say so.
  it('accepts the id appNavTarget builds for an installed app', () => {
    const target = appNavTarget({
      name: 'ledger',
      enabled: true,
      origin: 'installed',
      manifest: { ui: { pages: [{ route: '/apps/ledger' }] } },
    })

    expect(target).not.toBeNull()
    expect(isAppNavId(target!.id)).toBe(true)
  })

  it('rejects the id appNavTarget builds for a natively routed builtin', () => {
    // A native builtin keeps its bare name as its id, which is exactly the
    // class that must NOT receive a derived count.
    const target = appNavTarget({
      name: 'projects',
      enabled: true,
      origin: 'builtin',
      manifest: { ui: { pages: [{ route: '/projects' }] } },
    })

    expect(target).not.toBeNull()
    expect(isAppNavId(target!.id)).toBe(false)
  })
})
