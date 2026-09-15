// The composer and the transcript scroller are siblings in one column, so both
// viewport changes the transcript sees are the SAME geometry and separable only by
// cause: a composer growing under the reader's typing must NOT re-pin a follower
// (that is the phone bounce), while a banner mounting must.
//
// The existing coverage for this module was source-text assertions in
// useVirtualChat.viewportResize.test.tsx -- they pin that the call sites exist,
// which is worth keeping, but they execute zero lines of the module itself. These
// drive the real functions.

import { describe, it, expect, beforeEach } from 'vitest'

import {
  COMPOSER_RESIZE_ATTRIBUTION_MS,
  markComposerResize,
  composerExplainsViewportChange,
  __resetComposerResizeMark,
} from '../utils/composerResize'

describe('composer resize attribution', () => {
  beforeEach(() => { __resetComposerResizeMark() })

  it('explains nothing before the composer has ever resized', () => {
    // The `> 0` half of the predicate, and it needs a `now` INSIDE the window of
    // the zero stamp to test anything: at now=1000 the difference is 1000ms, so a
    // build without the `> 0` guard answers false too and the test proves nothing.
    // A fake-timer suite starting near epoch is exactly the real shape here.
    expect(composerExplainsViewportChange(COMPOSER_RESIZE_ATTRIBUTION_MS - 1)).toBe(false)
    expect(composerExplainsViewportChange(0)).toBe(false)
  })

  it('explains a viewport change inside the window', () => {
    markComposerResize(1_000)
    expect(composerExplainsViewportChange(1_000)).toBe(true)
    expect(composerExplainsViewportChange(1_000 + COMPOSER_RESIZE_ATTRIBUTION_MS)).toBe(true)
  })

  it('stops explaining one millisecond past the window', () => {
    // The bound is inclusive, so this is the first instant a genuine chrome
    // shrink is allowed to re-pin again.
    markComposerResize(1_000)
    expect(composerExplainsViewportChange(1_000 + COMPOSER_RESIZE_ATTRIBUTION_MS + 1)).toBe(false)
  })

  it('reads the clock when no timestamp is passed', () => {
    // Both functions default to Date.now(), which is how the real call sites use
    // them -- the autosizer marks without an argument and the RO branch asks
    // without one. Exercised together so the defaults are not dead code.
    markComposerResize()
    expect(composerExplainsViewportChange()).toBe(true)
  })

  it('re-arms on a later resize rather than staying expired', () => {
    markComposerResize(1_000)
    expect(composerExplainsViewportChange(2_000)).toBe(false)
    markComposerResize(2_000)
    expect(composerExplainsViewportChange(2_000)).toBe(true)
  })
})
