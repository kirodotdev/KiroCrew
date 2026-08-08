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
  newSegmentText,
  resolveEnabledAgents,
} from '../apps/meetings/hooks/useMeetingSession'
import {
  CAPTION_WINDOW_CHARS,
  captionWindow,
} from '../apps/meetings/hooks/useMeetingTranscription'
import type { AgentDef, MeetingsConfig } from '../apps/meetings/api'
import { readFileSync } from 'node:fs'
import EN_CATALOG from '../i18n/locales/en.json'

// Plain repo-relative paths, the convention in PapyrusCloseProject.test.tsx:
// vitest runs with `website/` as cwd.
const TranscriptionSource = readFileSync(
  'src/apps/meetings/hooks/useMeetingTranscription.ts', 'utf-8',
)
const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const SettingsSource = readFileSync('src/apps/meetings/SettingsView.tsx', 'utf-8')

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

describe('final-segment dispatch is retried, never swallowed', () => {
  // A dispatch is the ONLY path a final segment reaches the agents, so a
  // rejection dropped on the floor means the notes and tasks silently omit that
  // stretch of the meeting — the same "a queue is discarded without being
  // drained" failure the backend teardown paths were fixed for, reached from the
  // client. `.catch(() => {})` was exactly that.
  //
  // Asserted against the SOURCE. The retry lives inside `useMeetingTranscription`
  // between a live WebSocket handler and `meetingsApi`, so exercising it for real
  // needs a WebSocket + AudioContext + MediaStream harness; a test built on those
  // mocks would assert the mocks' behaviour, not the shipped code's. Stating the
  // limitation beats a test that looks behavioural and is not.

  it('does not swallow a dispatch rejection', () => {
    expect(TranscriptionSource).not.toMatch(/dispatch\([^)]*\)\.catch\(\(\)\s*=>\s*\{\s*\}\)/)
  })

  it('routes the dispatch through the retry helper', () => {
    // `toDispatch`, not `text`: the caller's dedup returns only the NEW suffix of
    // a growing final, and dispatching the raw text would re-send what the agents
    // already have (see the growing-final suite below).
    expect(TranscriptionSource).toContain('dispatchWithRetry(toDispatch)')
    // The helper awaits the API and retries on the declared schedule.
    const helper = TranscriptionSource.match(
      /const dispatchWithRetry[\s\S]*?\n  \},\n?\s*\[[^\]]*\],?\n?\s*\)/,
    )
    expect(helper, 'dispatchWithRetry not found').not.toBeNull()
    expect(helper![0]).toContain('meetingsApi.dispatch')
    expect(helper![0]).toContain('DISPATCH_RETRY_DELAYS_MS')
  })

  it('reports the give-up instead of failing silently', () => {
    // Bounded retry is only acceptable because exhausting it is VISIBLE: the
    // segment really is lost, so the user has to be told rather than left with a
    // quiet gap in the notes.
    const helper = TranscriptionSource.match(
      /const dispatchWithRetry[\s\S]*?\n  \},\n?\s*\[[^\]]*\],?\n?\s*\)/,
    )
    expect(helper![0]).toContain('onErrorRef.current?.(')
  })

  it('the reported code has a distinct catalog key', () => {
    // Reusing `sttUnavailable` would misdescribe it — the microphone is fine and
    // the audio was recognized; only the delivery failed.
    expect(SessionSource).toContain("dispatch: 'apps.meetings.session.sttDispatchFailed'")
    expect(EN_CATALOG.apps.meetings.session.sttDispatchFailed).toBeTruthy()
  })
})

describe('transcription restarts after an unexpected disconnect', () => {
  // The status-binding effect keyed on `status` ALONE, so the binding was
  // one-directional: if the socket dropped while the meeting was active, the
  // transcription hook's `cleanup()` set `active` false, `status` had not changed,
  // and the effect never re-ran. Transcription stayed dead for the rest of the
  // meeting while the UI still showed Live, and every word after that point was
  // missing from the notes. The hook's watchdog reconnects a STALLED socket; one
  // that closes cleanly and unexpectedly lands here instead.
  //
  // Source contract: the effect lives inside `useMeetingSession` and driving it
  // for real needs a WebSocket + AudioContext + MediaStream harness (see the
  // dispatch suite above for the same reasoning).

  it('keys the binding effect on the transcription active flag too', () => {
    const effect = SessionSource.match(
      /Bind the microphone[\s\S]*?\n  \}, \[([^\]]*)\]\)/,
    )
    expect(effect, 'the status-binding effect was not found').not.toBeNull()
    const deps = effect![1]
    expect(deps).toContain('status')
    expect(deps).toContain('transcriptionActive')
  })

  it('reads the flag into a value the dependency array can compare', () => {
    // `transcriptionRef.current.active` in the array would compare the REF, which
    // never changes, so the effect would still not re-run.
    expect(SessionSource).toContain('const transcriptionActive = transcription.active')
  })
})

describe('a growing final contributes only its new words', () => {
  // STT revises a final upward: `"yes"` then `"yes please"` inside the dedup
  // window. The old boolean dedup correctly saw the overlap and suppressed the
  // second outright — which discarded "please", so the notes and task extraction
  // never saw it. For short affirmations that is most of the time.
  const prev = (text: string) => ({ text, ts: 1_000 })

  it('sends only the added suffix', () => {
    expect(newSegmentText('yes please', prev('yes'), 1_500)).toBe('please')
  })

  it('sends nothing for an exact repeat', () => {
    expect(newSegmentText('yes', prev('yes'), 1_500)).toBe('')
  })

  it('sends nothing for a shorter re-recognition already covered', () => {
    // Guessing a diff here would hand the agents a fragment out of context.
    expect(newSegmentText('yes', prev('yes please'), 1_500)).toBe('')
  })

  it('sends the whole segment when it is genuinely new', () => {
    expect(newSegmentText('next topic', prev('yes'), 1_500)).toBe('next topic')
  })

  it('sends the whole segment once the dedup window has passed', () => {
    // A real repetition minutes later is speech, not a revision.
    expect(newSegmentText('yes', prev('yes'), 1_000 + 60_000)).toBe('yes')
  })

  it('drops whitespace-only input', () => {
    expect(newSegmentText('   ', prev('yes'), 1_500)).toBe('')
  })
})

describe('an ambiguous dispatch failure is not retried', () => {
  // The dispatch endpoint broadcasts to every agent queue BEFORE it responds, so a
  // bare fetch rejection (connection reset, navigation, TLS drop) is ambiguous: the
  // segment may already have been accepted, and retrying would duplicate it into
  // all of them. Duplicated transcript is worse than a reported gap — the notes
  // silently repeat a passage and the task extractor files the same action item
  // twice, with nothing to indicate why.
  //
  // This narrows my own earlier retry fix, which caught everything.

  it('retries only a failure the server explicitly reported', () => {
    const helper = TranscriptionSource.match(
      /const dispatchWithRetry[\s\S]*?\n  \},\n?\s*\[[^\]]*\],?\n?\s*\)/,
    )
    expect(helper, 'dispatchWithRetry not found').not.toBeNull()
    // A status-bearing error means a response arrived, so the request was rejected.
    expect(helper![0]).toContain('error instanceof MeetingsApiError')
    // ...and anything else short-circuits to the report rather than looping.
    expect(helper![0]).toMatch(/!reported \|\| attempt >=/)
  })

  it('still reports the give-up in both cases', () => {
    const helper = TranscriptionSource.match(
      /const dispatchWithRetry[\s\S]*?\n  \},\n?\s*\[[^\]]*\],?\n?\s*\)/,
    )
    expect(helper![0]).toContain("onErrorRef.current?.('dispatch')")
  })
})

describe('start() cannot run twice concurrently', () => {
  // `wsRef` alone does not prevent it: that is assigned only once the socket is
  // created, and everything before it is awaited (getUserMedia, the AudioWorklet
  // module). Two calls landing in that window both proceed, leaving two microphone
  // streams and two sockets whose finals are dispatched twice.
  //
  // The watchdog reaches it: its `cleanup()` clears `active`, which the session hook
  // now watches to restart a dropped socket — so the watchdog's own `start()` and
  // that effect race. This is an interaction between two of my own fixes, which is
  // why the guard lives with the function that owns the invariant.

  it('takes an in-progress guard before the first await', () => {
    const body = TranscriptionSource.match(/const start = useCallback\([\s\S]*?\n  \}, \[/)
    expect(body, 'start() not found').not.toBeNull()
    const guardAt = body![0].indexOf('startingRef.current = true')
    const firstAwait = body![0].indexOf('await ')
    expect(guardAt).toBeGreaterThan(-1)
    expect(firstAwait).toBeGreaterThan(-1)
    expect(guardAt).toBeLessThan(firstAwait)
  })

  it('releases the guard in cleanup, so a failed start cannot wedge it', () => {
    // `cleanup` runs on every teardown path INCLUDING start's own failure exits, so
    // releasing there is what stops a partway failure blocking every later attempt
    // for the rest of the meeting.
    const cleanup = TranscriptionSource.match(/const cleanup = useCallback\([\s\S]*?\n  \}, \[/)
    expect(cleanup, 'cleanup() not found').not.toBeNull()
    expect(cleanup![0]).toContain('startingRef.current = false')
  })

  it('releases the guard once the session is live', () => {
    expect(TranscriptionSource).toMatch(
      /startingRef\.current = false\n\s*setActive\(true\)/,
    )
  })
})

describe('the configured default preset survives an async config', () => {
  // `useState(config?.default_preset ?? '')` captures its initial value ONCE, and
  // `config` arrives from a query — so a meeting opened before that resolved kept
  // `''` forever and started with the roster defaults instead of the configured
  // preset, silently omitting whatever agents the preset adds.

  it('applies the default preset when the config lands', () => {
    const effect = SessionSource.match(
      /const presetAppliedRef[\s\S]*?\n  \}, \[config\?\.default_preset\]\)/,
    )
    expect(effect, 'no effect keyed on config.default_preset').not.toBeNull()
    expect(effect![0]).toContain('setSelectedPreset')
  })

  it('never overwrites a preset the user chose', () => {
    // `current || preset` keeps a real selection; the ref stops a config refetch
    // re-applying the default after the user cleared it.
    const effect = SessionSource.match(
      /const presetAppliedRef[\s\S]*?\n  \}, \[config\?\.default_preset\]\)/,
    )
    expect(effect![0]).toContain('current => current || preset')
    expect(effect![0]).toContain('presetAppliedRef.current')
  })
})

describe('task sidebar inputs track the server value', () => {
  // `defaultValue` is read on MOUNT only. So when the extractor agent revised a
  // task, the poll updated the props and the input kept showing the old text — and
  // `onBlur` then wrote that stale value back, silently reverting the agent's
  // update. Keying on the server value remounts the input when the server's copy
  // changes, which is what makes the refresh visible.

  const sidebar = readFileSync(
    'src/apps/meetings/components/TaskSidebar.tsx',
    'utf-8',
  )

  it('keys the description input on the server value', () => {
    expect(sidebar).toContain('key={`desc:${task.description}`}')
  })

  it('keys the assignee input on the server value', () => {
    expect(sidebar).toContain('key={`assignee:${task.assignee}`}')
  })

  it('every defaultValue input carries a value-derived key', () => {
    // Structural: a THIRD editable field added later must not reintroduce this.
    const inputs = sidebar.match(/<Input\b[\s\S]*?\/>/g) ?? []
    const withDefault = inputs.filter(i => i.includes('defaultValue='))
    expect(withDefault.length).toBeGreaterThan(0)
    for (const input of withDefault) {
      expect(input, `an input uses defaultValue without a key: ${input}`).toMatch(
        /key=\{`[^`]*\$\{task\./,
      )
    }
  })
})

describe('settings saves cannot revert each other', () => {
  // The backend PUT is a full, validated replace, so each patch carries the whole
  // config. Built from the render-time snapshot, two rapid changes each sent a base
  // missing the other's change and the later response reverted it.
  const settings = readFileSync('src/apps/meetings/SettingsView.tsx', 'utf-8')

  it('derives the payload from the CACHE, not the render snapshot', () => {
    // `onSuccess` writes the server's response into the cache, so reading it at send
    // time means the second save builds on the first's accepted result.
    expect(settings).toContain("queryClient.getQueryData<ConfigResponse>(['meetings', 'config'])")
  })

  it('chains saves so two are never in flight together', () => {
    // Reading the cache is not sufficient alone: with two requests outstanding the
    // first has not landed when the second is built. Awaiting the previous save is
    // what makes each payload derive from a config the server already accepted.
    expect(settings).toContain('savesRef')
    expect(settings).toMatch(/savesRef\.current = savesRef\.current[\s\S]*?mutateAsync/)
  })

  it('a failed save does not wedge the chain', () => {
    expect(settings).toMatch(/\.catch\(\(\) => undefined\)/)
  })
})

describe('settings layout remains scrollable', () => {
  it('keeps cards out of a shrinking flex column', () => {
    // Cards hide overflow for their glow treatment. Making them flex items lets
    // the browser shrink each card to the viewport, clipping its contents while
    // leaving the scroll container with no overflow to scroll.
    const scrollContainer = SettingsSource.match(
      /<div className="([^"]*overflow-y-auto[^"]*)">/,
    )

    expect(scrollContainer).not.toBeNull()
    expect(scrollContainer![1]).toContain('flex-1 min-h-0')
    expect(scrollContainer![1]).not.toMatch(/(?:^|\s)flex(?:\s|$)/)
    expect(scrollContainer![1]).not.toContain('flex-col')
  })
})

describe('a nested settings patch is derived from the latest config', () => {
  // Chaining the saves and reading the cache fixed the SCALAR case. A field derived
  // from the config — `meeting_agents` mapped from the existing array, `calendar`
  // spread to keep its other key — was still computed at call time from the render
  // snapshot, so two rapid toggles each queued a value missing the other's change.
  const settings = readFileSync('src/apps/meetings/SettingsView.tsx', 'utf-8')

  it('patch accepts an updater resolved against the latest config', () => {
    expect(settings).toMatch(/\(latest: MeetingsConfig\) => Partial<MeetingsConfig>/)
    expect(settings).toContain("typeof changes === 'function' ? changes(latest) : changes")
  })

  it('no patch payload reads the render-time config', () => {
    // Structural: the object form is for SCALARS only. A payload that reads `config`
    // is the shape that was wrong — it captures the snapshot — so such a call has to
    // use the `latest =>` form instead. Scanning one line per call keeps this from
    // over-matching the surrounding JSX.
    const offenders = settings
      .split('\n')
      .filter(line => /patch\(\{/.test(line) && /\bconfig[?.]/.test(line))
    expect(offenders, `these patch payloads read the snapshot: ${offenders.join(' | ')}`)
      .toEqual([])
  })

  it('the agent and calendar patches both use the updater form', () => {
    // The two nested fields: an array mapped from the existing one, and an object
    // whose other key must be preserved.
    expect(settings).toContain('latest.meeting_agents.map(')
    expect(settings).toContain('calendar: { ...latest.calendar,')
  })
})

describe('starting before the config loads does not persist an empty roster', () => {
  // `resolveEnabledAgents` derives the roster from `config.meeting_agents`, so
  // before the config query resolves it can only return `[]`. And `[]` is not
  // "unknown" to the start endpoint — `field_str_list` deliberately distinguishes
  // absent ("use the configured defaults") from `[]` ("run no agents"). So a
  // meeting started in that window persisted an explicit empty roster: the
  // note-taker, the diagram agent and every other configured agent never ran, and
  // the meeting recorded audio while producing nothing.

  it('omits agents_enabled entirely while the roster is unknown', () => {
    // OMITTED, not `[]` — `JSON.stringify` drops an `undefined` key, so the server
    // sees an absent field and falls back to the defaults.
    expect(SessionSource).toContain('agents_enabled: rosterIsKnown ? enabledIds : undefined')
  })

  it('treats the roster as known once either source has arrived', () => {
    // Two sources: a roster already persisted on the meeting, or a loaded config to
    // derive one from. Either is enough.
    expect(SessionSource).toContain(
      'const rosterIsKnown = Boolean(meta?.agents_enabled) || Boolean(config)',
    )
  })

  it('keys on the config arriving, not on the roster being non-empty', () => {
    // An empty roster the USER chose is a legitimate state and must still be sent
    // verbatim, so `enabledIds.length` is the wrong question — it would silently
    // convert "run no agents" back into "run the defaults".
    const startCall = SessionSource.match(/const startMutation[\s\S]*?\n  \}\)/)
    expect(startCall, 'no startMutation found').not.toBeNull()
    expect(startCall![0]).not.toMatch(/enabledIds\.length/)
  })
})

describe('the live caption shows the newest speech, not the meeting opening', () => {
  // `finalsRef` accumulates every final segment and is cleared only by `start()`,
  // so by mid-meeting it holds the entire transcript. It used to be handed to the
  // caption verbatim, into an element that clipped its overflow — and
  // `text-overflow: ellipsis` shows a string's HEAD. The visible caption was
  // therefore the meeting's first sentence, frozen for its whole duration.

  const SEGMENTS = Array.from(
    { length: 40 },
    (_, i) => `segment ${i} with several spoken words in it`,
  )

  it('drops the oldest segments once the window is full', () => {
    const out = captionWindow(SEGMENTS)
    expect(out).toContain('segment 39')
    // The defect, stated as an assertion: the opening of the meeting must be gone.
    expect(out).not.toContain('segment 0 ')
  })

  it('bounds what the caption element is asked to render', () => {
    expect(captionWindow(SEGMENTS).length).toBeLessThanOrEqual(CAPTION_WINDOW_CHARS)
  })

  it('leaves a transcript that already fits completely alone', () => {
    // The common case must not be trimmed at all.
    expect(captionWindow(['hello there', 'how are you'])).toBe('hello there how are you')
  })

  it('keeps the in-flight partial at the end', () => {
    expect(captionWindow(['committed words'], 'and the partial')).toBe(
      'committed words and the partial',
    )
    expect(captionWindow([], 'partial only')).toBe('partial only')
    expect(captionWindow([])).toBe('')
  })

  it('keeps the tail, not the head, when one segment alone overflows', () => {
    const long = Array.from({ length: 60 }, (_, i) => `word${i}`).join(' ')
    expect(long.length).toBeGreaterThan(CAPTION_WINDOW_CHARS)
    const out = captionWindow([long])
    expect(out.length).toBeLessThanOrEqual(CAPTION_WINDOW_CHARS)
    // A suffix of the segment: the newest words survive, the oldest are cut.
    expect(long.endsWith(out)).toBe(true)
    // And cut at a word boundary, so the caption never opens mid-token.
    expect(out.startsWith('word')).toBe(true)
  })

  it('routes both caption call sites through the bounded window', () => {
    // Exporting the helper is not enough. A call site left on the raw
    // accumulation would restore the bug while every assertion above still
    // passed, so the guard is on the source itself.
    expect(TranscriptionSource).not.toContain("finalsRef.current.join(' ')")
    expect(TranscriptionSource).toContain('captionWindow(finalsRef.current, lastPartial)')
    expect(TranscriptionSource).toContain('captionWindow(finalsRef.current)')
  })
})
