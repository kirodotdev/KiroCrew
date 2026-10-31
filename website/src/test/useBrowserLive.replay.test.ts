import { describe, it, expect, beforeEach, afterEach } from 'vitest'

import {
  activeEvent, currentStep, durationOf, frameAt, nextOffset, offsetOf, prevOffset, readRecording,
  readRecordingList, recordingFromMemory, pickRecording, scrollDirection, toPercent, OVERLAY_HOLD_MS, frameForAction, ACTION_FRAME_LEAD_MS,
} from '../hooks/useBrowserLive.replay'
import { browserLiveStore } from '../hooks/useBrowserLive'
import { browserStageStore, stageTurnId } from '../hooks/useBrowserLive.stage'
import { containSize } from '../pages/chat/BrowserLiveReplayStage'
import { replayKeyAction, clockLabel } from '../pages/chat/BrowserLiveTimeline'
import { turnChoices } from '../pages/chat/BrowserLiveStage'

/**
 * The replay's data and arithmetic, without a DOM: the loose wire parse, the
 * in-memory fallback, the play-head maths (frame at t, the action drawn at t,
 * step back / forward), the overlay's coordinate mapping, and the stage store.
 */

const S = 'slot:abc'
const T0 = 1_700_000_000_000

const wire = {
  turn_id: 't1', started_ts: T0, ended_ts: T0 + 10_000, live: false, width: 1280, height: 800,
  extra_field: 'ignored',
  frames: [
    { i: 2, ts: T0 + 4000, w: 1280, h: 800 },
    { i: 0, ts: T0, w: 1280, h: 800 },
    { i: 1, ts: T0 + 2000, w: 640, h: 400 },
    { i: 'bad', ts: T0 },
  ],
  events: [
    { id: 'e2', ts: T0 + 5000, op: 'type', label: 'Type “kiro”', args: {}, point: { x: 640, y: 400 }, url: 'https://a.test/', status: 'ok', actor: 'agent' },
    { id: 'e1', ts: T0 + 1000, op: 'click', label: 'Click Sign in', point: { x: 320, y: 200 } },
    { ts: T0 + 8000, op: 'weird-op', label: 'Something' },
    { op: 'click', label: 'no time' },
    { id: 'e4', ts: T0 + 9000, op: 'drag', path: [{ x: 0, y: 0 }, { x: 10, y: 10 }], point: null, status: 'error', actor: 'user' },
  ],
}

describe('readRecording', () => {
  it('reads loosely: sorts, drops what cannot be placed, defaults the rest', () => {
    const rec = readRecording(wire, S)!
    expect(rec.turnId).toBe('t1')
    expect(rec.frames.map(f => f.i)).toEqual([0, 1, 2])
    expect(rec.frames[0].src).toBe('/api/browser/live/slot%3Aabc/recordings/t1/frames/0.jpg')
    expect(rec.events.map(e => e.id)).toEqual(['e1', 'e2', 'e3', 'e4'])
    expect(rec.events[2].op).toBe('other')          // an unknown op is kept as "other"
    expect(rec.events[1].args).toEqual({})
    expect(rec.events[0].status).toBe('ok')         // missing -> ok
    expect(rec.events[0].actor).toBe('agent')       // missing -> agent
    expect(rec.events[3]).toMatchObject({ status: 'error', actor: 'user', path: [{ x: 0, y: 0 }, { x: 10, y: 10 }] })
    expect(rec.width).toBe(1280)
    expect(durationOf(rec)).toBe(10_000)
  })

  it('is null for a body that is not a recording', () => {
    expect(readRecording(null, S)).toBeNull()
    expect(readRecording({ frames: [] }, S)).toBeNull()
    expect(readRecording('nope', S)).toBeNull()
  })

  it('missing size falls back to the last frame', () => {
    const rec = readRecording({ turn_id: 'x', frames: [{ i: 0, ts: 5, w: 300, h: 200 }] }, S)!
    expect(rec).toMatchObject({ width: 300, height: 200, startedTs: 5, live: false, endedTs: null })
  })

  it('reads the turn list and skips rows without an id or a start', () => {
    expect(readRecordingList({ turns: [
      { turn_id: 'a', started_ts: 1, ended_ts: null, live: true, frame_count: 3, event_count: 2, bytes: 9, start_url: 'https://x.test/' },
      { turn_id: '', started_ts: 1 },
      { started_ts: 2 },
    ] })).toEqual([{ turnId: 'a', startedTs: 1, endedTs: null, live: true, frameCount: 3, eventCount: 2, startUrl: 'https://x.test/' }])
    expect(readRecordingList({})).toEqual([])
    expect(readRecordingList(null)).toEqual([])
  })
})

describe('the play head', () => {
  const rec = readRecording(wire, S)!

  it('shows the last frame at or before t, and the first before any', () => {
    expect(frameAt(rec, 0)).toBe(0)
    expect(frameAt(rec, 1999)).toBe(0)
    expect(frameAt(rec, 2000)).toBe(1)
    expect(frameAt(rec, 9000)).toBe(2)
  })

  it('draws an action only for a short hold after it', () => {
    expect(activeEvent(rec, 500)).toBe(-1)
    expect(activeEvent(rec, 1000)).toBe(0)
    expect(activeEvent(rec, 1000 + OVERLAY_HOLD_MS)).toBe(0)
    expect(activeEvent(rec, 1001 + OVERLAY_HOLD_MS)).toBe(-1)
    expect(currentStep(rec, 3000)).toBe(0)           // the list still highlights it
  })

  it('steps forward to the next action and back like a media player', () => {
    expect(nextOffset(rec, 0)).toBe(1000)
    expect(nextOffset(rec, 1000)).toBe(5000)
    expect(nextOffset(rec, 9500)).toBe(durationOf(rec))  // past the last: the end
    expect(prevOffset(rec, 5000)).toBe(1000)             // on a step: the one before
    expect(prevOffset(rec, 7000)).toBe(5000)             // inside a step: its start
    expect(prevOffset(rec, 1000)).toBe(0)
    expect(offsetOf(rec, 3)).toBe(9000)
  })
})

describe('overlay geometry', () => {
  it('maps frame pixels to percentages of the picture box', () => {
    expect(toPercent({ x: 320, y: 200 }, { w: 640, h: 400 })).toEqual({ left: 50, top: 50 })
    expect(toPercent({ x: 1280, y: 0 }, { w: 1280, h: 800 })).toEqual({ left: 100, top: 0 })
    expect(toPercent({ x: 2000, y: -5 }, { w: 1280, h: 800 })).toEqual({ left: 100, top: 0 })  // clamped
    expect(toPercent({ x: 1, y: 1 }, { w: 0, h: 0 })).toBeNull()
  })

  it('fits the picture box inside its container at the frame ratio', () => {
    expect(containSize(1000, 1000, 16 / 10)).toEqual({ w: 1000, h: 625 })
    expect(containSize(1000, 300, 16 / 10)).toEqual({ w: 480, h: 300 })
    expect(containSize(0, 300, 1.6)).toEqual({ w: 0, h: 0 })
  })

  it('reads the scroll direction from any spelling of its delta', () => {
    expect(scrollDirection({ dy: 300 })).toBe('down')
    expect(scrollDirection({ delta_y: -10 })).toBe('up')
    expect(scrollDirection({ deltaX: -50, deltaY: 5 })).toBe('left')
    expect(scrollDirection({})).toBe('down')
    // The gateway keeps a scroll's point as x/y; that is a place, not a direction.
    expect(scrollDirection({ x: 640, y: 300 })).toBe('down')
    expect(scrollDirection({ x: 640, y: 300, dy: -200 })).toBe('up')
  })
})

describe('the in-memory fallback', () => {
  beforeEach(() => browserLiveStore.__resetForTests())
  afterEach(() => browserLiveStore.__resetForTests())

  it('replays the ring with the logged step and URL changes as markers', () => {
    browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', url: 'https://a.test/' }, 1000)
    browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', step: { index: 0, label: 'click Sign in' } }, 3000)
    browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', step: { index: 0, label: 'click Sign in' } }, 3500)  // same step: no new mark
    const turn = browserLiveStore.turn(S, 't1')!
    turn.ring.push({ header: { type: 'browser_frame', session_key: S, stream: '', seq: 1, w: 640, h: 400, mask: [] }, jpeg: new Uint8Array([1]), receivedAt: 1500 })
    const rec = recordingFromMemory(turn)
    expect(rec.source).toBe('memory')
    expect(rec.startedTs).toBe(1000)
    expect(rec.events.map(e => [e.op, e.label])).toEqual([['navigate', 'https://a.test/'], ['other', 'click Sign in']])
    expect(rec.frames).toHaveLength(1)
    expect(rec).toMatchObject({ width: 640, height: 400, live: true })
  })
})

describe('the stage store', () => {
  beforeEach(() => browserStageStore.__resetForTests())
  afterEach(() => browserStageStore.__resetForTests())

  it('opens on the panel through a host, else as a sheet', () => {
    expect(browserStageStore.open(S, { turnId: null, mode: 'live' }).surface).toBe('sheet')
    const opened: string[] = []
    const off = browserStageStore.registerHost(k => opened.push(k))
    expect(browserStageStore.open(S, { turnId: 't1', mode: 'replay' }).surface).toBe('panel')
    expect(opened).toEqual([S])
    expect(browserStageStore.open(S, { turnId: 't1', mode: 'replay', preferSheet: true }).surface).toBe('sheet')
    browserStageStore.open(S, { turnId: 't1', mode: 'replay' })
    off()  // the page went away: its panel stage goes with it
    expect(browserStageStore.get(S)).toBeNull()
  })

  it('each open bumps the nonce, so a second Replay restarts', () => {
    const a = browserStageStore.open(S, { turnId: 't1', mode: 'replay' }).nonce
    const b = browserStageStore.open(S, { turnId: 't1', mode: 'replay' }).nonce
    expect(b).toBeGreaterThan(a)
    browserStageStore.update(S, { mode: 'live' })
    expect(browserStageStore.get(S)?.nonce).toBe(b)
    browserStageStore.close(S)
    expect(browserStageStore.get(S)).toBeNull()
  })

  it('a follow-now stage shows the current turn', () => {
    const stage = browserStageStore.open(S, { turnId: null, mode: 'live' })
    expect(stageTurnId(stage, 't9')).toBe('t9')
    expect(stageTurnId({ ...stage, turnId: 't1' }, 't9')).toBe('t1')
    expect(stageTurnId(null, 't9')).toBeNull()
  })
})

describe('controls', () => {
  it('maps keys to replay actions', () => {
    expect(replayKeyAction(' ')).toBe('toggle')
    expect(replayKeyAction('ArrowRight')).toBe('next')
    expect(replayKeyAction('ArrowLeft')).toBe('prev')
    expect(replayKeyAction('Home')).toBe('start')
    expect(replayKeyAction('a')).toBeNull()
  })

  it('prints m:ss', () => {
    expect(clockLabel(0)).toBe('0:00')
    expect(clockLabel(65_400)).toBe('1:05')
  })

  it('merges the gateway turn list with this page’s turns, oldest first', () => {
    const rows = turnChoices(
      [{ turnId: 'a', startedTs: 1, endedTs: 2, live: false, frameCount: 1, eventCount: 1, startUrl: 'https://old.test/' }],
      [],
    )
    expect(rows).toEqual([{ turnId: 'a', startedTs: 1, host: 'old.test' }])
    expect(turnChoices(null, [])).toEqual([])
  })
})

describe('the frame an action is drawn on', () => {
  // The click lands at 1000; the browser scrolled its target into view first, so
  // the frame at 980 shows the old scroll and the one at 1017 the page it hit.
  const rec = readRecording({
    turn_id: 't', started_ts: 0, ended_ts: 5000, width: 100, height: 50,
    frames: [{ i: 0, ts: 0, w: 100, h: 50 }, { i: 1, ts: 980, w: 100, h: 50 }, { i: 2, ts: 1017, w: 100, h: 50 }, { i: 3, ts: 4200, w: 100, h: 50 }],
    events: [{ ts: 1000, op: 'click', point: { x: 5, y: 5 } }, { ts: 3000, op: 'click', point: { x: 5, y: 5 } }],
  }, 's')!

  it('shows the first frame after the action while its mark is up', () => {
    expect(frameForAction(rec, 1000, 0)).toBe(2)
    expect(frameForAction(rec, 1500, 0)).toBe(2)
  })

  it('keeps the play-head frame when the next frame is too far after the action', () => {
    expect(4200 - 3000).toBeGreaterThan(ACTION_FRAME_LEAD_MS)
    expect(frameForAction(rec, 3000, 1)).toBe(2)
  })

  it('never goes back to an earlier frame', () => {
    expect(frameForAction(rec, 4300, 1)).toBe(3)
  })
})


describe('which recording the view shows', () => {
  const rec = (frames: number, events: number): Recording => ({
    turnId: 't', startedTs: 0, endedTs: 1, live: false, width: 10, height: 10, source: 'server',
    frames: Array.from({ length: frames }, (_, i) => ({ i, ts: i, w: 10, h: 10, src: `f${i}` })),
    events: Array.from({ length: events }, (_, i) => ({ id: `e${i}`, ts: i, op: 'click', label: '', args: {}, point: null, path: null, url: '', status: 'ok', actor: 'agent', loop: false })),
  }) as Recording
  it('the gateway with frames, else the card with frames, else the gateway\'s steps alone', () => {
    const server = rec(2, 1), memory = rec(3, 0), stepsOnly = rec(0, 2)
    expect(pickRecording(server, memory, false).recording).toBe(server)
    expect(pickRecording(stepsOnly, memory, false).recording).toBe(memory)
    expect(pickRecording(stepsOnly, null, false).recording).toBe(stepsOnly)
    expect(pickRecording(null, null, true)).toEqual({ recording: null, loading: true })
    expect(pickRecording(rec(0, 0), null, false).recording).toBeNull()
  })
})
