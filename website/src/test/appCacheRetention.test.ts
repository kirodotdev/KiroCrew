/**
 * The two rules that decide where an app's cached data lands and how long it
 * stays — verified without mounting a tree, the way `overlaySlots.test.ts`
 * verifies slot ownership.
 *
 * Each case here is paired with the mutation it kills; a guard whose deletion
 * leaves the suite green is not a guard.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  APP_CACHE_RETENTION_MS,
  applyCacheRetention,
  resolveAppQueryKey,
  resolveCacheRetention,
  type AppQueryKey,
  type CacheRetentionPlan,
} from '../apps/appCacheRetention'

/** Records what was registered, so "exactly one registration" is observable. */
function recordingSink() {
  const calls: { queryKey: AppQueryKey; options: { gcTime: number } }[] = []
  return {
    calls,
    setQueryDefaults(queryKey: AppQueryKey, options: { gcTime: number }) {
      calls.push({ queryKey, options })
    },
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('resolveCacheRetention', () => {
  it('registers nothing when there is no host-owned namespace', () => {
    // `null` is what `useTrustedAppId()` returns on a host page and in an
    // external app. Registering a default here would attach retention to keys
    // that belong to no app.
    //
    // MUTATION: delete the `if (!trustedAppId) return null` branch — an appId of
    // `''` produces `{ keyPrefix: [''] }`, which react-query matches against
    // EVERY key, quietly turning a per-app feature into the global gcTime raise
    // the design explicitly rules out.
    expect(resolveCacheRetention(null)).toBeNull()
  })

  it('registers the app namespace exactly, as one prefix segment', () => {
    // The prefix is `['aws-control']`, not `['app', 'aws-control']`: react-query
    // matches defaults by prefix, so this one registration covers every key the
    // app already writes by hand — which is what lets an app be fixed with no
    // change to the app.
    expect(resolveCacheRetention('aws-control')).toEqual({
      keyPrefix: ['aws-control'],
      gcTime: APP_CACHE_RETENTION_MS,
    })
  })
})

describe('APP_CACHE_RETENTION_MS', () => {
  it('is 30 minutes', () => {
    // MUTATION: change the constant — react-query's own default is 5 minutes,
    // which is shorter than the absence this feature exists to survive.
    //
    // There is deliberately nothing here reconciling this against a second
    // literal. Issue Radar's own `CACHE_RETENTION_MS` was the same 30 minutes and
    // is deleted by this change, so this is the only definition; a drift test
    // between two constants would only have been evidence that both still
    // existed.
    expect(APP_CACHE_RETENTION_MS).toBe(30 * 60_000)
  })

  it('is longer than the react-query default it exists to beat', () => {
    // The property that matters is not the exact number but that an ordinary
    // detour fits inside it. 5 minutes is what every app gets without this.
    expect(APP_CACHE_RETENTION_MS).toBeGreaterThan(5 * 60_000)
  })
})

describe('resolveAppQueryKey', () => {
  it('prefixes with the appId and nothing else', () => {
    // THE hard constraint of this change: the resulting key must be
    // byte-identical to what AWS Control writes by hand today, or its existing
    // `invalidateQueries` calls stop matching its own queries. Written as the
    // literal from `DrivePage.tsx` rather than built from the appId, so a change
    // to the prefix SHAPE fails here.
    expect(resolveAppQueryKey('aws-control', ['drive', 'default'])).toEqual([
      'aws-control',
      'drive',
      'default',
    ])
    expect(resolveAppQueryKey('aws-control', ['accounts'])).toEqual(['aws-control', 'accounts'])
  })

  it('uses the key unchanged when there is no host-owned namespace', () => {
    // MUTATION: invent a fallback prefix here — an un-namespaced key still
    // fetches and still shares with any other reader of the same key, where a
    // made-up prefix puts the data where no other reader looks.
    expect(resolveAppQueryKey(null, ['drive', 'default'])).toEqual(['drive', 'default'])
  })

  it('names the app itself when the key is empty', () => {
    expect(resolveAppQueryKey('aws-control', [])).toEqual(['aws-control'])
  })

  it('does not prefix twice, and says so', () => {
    // The one conversion slip review does not catch: `['x', 'x', 'drive']` is a
    // valid key that fetches correctly and is never invalidated by the app's own
    // `['x', 'drive']` mutations, so the symptom (stale data after a write)
    // surfaces far from the line that caused it.
    //
    // MUTATION: delete the already-prefixed branch — the key gains a second
    // segment and this fails on length as well as on the warning.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const key = resolveAppQueryKey('double-app', ['double-app', 'drive'])
    expect(key).toEqual(['double-app', 'drive'])
    expect(key.filter((segment) => segment === 'double-app')).toHaveLength(1)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('double-app')
  })

  it('warns once per app, not once per render', () => {
    // A refused or corrected capability has to be visible, but a component
    // re-rendering on every keystroke must not bury the rest of the console.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    resolveAppQueryKey('chatty-app', ['chatty-app', 'a'])
    resolveAppQueryKey('chatty-app', ['chatty-app', 'b'])
    resolveAppQueryKey('chatty-app', ['chatty-app', 'a'])
    expect(warn).toHaveBeenCalledTimes(1)
  })
})

describe('applyCacheRetention', () => {
  it('registers one default, against the plan it was given', () => {
    const sink = recordingSink()
    const plan: CacheRetentionPlan = { keyPrefix: ['aws-control'], gcTime: APP_CACHE_RETENTION_MS }
    applyCacheRetention(sink, plan)
    expect(sink.calls).toEqual([
      { queryKey: ['aws-control'], options: { gcTime: APP_CACHE_RETENTION_MS } },
    ])
  })

  it('has no dead null-branch: a missing plan fails loudly', () => {
    // This replaces a runtime `if (!plan) return false` guard that First
    // Principles correctly identified as unreachable -- the only caller resolves
    // the plan and returns early when there is none.
    //
    // The assertion is the THROW, not the type. `CacheRetentionPlan` being
    // non-nullable stops a null reaching here from typed code, but a type alone
    // is not a test: re-adding the guard AND widening the parameter type back
    // compiles cleanly, and the only thing that notices is this case, because a
    // restored guard would return quietly instead of throwing. A caller that has
    // no plan is a caller that should not have called.
    const sink = recordingSink()
    expect(() => applyCacheRetention(sink, null as unknown as CacheRetentionPlan)).toThrow()
    expect(sink.calls).toHaveLength(0)
  })
})
