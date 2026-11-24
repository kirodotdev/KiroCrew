import { describe, expect, it } from 'vitest'
import type { V3 } from './hand'
import { POINT, PointDetector, TalkSession, chooseBot, endsWith, fingerShape, wholeSentence, type FingerShape, type HandSample, type TalkEvent } from './pointTalk'

const POINTING: FingerShape = { index: 0.98, others: 0.45 }
const OPEN: FingerShape = { index: 0.97, others: 0.95 }
const FIST: FingerShape = { index: 0.4, others: 0.4 }

/** A hand sample every 16 ms. `yawDegPerS` turns the ray, `mPerS` moves the hand. */
function feedFor(d: PointDetector, ms: number, shape: FingerShape | null, o: { yawDegPerS?: number; mPerS?: number; pinching?: boolean; from?: number } = {}): boolean {
  let last = false
  for (let t = o.from ?? 0; t <= (o.from ?? 0) + ms; t += 16) {
    const yaw = ((o.yawDegPerS ?? 0) * t) / 1000 * (Math.PI / 180)
    const s: HandSample = { t, origin: [((o.mPerS ?? 0) * t) / 1000, 1.2, -0.2], dir: [Math.sin(yaw), 0, -Math.cos(yaw)], shape, pinching: !!o.pinching }
    last = d.feed(s)
  }
  return last
}

describe('a pointing finger, or a hand that is just moving', () => {
  it('reads a hand shape from its joints: how straight the index is against the other three', () => {
    const finger = (name: string, curl: number): Record<string, V3> => {
      // Three bones of 3 cm, each bent `curl` radians more than the one before.
      const pts: V3[] = [[0, 0, 0]]
      let a = 0
      for (let i = 0; i < 3; i += 1) { a += curl; const p = pts[pts.length - 1]; pts.push([p[0], p[1] - Math.sin(a) * 0.03, p[2] - Math.cos(a) * 0.03]) }
      return Object.fromEntries(['phalanx-proximal', 'phalanx-intermediate', 'phalanx-distal', 'tip'].map((n, i) => [`${name}-finger-${n}`, pts[i]]))
    }
    const hand = { ...finger('index', 0.02), ...finger('middle', 0.9), ...finger('ring', 0.9), ...finger('pinky', 0.9) }
    const shape = fingerShape(hand)!
    expect(shape.index).toBeGreaterThan(0.99)
    expect(shape.others).toBeLessThan(0.75)
    expect(fingerShape({ ...hand, 'ring-finger-tip': null })).toBeNull()
  })

  it('counts a still hand with the index held out as pointing', () => {
    expect(feedFor(new PointDetector(), 400, POINTING)).toBe(true)
  })

  it('does not count an open hand, a fist or a pinch, however still', () => {
    expect(feedFor(new PointDetector(), 400, OPEN)).toBe(false)
    expect(feedFor(new PointDetector(), 400, FIST)).toBe(false)
    expect(feedFor(new PointDetector(), 400, POINTING, { pinching: true })).toBe(false)
  })

  it('does not count a pointing shape that sweeps or travels: that is a hand in motion', () => {
    expect(feedFor(new PointDetector(), 400, POINTING, { yawDegPerS: 120 })).toBe(false)
    expect(feedFor(new PointDetector(), 400, POINTING, { mPerS: 0.9 })).toBe(false)
    // A slow drift, as a held arm has, still points.
    expect(feedFor(new PointDetector(), 400, POINTING, { yawDegPerS: 8, mPerS: 0.05 })).toBe(true)
  })

  it('stops pointing when the hand speeds up, and points again once it has come to rest', () => {
    const d = new PointDetector()
    expect(feedFor(d, 300, POINTING)).toBe(true)
    expect(feedFor(d, 300, POINTING, { yawDegPerS: 200, from: 316 })).toBe(false)
    let last = false
    for (let t = 640; t <= 1200; t += 16) last = d.feed({ t, origin: [0, 1.2, -0.2], dir: [0, 0, -1], shape: POINTING, pinching: false })
    expect(last).toBe(true)
  })

  it('is not pointing when the hand is not tracked, and a source with no fingers needs only to be still', () => {
    const d = new PointDetector()
    expect(d.feed(null)).toBe(false)
    expect(feedFor(new PointDetector(), 200, null)).toBe(true)
    expect(feedFor(new PointDetector(), 400, null, { yawDegPerS: 150 })).toBe(false)
  })
})

describe('which Bot is meant', () => {
  it('takes the Bot the ray is on, and nobody when it is on none', () => {
    expect(chooseBot([{ id: 'a', deg: 1, radius: 4 }, { id: 'b', deg: 12, radius: 4 }], '')).toEqual({ id: 'a', ambiguous: [] })
    expect(chooseBot([{ id: 'a', deg: 9, radius: 4 }], '')).toEqual({ id: '', ambiguous: [] })
  })

  it('can hit a Bot that is drawn tiny, as in the miniature', () => {
    expect(chooseBot([{ id: 'a', deg: 2.2, radius: 0.6 }], '').id).toBe('a')
    expect(chooseBot([{ id: 'a', deg: POINT.PICK_DEG + 0.3, radius: 0.6 }], '').id).toBe('')
  })

  it('refuses to guess between two Bots that are too close to call, and names both', () => {
    const two = [{ id: 'a', deg: 1.4, radius: 1 }, { id: 'b', deg: 2.2, radius: 1 }]
    expect(chooseBot(two, '')).toEqual({ id: '', ambiguous: ['a', 'b'] })
    // Clearly nearer to one of them: that one.
    expect(chooseBot([{ id: 'a', deg: 0.3, radius: 1 }, { id: 'b', deg: 2.3, radius: 1 }], '').id).toBe('a')
  })

  it('lets the Bot that listens keep the turn while the ray is still on it, even when its neighbour is nearer', () => {
    const two = [{ id: 'a', deg: 2.2, radius: 1 }, { id: 'b', deg: 1.4, radius: 1 }]
    expect(chooseBot(two, 'a').id).toBe('a')
    // A little outside its edge is still its turn; clearly off it is not.
    expect(chooseBot([{ id: 'a', deg: POINT.PICK_DEG + 1.5, radius: 1 }], 'a').id).toBe('a')
    expect(chooseBot([{ id: 'a', deg: POINT.PICK_DEG + POINT.EXIT_DEG + 0.5, radius: 1 }, { id: 'b', deg: 0.5, radius: 1 }], 'a').id).toBe('b')
  })
})

describe('one sentence to one Bot', () => {
  /** Run the session with the finger on `bot` from `from` to `to`, and collect what happened. */
  const run = (s: TalkSession, from: number, to: number, bot: string, o: { pointing?: boolean; tracked?: boolean } = {}): TalkEvent[] => {
    const out: TalkEvent[] = []
    for (let now = from; now <= to; now += 16) {
      const e = s.point({ now, pointing: o.pointing ?? true, bot })
      if (e) out.push(e)
    }
    return out
  }
  const listening = () => { const s = new TalkSession(); run(s, 0, POINT.REST_MS + 32, 'juno'); return s }

  it('notices at once, and listens only after the finger has rested', () => {
    const s = new TalkSession()
    expect(run(s, 0, 100, 'juno').map(e => e.type)).toEqual(['notice'])
    expect(s.phase).toBe('noticed')
    expect(run(s, 116, POINT.REST_MS + 32, 'juno').map(e => e.type)).toEqual(['listen'])
    expect(s.phase).toBe('listening')
  })

  it('does not listen to a finger that only passes over a Bot', () => {
    const s = new TalkSession()
    run(s, 0, 150, 'juno')
    expect(run(s, 166, 400, '').map(e => e.type)).toEqual(['cancel'])
    expect(s.phase).toBe('idle')
  })

  it('sends on a pause after the last word', () => {
    const s = listening()
    s.heard('Start', 500)
    s.heard('Start the rollout', 900)
    const ev = run(s, 916, 900 + POINT.PAUSE_OPEN_MS + 32, 'juno')
    expect(ev).toEqual([{ type: 'send', bot: 'juno', text: 'Start the rollout', why: 'pause' }])
  })

  it('ends a whole sentence sooner than one that is still open', () => {
    const whole = listening()
    whole.heard('Start the rollout.', 900)
    expect(run(whole, 916, 900 + POINT.PAUSE_MS - 32, 'juno')).toEqual([])
    expect(run(whole, 900 + POINT.PAUSE_MS - 16, 900 + POINT.PAUSE_MS + 32, 'juno')[0]).toMatchObject({ type: 'send', why: 'pause', text: 'Start the rollout.' })
    // "Start the" is not a sentence yet: the person may be thinking. It gets the longer wait.
    const open = listening()
    open.heard('Start the', 900)
    expect(run(open, 916, 900 + POINT.PAUSE_OPEN_MS - 32, 'juno')).toEqual([])
    expect(run(open, 900 + POINT.PAUSE_OPEN_MS - 16, 900 + POINT.PAUSE_OPEN_MS + 32, 'juno')[0]).toMatchObject({ type: 'send', why: 'pause' })
    expect(POINT.PAUSE_MS).toBe(600)
    expect(POINT.PAUSE_OPEN_MS).toBeGreaterThan(POINT.PAUSE_MS)
    for (const t of ['Ship it.', 'Is it done?', 'Go!', '出荷して。', 'हो गया।']) expect(wholeSentence(t), t).toBe(true)
    for (const t of ['Ship it', 'Start the', '', 'v1.2']) expect(wholeSentence(t), t).toBe(false)
  })

  it('does not send while words keep coming', () => {
    const s = listening()
    let now = 500
    for (let i = 1; i <= 8; i += 1) { s.heard('word '.repeat(i).trim(), now); expect(run(s, now, now + 600, 'juno')).toEqual([]); now += 616 }
    expect(s.phase).toBe('hearing')
  })

  it('does not end the sentence when the hand drops, goes back to work, or is lost from sight: only the pause does', () => {
    for (const o of [{ pointing: false }, { pointing: false, tracked: false }]) {
      const s = listening()
      s.heard('Go', 500)
      expect(run(s, 516, 1000, '', o)).toEqual([])
      s.heard('Go ahead with it', 1000)
      expect(run(s, 1016, 1000 + POINT.PAUSE_OPEN_MS - 32, '', o)).toEqual([])
      expect(run(s, 1000 + POINT.PAUSE_OPEN_MS - 16, 1000 + POINT.PAUSE_OPEN_MS + 32, '', o).map(e => [e.type, e.why, e.text])).toEqual([['send', 'pause', 'Go ahead with it']])
    }
  })

  it('keeps the same listener when the finger points at another Bot in the middle of a sentence', () => {
    const s = listening()
    s.heard('Start the', 500)
    expect(run(s, 516, 900, 'mira')).toEqual([])
    expect(s.bot).toBe('juno')
    s.heard('Start the rollout', 900)
    expect(run(s, 916, 900 + POINT.PAUSE_OPEN_MS + 32, 'mira')[0]).toEqual({ type: 'send', bot: 'juno', text: 'Start the rollout', why: 'pause' })
  })

  it('makes the Bot the listener with the first word, before the finger has rested', () => {
    const s = new TalkSession()
    expect(run(s, 0, 64, 'juno').map(e => e.type)).toEqual(['notice'])
    s.heard('Juno', 80)
    expect(run(s, 96, 112, 'juno').map(e => e.type)).toEqual(['listen'])
    expect(s.phase).toBe('hearing')
    // The hand has left already. The words still go to Juno.
    s.heard('Juno ship it', 400)
    expect(run(s, 416, 400 + POINT.PAUSE_OPEN_MS + 32, '', { pointing: false }).map(e => [e.type, e.bot, e.text])).toEqual([['send', 'juno', 'Juno ship it']])
  })

  it('drops the sentence on "never mind", words and all', () => {
    const s = listening()
    s.heard('Start the rollout never mind', 500)
    expect(endsWith(s.text, 'never mind|forget it')).toBe(true)
    expect(s.drop()).toEqual({ type: 'cancel', bot: 'juno', text: 'Start the rollout never mind', why: 'never-mind' })
    expect(s.phase).toBe('idle')
    expect(new TalkSession().drop()).toBeNull()
  })

  it('knows a closing phrase only as whole words at the end of what was said', () => {
    expect(endsWith('Trim the cache. Never mind!', 'never mind|forget it')).toBe(true)
    expect(endsWith('oh forget it', 'never mind|forget it')).toBe(true)
    expect(endsWith('never mind the gap, ship it', 'never mind|forget it')).toBe(false)
    expect(endsWith('he said nevermind', 'never mind')).toBe(false)
    expect(endsWith('', 'never mind')).toBe(false)
    expect(endsWith('anything', '')).toBe(false)
    expect(endsWith('もういい', 'もういい|やめて')).toBe(true)
  })

  it('sends when the recogniser says the sentence is complete, and only when there are words', () => {
    const s = listening()
    expect(s.endpoint()).toBeNull()
    s.heard('Ship it', 500)
    expect(s.endpoint()).toEqual({ type: 'send', bot: 'juno', text: 'Ship it', why: 'endpoint' })
  })

  it('gives up listening after a while with no word, wherever the hand is', () => {
    const s = listening()
    expect(run(s, 400, 400 + POINT.WAIT_MS + 64, 'juno').map(e => [e.type, e.why])).toEqual([['cancel', 'silence']])
    const t = listening()
    expect(run(t, 400, 3000, '', { pointing: false })).toEqual([])
    expect(run(t, 3016, 400 + POINT.WAIT_MS + 64, '').map(e => [e.type, e.why])).toEqual([['cancel', 'silence']])
  })

  it('is one sentence for one point: the finger must leave before the same Bot listens again', () => {
    const s = listening()
    s.heard('Thanks', 500)
    run(s, 516, 500 + POINT.PAUSE_OPEN_MS + 32, 'juno')
    expect(run(s, 1800, 3000, 'juno')).toEqual([])
    run(s, 3016, 3100, '')
    expect(run(s, 3116, 3116 + POINT.REST_MS + 32, 'juno').map(e => e.type)).toEqual(['notice', 'listen'])
  })

  it('ignores words when no Bot is listening, and hands kept words back on stop', () => {
    const s = new TalkSession()
    s.heard('hello', 10)
    expect(s.text).toBe('')
    const t = listening()
    t.heard('half a', 500)
    expect(t.stop()).toMatchObject({ type: 'keep', bot: 'juno', text: 'half a' })
    expect(new TalkSession().stop()).toBeNull()
  })
})
