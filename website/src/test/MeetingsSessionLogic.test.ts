// The pure decision functions behind the meeting session hook.
//
// Ported from the upstream app's own tests, which exercised the same three rules
// as inline copies of the hook's logic. Here they are exported from the hook, so
// the test binds to the SHIPPING code rather than a duplicate that can drift.

import { describe, it, expect } from 'vitest'

import {
  ALLOWED_TRANSITIONS,
  canTransition,
  isDuplicateSegment,
  resolveEnabledAgents,
} from '../apps/meetings/hooks/useMeetingSession'
import type { AgentDef, MeetingsConfig } from '../apps/meetings/api'

const AGENTS: AgentDef[] = [
  { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown', enabled_by_default: true },
  { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html', enabled_by_default: false },
  { id: 'summarizer', name: 'Summarizer', widget_type: 'markdown', enabled_by_default: true },
]

const CONFIG = {
  presets: {
    standup: { enabled_agents: ['note-taker'] },
    design: { enabled_agents: ['note-taker', 'sketch-artist'] },
    empty: { enabled_agents: [] },
  },
  meeting_agents: AGENTS,
} as unknown as MeetingsConfig

describe('isDuplicateSegment', () => {
  const now = 1_000_000

  it('accepts the first segment', () => {
    expect(isDuplicateSegment('hello world', { text: '', ts: 0 }, now)).toBe(false)
  })

  it('rejects empty and whitespace-only text', () => {
    expect(isDuplicateSegment('', { text: '', ts: 0 }, now)).toBe(true)
    expect(isDuplicateSegment('   ', { text: '', ts: 0 }, now)).toBe(true)
  })

  it('rejects an exact repeat inside the window', () => {
    expect(isDuplicateSegment('hello world', { text: 'hello world', ts: now - 1000 }, now)).toBe(true)
  })

  it('rejects a substring of the previous segment', () => {
    // Speech recognition re-emits a shortened form of what it already committed.
    expect(
      isDuplicateSegment('meeting has started', { text: 'the meeting has started now', ts: now - 500 }, now),
    ).toBe(true)
  })

  it('rejects a superstring of the previous segment', () => {
    expect(isDuplicateSegment('hello world', { text: 'hello', ts: now - 500 }, now)).toBe(true)
  })

  it('accepts the same text once the window has passed', () => {
    // A genuinely repeated sentence 5s later is new information, not an echo.
    expect(isDuplicateSegment('hello world', { text: 'hello world', ts: now - 5001 }, now)).toBe(false)
  })

  it('accepts different text inside the window', () => {
    expect(
      isDuplicateSegment('completely different', { text: 'first message', ts: now - 100 }, now),
    ).toBe(false)
  })
})

describe('resolveEnabledAgents', () => {
  it('uses a preset when one is selected', () => {
    expect(resolveEnabledAgents('standup', CONFIG, AGENTS)).toEqual(['note-taker'])
    expect(resolveEnabledAgents('design', CONFIG, AGENTS)).toEqual(['note-taker', 'sketch-artist'])
  })

  it('falls back to the roster defaults with no preset', () => {
    expect(resolveEnabledAgents('', CONFIG, AGENTS)).toEqual(['note-taker', 'summarizer'])
  })

  it('falls back for an unknown preset name', () => {
    expect(resolveEnabledAgents('ghost', CONFIG, AGENTS)).toEqual(['note-taker', 'summarizer'])
  })

  it('falls back for a preset that enables nothing', () => {
    // An empty preset is indistinguishable from an unset one, and defaulting to
    // "no agents" would silently capture nothing for the whole meeting.
    expect(resolveEnabledAgents('empty', CONFIG, AGENTS)).toEqual(['note-taker', 'summarizer'])
  })

  it('treats a missing enabled_by_default as enabled', () => {
    const agents: AgentDef[] = [{ id: 'x', name: 'X', widget_type: 'markdown' }]
    expect(resolveEnabledAgents('', undefined, agents)).toEqual(['x'])
  })
})

describe('meeting status transitions', () => {
  it.each([
    ['idle', 'active', true],
    ['active', 'paused', true],
    ['active', 'reviewing', true],
    ['paused', 'active', true],
    ['paused', 'reviewing', true],
    ['reviewing', 'ended', true],
    ['ended', 'active', true],
    ['idle', 'ended', false],
    ['idle', 'reviewing', false],
    ['active', 'ended', false],
    ['reviewing', 'active', false],
  ] as const)('%s -> %s is %s', (from, to, allowed) => {
    expect(canTransition(from, to)).toBe(allowed)
  })

  it('never lets a meeting reach ended without passing through review', () => {
    // The review gate is the app's product promise: no action item is silently
    // dropped. A direct active -> ended edge would bypass it.
    for (const [from, targets] of Object.entries(ALLOWED_TRANSITIONS)) {
      if (from !== 'reviewing') expect(targets).not.toContain('ended')
    }
  })
})

describe('the duplicate check actually gates the dispatch', () => {
  // Regression: `onSegment` computed `isDuplicateSegment` and early-returned,
  // but returned `void` — so the caller had no channel to act on it and
  // dispatched every final unconditionally. The whole dedup mechanism, and the
  // tests above, exercised a result nothing read: overlapping finals reached
  // every listening agent twice (duplicated notes, tasks, and agent turns).
  // `onFinal` now returns `boolean | void` and the transcription hook skips the
  // dispatch on an explicit `false`.
  function dispatchDecisions(segments: string[], onFinal: (t: string) => boolean | void) {
    const dispatched: string[] = []
    for (const text of segments) {
      // Mirrors the guard in useMeetingTranscription's onmessage handler.
      if (onFinal(text) === false) continue
      dispatched.push(text)
    }
    return dispatched
  }

  /** The real onSegment logic, over a monotonically advancing clock. */
  function makeOnSegment() {
    let last = { text: '', ts: 0 }
    let clock = 1_000
    return (text: string): boolean => {
      clock += 100
      if (isDuplicateSegment(text, last, clock)) return false
      last = { text, ts: clock }
      return true
    }
  }

  it('suppresses an overlapping repeat instead of dispatching it twice', () => {
    const dispatched = dispatchDecisions(
      ['the meeting has started', 'the meeting has started', 'next topic please'],
      makeOnSegment(),
    )
    expect(dispatched).toEqual(['the meeting has started', 'next topic please'])
  })

  it('suppresses a prefix-overlap final, the shape STT actually emits', () => {
    const dispatched = dispatchDecisions(['hello', 'hello world'], makeOnSegment())
    expect(dispatched).toEqual(['hello'])
  })

  it('still dispatches genuinely distinct segments', () => {
    const dispatched = dispatchDecisions(['first point', 'second point'], makeOnSegment())
    expect(dispatched).toEqual(['first point', 'second point'])
  })

  it('a caller that returns nothing keeps dispatching (opt-in suppression)', () => {
    const dispatched = dispatchDecisions(['a', 'a'], () => undefined)
    expect(dispatched).toEqual(['a', 'a'])
  })
})
