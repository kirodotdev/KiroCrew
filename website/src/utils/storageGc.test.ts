import { beforeEach, describe, expect, it } from 'vitest'

import { gcOrphanedStorage } from './storageGc'

/**
 * `storageGc` runs on every app boot and DELETES localStorage keys. Its
 * session-id extraction is
 *
 *     key.slice(prefix.length).split(':')[0]
 *
 * The `split(':')` half is load-bearing, not defensive: `useTouchedFiles`
 * writes a companion watermark key `kirocrew:touched-files:<slot>:toolClearedAt`
 * alongside the list key, and both must resolve to the same session id.
 *
 * That makes the whole GC correct ONLY while slot keys are free of colons. The
 * moment a slot key contains one, `split(':')[0]` truncates it to the part
 * before the colon, that fragment is never in `liveSessionIds`, and every key
 * belonging to that live session is wiped on every boot — height caches, panel
 * tabs, touched files, web-preview state.
 *
 * Slot keys ARE colon-free today: the backend folds every non-`[\w\-.]`
 * character (colons included) to `_` before a key becomes a slot name, so a
 * channel-born conversation surfaces as `slack_1785370133.085469`, never
 * `slack:1785370133.085469`. These tests lock that guarantee in from this side:
 * the underscore-folded forms must survive, and the colon-bearing form is
 * pinned with an explicit characterization test so anyone who lets a raw
 * channel key reach a slot key sees a red test naming the hazard.
 */

const HEIGHTS = 'vc_heights_'
const TOUCHED = 'kirocrew:touched-files:'
const PANEL_TABS = 'mc-panel-tabs:'
const ACTIVITY = 'mc-activity-open:'

describe('gcOrphanedStorage', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('removes keys for a session that no longer exists', () => {
    localStorage.setItem(`${HEIGHTS}chat-1-1`, '{}')
    localStorage.setItem(`${PANEL_TABS}chat-1-1`, '[]')

    expect(gcOrphanedStorage(new Set(['chat-2-2']))).toBe(2)
    expect(localStorage.getItem(`${HEIGHTS}chat-1-1`)).toBeNull()
    expect(localStorage.getItem(`${PANEL_TABS}chat-1-1`)).toBeNull()
  })

  it('keeps keys for a live session', () => {
    localStorage.setItem(`${HEIGHTS}chat-1-1`, '{}')
    localStorage.setItem(`${ACTIVITY}chat-1-1`, 'true')

    expect(gcOrphanedStorage(new Set(['chat-1-1']))).toBe(0)
    expect(localStorage.getItem(`${HEIGHTS}chat-1-1`)).toBe('{}')
    expect(localStorage.getItem(`${ACTIVITY}chat-1-1`)).toBe('true')
  })

  it('leaves keys outside the session-scoped prefixes alone', () => {
    localStorage.setItem('mc-chat-config', '{"simplifiedToolNames":false}')
    localStorage.setItem('theme', 'dark')

    expect(gcOrphanedStorage(new Set())).toBe(0)
    expect(localStorage.getItem('mc-chat-config')).toBe('{"simplifiedToolNames":false}')
    expect(localStorage.getItem('theme')).toBe('dark')
  })

  it('resolves the :toolClearedAt watermark to the same session as its list key', () => {
    // The reason `split(':')` exists at all. Both keys belong to chat-1-100.
    localStorage.setItem(`${TOUCHED}chat-1-100`, '[]')
    localStorage.setItem(`${TOUCHED}chat-1-100:toolClearedAt`, '1700000000000')

    expect(gcOrphanedStorage(new Set(['chat-1-100']))).toBe(0)
    expect(localStorage.getItem(`${TOUCHED}chat-1-100:toolClearedAt`)).toBe('1700000000000')

    // ...and both go together when the session is gone.
    expect(gcOrphanedStorage(new Set(['chat-9-9']))).toBe(2)
    expect(localStorage.getItem(`${TOUCHED}chat-1-100`)).toBeNull()
    expect(localStorage.getItem(`${TOUCHED}chat-1-100:toolClearedAt`)).toBeNull()
  })

  describe('the colon guarantee', () => {
    it('keeps every key of a live channel-born session (underscore-folded)', () => {
      // The shape the backend actually mints: `slack:<ts>` folded to `slack_<ts>`.
      const slot = 'slack_1785370133.085469'
      localStorage.setItem(`${HEIGHTS}${slot}`, '{"0":42}')
      localStorage.setItem(`${PANEL_TABS}${slot}`, '[{"kind":"files"}]')
      localStorage.setItem(`${ACTIVITY}${slot}`, 'true')
      localStorage.setItem(`${TOUCHED}${slot}`, '[{"path":"/x.ts"}]')
      localStorage.setItem(`${TOUCHED}${slot}:toolClearedAt`, '1700000000000')

      expect(gcOrphanedStorage(new Set([slot, 'chat-1-1']))).toBe(0)
      expect(localStorage.getItem(`${HEIGHTS}${slot}`)).toBe('{"0":42}')
      expect(localStorage.getItem(`${PANEL_TABS}${slot}`)).toBe('[{"kind":"files"}]')
      expect(localStorage.getItem(`${ACTIVITY}${slot}`)).toBe('true')
      expect(localStorage.getItem(`${TOUCHED}${slot}`)).toBe('[{"path":"/x.ts"}]')
      expect(localStorage.getItem(`${TOUCHED}${slot}:toolClearedAt`)).toBe('1700000000000')
    })

    it('still collects an ORPHANED channel-born session', () => {
      const slot = 'slack_1785370133.085469'
      localStorage.setItem(`${HEIGHTS}${slot}`, '{}')
      localStorage.setItem(`${PANEL_TABS}${slot}`, '[]')

      expect(gcOrphanedStorage(new Set(['chat-1-1']))).toBe(2)
      expect(localStorage.getItem(`${HEIGHTS}${slot}`)).toBeNull()
    })

    it('WOULD wipe a live session whose slot key contained a colon', () => {
      // Characterization test, deliberately asserting the BROKEN behaviour so
      // the hazard is visible instead of latent. `slack:1785…` truncates to
      // `slack`, which is not in liveSessionIds, so the live session's state is
      // destroyed on boot.
      //
      // If this test starts failing because slot keys are now colon-safe, that
      // is an IMPROVEMENT: delete this case. If it starts failing because a
      // colon-bearing slot key was introduced upstream, the extraction in
      // gcOrphanedStorage must be fixed (split off only a trailing
      // `:toolClearedAt`, not the first colon) before that key can ship.
      const raw = 'slack:1785370133.085469'
      localStorage.setItem(`${HEIGHTS}${raw}`, '{"0":42}')

      expect(gcOrphanedStorage(new Set([raw]))).toBe(1)
      expect(localStorage.getItem(`${HEIGHTS}${raw}`)).toBeNull()
    })

    it('does not wipe a live session whose slot key contains dots or dashes', () => {
      // The other characters a channel key survives the fold with — these must
      // NOT be treated as separators.
      const dotted = 'slack_1785370133.085469'
      const dashed = 'chat-12-345'
      localStorage.setItem(`${HEIGHTS}${dotted}`, '{}')
      localStorage.setItem(`${HEIGHTS}${dashed}`, '{}')

      expect(gcOrphanedStorage(new Set([dotted, dashed]))).toBe(0)
      expect(localStorage.getItem(`${HEIGHTS}${dotted}`)).toBe('{}')
      expect(localStorage.getItem(`${HEIGHTS}${dashed}`)).toBe('{}')
    })
  })

  it('does not delete a key whose remainder is empty', () => {
    // `vc_heights_` with nothing after it yields '' — falsy, so it is skipped
    // rather than deleted as an orphan of the empty session.
    localStorage.setItem(HEIGHTS, '{}')
    expect(gcOrphanedStorage(new Set())).toBe(0)
    expect(localStorage.getItem(HEIGHTS)).toBe('{}')
  })

  it('handles an empty store and an empty live set', () => {
    expect(gcOrphanedStorage(new Set())).toBe(0)
  })
})
