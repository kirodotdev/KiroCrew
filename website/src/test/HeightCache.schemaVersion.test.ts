// Feature: chat-virtualizer — the persisted height blob is VERSIONED, and one
// implausible entry inside a current-version blob is dropped.
//
// Reported as "the transcript is dragged upward whenever I scroll": the
// virtualizer's total height shrank under a reader sitting at the bottom and
// the browser answered by clamping scrollTop to the reduced maximum, moving the
// view up by exactly the height lost. One route into that shrink is this cache.
// `load()` validated SHAPE only, so heights persisted by a build with different
// measurement semantics were loaded as truth, priced the offset tree, and were
// then corrected DOWNWARD as rows really measured.
//
// The version stamp closes that route by refusing the blob instead of trusting
// it. A refused blob is not a loss: the rows load as UNMEASURED, which is the
// state the estimate path already handles.

import { describe, it, expect, beforeEach } from 'vitest'

import {
  HeightCache,
  HEIGHT_SCHEMA_VERSION,
  SCHEMA_VERSION_KEY,
} from '../hooks/virtualizer/HeightCache'

/** The fallback `averageHeight()` returns when nothing is measured. Mirrors
 *  DEFAULT_ESTIMATED_HEIGHT, which the module keeps private. */
const DEFAULT_AVERAGE = 100

const keyFor = (sid: string) => `vc_heights_${sid}`

beforeEach(() => {
  window.localStorage.clear()
})

describe('HeightCache: persisted schema version', () => {
  it('discards a blob with no version and reports nothing measured', () => {
    const sid = 'unversioned'
    // Exactly the shape every pre-version build wrote: bare key -> height.
    window.localStorage.setItem(keyFor(sid), JSON.stringify({ a: 300, b: 420, c: 180 }))

    const c = new HeightCache(sid)

    // Empty, not "loaded and later corrected" — the correction is the shrink.
    expect(c.size()).toBe(0)
    expect(c.get('a')).toBeUndefined()
    // And the mean must fall back rather than report a mean of foreign numbers,
    // because averageHeight() prices every UNMEASURED row in the session.
    expect(c.averageHeight()).toBe(DEFAULT_AVERAGE)
  })

  it('discards a blob written by a different schema version', () => {
    const sid = 'stale-version'
    window.localStorage.setItem(
      keyFor(sid),
      JSON.stringify({ [SCHEMA_VERSION_KEY]: 'h0-from-an-older-build', a: 300, b: 420 }),
    )

    const c = new HeightCache(sid)

    expect(c.size()).toBe(0)
    expect(c.averageHeight()).toBe(DEFAULT_AVERAGE)
  })

  it('removes the refused blob so the next open does not re-read it', () => {
    const sid = 'refused-removed'
    window.localStorage.setItem(keyFor(sid), JSON.stringify({ a: 300 }))

    new HeightCache(sid)

    expect(window.localStorage.getItem(keyFor(sid))).toBeNull()
  })

  it('refuses a blob whose version slot holds a number, not the version', () => {
    // The only value a colliding ROW key could put here is a height. Comparing
    // the version as a string makes that collision fail CLOSED (discard) rather
    // than matching some numeric version by accident.
    const sid = 'numeric-version'
    window.localStorage.setItem(
      keyFor(sid),
      JSON.stringify({ [SCHEMA_VERSION_KEY]: 1, a: 300 }),
    )

    expect(new HeightCache(sid).size()).toBe(0)
  })

  it('loads a current-version blob and never surfaces the version as a row', () => {
    const sid = 'current-version'
    window.localStorage.setItem(
      keyFor(sid),
      JSON.stringify({ [SCHEMA_VERSION_KEY]: HEIGHT_SCHEMA_VERSION, a: 300, b: 500 }),
    )

    const c = new HeightCache(sid)

    expect(c.size()).toBe(2)
    expect(c.get('a')).toBe(300)
    expect(c.get('b')).toBe(500)
    // The stamp is not a measurement: it must not become a cache entry, must
    // not enter the mean, and must not be readable as a height.
    expect(c.get(SCHEMA_VERSION_KEY)).toBeUndefined()
    expect(c.averageHeight()).toBe(400)
  })

  it('round-trips its own write through a second instance', () => {
    const sid = 'round-trip'
    const first = new HeightCache(sid)
    first.set('a', 300)
    first.set('b', 500)
    first.flush()

    const raw = window.localStorage.getItem(keyFor(sid))
    expect(raw).not.toBeNull()
    expect(JSON.parse(raw!)[SCHEMA_VERSION_KEY]).toBe(HEIGHT_SCHEMA_VERSION)

    // What this build writes, this build must be able to read back — the guard
    // has to reject foreign blobs without rejecting its own.
    const second = new HeightCache(sid)
    expect(second.size()).toBe(2)
    expect(second.get('a')).toBe(300)
    expect(second.get('b')).toBe(500)
  })

  it('keeps a row whose key collides with the version slot out of the blob', () => {
    const sid = 'colliding-row-key'
    const c = new HeightCache(sid)
    c.set(SCHEMA_VERSION_KEY, 300)
    c.set('a', 420)
    c.flush()

    const persisted = JSON.parse(window.localStorage.getItem(keyFor(sid))!)
    // The stamp survives intact — a row cannot overwrite it — so the blob is
    // still loadable. The colliding row simply is not persisted.
    expect(persisted[SCHEMA_VERSION_KEY]).toBe(HEIGHT_SCHEMA_VERSION)
    expect(persisted.a).toBe(420)
    expect(new HeightCache(sid).get('a')).toBe(420)
  })
})
