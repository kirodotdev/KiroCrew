import { describe, expect, it } from 'vitest'
import { CARD, STEADY_MS, aimTarget, angleDeg, cardLayer, cardLines, steadyAim, cardStep, flyShare, joinWords, keysShown, landing, listens, markStep, newCard, ownUp, showsLang, type BotAt, type Card, type CardEvent, type CardTarget } from './chatCard'
import { BAND, DRAW } from './drawOrder'
import { chatSessionPath, livePath } from './streamPath'

const run = (card: Card, ...events: CardEvent[]): Card => events.reduce(cardStep, card)
const EYE = [0, 1.6, 0] as const
const HOME = [-0.24, 1.5, -0.6] as const
const JUNO: BotAt = { slug: 'juno', at: [0.2, 1.1, -1.4], radiusM: 0.15 }
const MIRA: BotAt = { slug: 'mira', at: [-0.2, 1.1, -1.4], radiusM: 0.15 }
const toward = (p: readonly [number, number, number]) => [p[0] - EYE[0], p[1] - EYE[1], p[2] - EYE[2]] as const

describe('the new chat card', () => {
  it('comes out listening where there is speech, and with its keyboard where there is none', () => {
    const heard = newCard('a', true)
    expect(listens(heard)).toBe(true)
    // M106: until its microphone listens it has its keys; they go while it listens, and only then.
    expect(keysShown(heard)).toBe(true)
    expect(keysShown(cardStep(heard, { type: 'mic', state: 'on' }))).toBe(false)
    expect(keysShown(cardStep(cardStep(heard, { type: 'mic', state: 'on' }), { type: 'mic', state: 'off' }))).toBe(true)
    const mute = newCard('b', false)
    expect(listens(mute)).toBe(false)
    expect(keysShown(mute)).toBe(true)
    expect(listens(null)).toBe(false)
    expect(keysShown(null)).toBe(false)
  })

  it('shows what is heard, and keeps the words still worked on apart', () => {
    const card = run(newCard('a', true), { type: 'mic', state: 'on' }, { type: 'heard', final: '', interim: 'ship the' }, { type: 'heard', final: 'ship the fix', interim: '' }, { type: 'heard', final: '', interim: 'today' })
    expect(card).toMatchObject({ mic: 'on', text: 'ship the fix', interim: 'today' })
  })

  it('thrown with words, flies, lands, and is sent once', () => {
    let card = run(newCard('a', true), { type: 'heard', final: 'hello', interim: '' }, { type: 'grab' }, { type: 'release', target: { to: 'bot', slug: 'juno' } })
    expect(card).toMatchObject({ phase: 'flying', target: { to: 'bot', slug: 'juno' } })
    expect(listens(card)).toBe(false)
    card = cardStep(card, { type: 'landed' })
    expect(card.phase).toBe('sending')
    // What comes after the throw changes nothing: a second throw, a key, a second landing.
    for (const e of [{ type: 'grab' }, { type: 'release', target: { to: 'open' } }, { type: 'key', key: 'Enter' }, { type: 'landed' }, { type: 'heard', final: 'more', interim: '' }] as CardEvent[]) expect(cardStep(card, e)).toBe(card)
    card = cardStep(card, { type: 'result', ok: true })
    expect(card.phase).toBe('sent')
    expect(cardStep(card, { type: 'result', ok: true })).toBe(card)
  })

  it('takes the words still worked on with it at the throw', () => {
    const card = run(newCard('a', true), { type: 'heard', final: 'open the', interim: 'door' }, { type: 'grab' }, { type: 'release', target: { to: 'open' } })
    expect(card).toMatchObject({ phase: 'flying', text: 'open the door', interim: '' })
  })

  it('thrown with no words, is not sent: the keyboard comes and the card keeps its target', () => {
    let card = run(newCard('a', true), { type: 'grab' }, { type: 'release', target: { to: 'bot', slug: 'mira' } })
    expect(card).toMatchObject({ phase: 'typing', target: { to: 'bot', slug: 'mira' } })
    expect(keysShown(card)).toBe(true)
    // Enter on an empty card, or on blanks alone, sends nothing.
    expect(cardStep(card, { type: 'key', key: 'Enter' })).toBe(card)
    expect(run(card, { type: 'key', key: ' ' }, { type: 'key', key: 'Enter' }).phase).toBe('typing')
    card = run(card, { type: 'key', key: 'h' }, { type: 'key', key: 'i' }, { type: 'key', key: '!' }, { type: 'key', key: 'Backspace' })
    expect(card.text).toBe('hi')
    card = cardStep(card, { type: 'key', key: 'Enter' })
    expect(card.phase).toBe('flying')
    expect(cardStep(card, { type: 'key', key: 'Enter' })).toBe(card)
  })

  it('never sends an empty card, whatever brought it to its target', () => {
    const card: Card = { ...newCard('a', true), phase: 'flying', target: { to: 'open' }, text: '  ' }
    expect(cardStep(card, { type: 'landed' }).phase).toBe('typing')
  })

  it('let go on its own place, or with its hand lost, stays where it was taken', () => {
    const out = run(newCard('a', true), { type: 'grab' })
    expect(out.phase).toBe('held')
    expect(cardStep(out, { type: 'release', target: null }).phase).toBe('out')
    expect(cardStep(out, { type: 'drop' }).phase).toBe('out')
    const typing = run(newCard('a', true), { type: 'grab' }, { type: 'release', target: { to: 'open' } }, { type: 'grab' })
    expect(typing).toMatchObject({ phase: 'held', from: 'typing' })
    expect(cardStep(typing, { type: 'drop' })).toMatchObject({ phase: 'typing', target: { to: 'open' } })
  })

  describe('whose send failed', () => {
    const failed = () => run(newCard('a', true, 'en-US'), { type: 'mic', state: 'on' }, { type: 'heard', final: 'hello', interim: '' }, { type: 'grab' }, { type: 'release', target: { to: 'bot', slug: 'mira' } }, { type: 'landed' }, { type: 'result', ok: false })

    it('comes back frozen: its words, its target and its id as they were', () => {
      expect(failed()).toMatchObject({ phase: 'out', failed: true, text: 'hello', target: { to: 'bot', slug: 'mira' }, id: 'a', mic: 'off' })
    })

    it('does not listen, shows no keyboard and no language, and takes no new words', () => {
      const card = failed()
      expect(listens(card)).toBe(false)
      expect(keysShown(card)).toBe(false)
      expect(showsLang(card)).toBe(false)
      for (const e of [{ type: 'heard', final: 'more', interim: 'words' }, { type: 'key', key: 'x' }, { type: 'key', key: 'Backspace' }, { type: 'key', key: 'Enter' }, { type: 'mic', state: 'on' }, { type: 'micPress' }, { type: 'quiet' }, { type: 'lang', lang: 'zh-CN' }] as CardEvent[]) expect(cardStep(card, e)).toBe(card)
      // The same where the browser has no recognizer: its keyboard does not come back for a frozen card.
      const mute = run(newCard('b', false), { type: 'key', key: 'o' }, { type: 'grab' }, { type: 'release', target: { to: 'open' } }, { type: 'landed' }, { type: 'result', ok: false })
      expect(keysShown(mute)).toBe(false)
      expect(cardStep(mute, { type: 'key', key: 'x' })).toBe(mute)
    })

    it('thrown anywhere, or pressed where it stands, goes to its own target again with the same words', () => {
      for (const target of [{ to: 'open' }, { to: 'bot', slug: 'juno' }, null] as const) {
        const card = run(failed(), { type: 'grab' }, { type: 'release', target })
        expect(card).toMatchObject({ phase: 'flying', failed: false, text: 'hello', target: { to: 'bot', slug: 'mira' }, id: 'a' })
      }
    })

    it('is not listening while it is held, and a lost hand leaves it frozen', () => {
      const held = cardStep(failed(), { type: 'grab' })
      expect(held.phase).toBe('held')
      expect(listens(held)).toBe(false)
      expect(cardStep(held, { type: 'heard', final: 'more', interim: '' })).toBe(held)
      expect(cardStep(held, { type: 'drop' })).toMatchObject({ phase: 'out', failed: true, text: 'hello' })
    })

    it('that fails again comes back frozen again, and one that goes through is sent', () => {
      const again = run(failed(), { type: 'grab' }, { type: 'release', target: null }, { type: 'landed' })
      expect(again.phase).toBe('sending')
      expect(cardStep(again, { type: 'result', ok: false })).toMatchObject({ phase: 'out', failed: true, target: { to: 'bot', slug: 'mira' } })
      expect(cardStep(again, { type: 'result', ok: true }).phase).toBe('sent')
    })
  })

  it('types on the card at once where there is no speech, and a throw sends it', () => {
    let card = run(newCard('a', false), { type: 'key', key: 'o' }, { type: 'key', key: 'k' })
    expect(card.text).toBe('ok')
    // No target yet: Enter sends nothing, the throw does.
    expect(cardStep(card, { type: 'key', key: 'Enter' })).toBe(card)
    card = run(card, { type: 'grab' }, { type: 'release', target: { to: 'open' } })
    expect(card.phase).toBe('flying')
  })

  it('takes no key while its keyboard does not show, and holds no more than its most', () => {
    // While its microphone listens the keys are away (D14), and a key does nothing.
    const card = cardStep(newCard('a', true), { type: 'mic', state: 'on' })
    expect(cardStep(card, { type: 'key', key: 'x' })).toBe(card)
    // Before it listens the keys show (M106), and a key is taken.
    expect(cardStep(newCard('a', true), { type: 'key', key: 'x' }).text).toBe('x')
    const full = run(newCard('a', false), ...Array.from({ length: CARD.mostChars + 5 }, () => ({ type: 'key', key: 'x' }) as CardEvent))
    expect(full.text.length).toBe(CARD.mostChars)
  })

  it('a recognizer that gives up turns the keyboard on, and stays given up', () => {
    const card = run(newCard('a', true), { type: 'mic', state: 'on' }, { type: 'mic', state: 'none' })
    expect(keysShown(card)).toBe(true)
    expect(cardStep(card, { type: 'mic', state: 'on' })).toBe(card)
  })
})

describe('the card’s microphone and language', () => {
  const on = () => run(newCard('a', true, 'en-US'), { type: 'mic', state: 'on' }, { type: 'heard', final: 'ship the', interim: 'fix' })

  it('goes off after its quiet time and keeps its words, with the ones still worked on', () => {
    const card = cardStep(on(), { type: 'quiet' })
    expect(card).toMatchObject({ paused: true, mic: 'off', text: 'ship the fix', interim: '', phase: 'out' })
    expect(listens(card)).toBe(false)
    // M106: its microphone does not listen, so its keys show.
    expect(keysShown(card)).toBe(true)
    expect(showsLang(card)).toBe(true)
  })

  it('stays off until a pinch on its mark, whatever the recognizer says meanwhile', () => {
    const off = cardStep(on(), { type: 'quiet' })
    expect(cardStep(off, { type: 'mic', state: 'on' })).toBe(off)
    expect(cardStep(off, { type: 'quiet' })).toBe(off)
    const again = cardStep(off, { type: 'micPress' })
    expect(again.paused).toBe(false)
    expect(listens(again)).toBe(true)
    expect(again.text).toBe('ship the fix')
  })

  it('a pinch on the mark turns a listening microphone off', () => {
    const off = cardStep(on(), { type: 'micPress' })
    expect(off).toMatchObject({ paused: true, mic: 'off', text: 'ship the fix' })
  })

  it('a card that is off is still thrown and sent, and is off while it is held', () => {
    const held = run(on(), { type: 'quiet' }, { type: 'grab' })
    expect(listens(held)).toBe(false)
    expect(cardStep(held, { type: 'release', target: { to: 'open' } }).phase).toBe('flying')
  })

  it('has no microphone to turn on where there is no speech, and none after the throw', () => {
    const mute = newCard('a', false, 'en-US')
    expect(cardStep(mute, { type: 'micPress' })).toBe(mute)
    expect(cardStep(mute, { type: 'quiet' })).toBe(mute)
    expect(showsLang(mute)).toBe(false)
    const typing = run(newCard('a', true, 'en-US'), { type: 'grab' }, { type: 'release', target: { to: 'open' } })
    expect(cardStep(typing, { type: 'micPress' })).toBe(typing)
    expect(cardStep(typing, { type: 'quiet' })).toBe(typing)
    expect(showsLang(typing)).toBe(false)
  })

  it('acts on a mark only at the up of that mark\'s own down', () => {
    expect(ownUp({ name: 'mic', pointerId: 1 }, 'mic', 1)).toBe(true)
    // No down on it (a press dragged in from empty space), another mark's down, another pointer's down.
    expect(ownUp(null, 'mic', 1)).toBe(false)
    expect(ownUp({ name: 'lang', pointerId: 1 }, 'mic', 1)).toBe(false)
    expect(ownUp({ name: 'mic', pointerId: 2 }, 'mic', 1)).toBe(false)
  })

  it('drops a mark\'s down when the press is cancelled or leaves, so that no later up acts for it', () => {
    const down = markStep(null, { type: 'down', name: 'mic', pointerId: 1 })
    expect(down).toEqual({ down: { name: 'mic', pointerId: 1 }, acts: false })
    // A plain press acts once: the up drops the down, and a second up finds none.
    const up = markStep(down.down, { type: 'up', name: 'mic', pointerId: 1 })
    expect(up).toEqual({ down: null, acts: true })
    expect(markStep(up.down, { type: 'up', name: 'mic', pointerId: 1 }).acts).toBe(false)
    // A long press that ends as a cancel, or a lost hand: the same pointer's later up on the mark does nothing.
    const cancelled = markStep(down.down, { type: 'cancel', name: 'mic', pointerId: 1 })
    expect(cancelled).toEqual({ down: null, acts: false })
    expect(markStep(cancelled.down, { type: 'up', name: 'mic', pointerId: 1 }).acts).toBe(false)
    // A leave of the mark drops its down; a leave of another mark does not.
    expect(markStep(down.down, { type: 'leave', name: 'mic', pointerId: 1 }).down).toBeNull()
    expect(markStep(down.down, { type: 'leave', name: 'lang', pointerId: 1 }).down).toEqual(down.down)
  })

  it('does not take a late result of the language it has left', () => {
    // The word still worked on joined the card at the switch; the old recognizer's last result for it comes after.
    const switched = cardStep(on(), { type: 'lang', lang: 'zh-CN' })
    expect(switched.text).toBe('ship the fix')
    expect(cardStep(switched, { type: 'heard', final: 'fix', interim: '', lang: 'en-US' })).toBe(switched)
    expect(cardStep(switched, { type: 'heard', final: '', interim: 'fix it', lang: 'en-US' })).toBe(switched)
    // A result in the card's own language is taken as before.
    expect(cardStep(switched, { type: 'heard', final: '今天', interim: '', lang: 'zh-CN' }).text).toBe('ship the fix今天')
  })

  it('takes another language and keeps its words, with the word still worked on', () => {
    expect(on()).toMatchObject({ text: 'ship the', interim: 'fix' })
    const card = cardStep(on(), { type: 'lang', lang: 'zh-CN' })
    expect(card).toMatchObject({ lang: 'zh-CN', text: 'ship the fix', interim: '' })
    expect(cardStep(card, { type: 'lang', lang: 'zh-CN' })).toBe(card)
    expect(cardStep(card, { type: 'lang', lang: '' })).toBe(card)
  })
})

describe('one reading of the aim for the words and the act (M77)', () => {
  const mira = { to: 'bot', slug: 'mira' } as const
  const run = (frames: (readonly [number, CardTarget | null])[]) => {
    let was = { raw: null as CardTarget | null, since: 0 }
    let shown: CardTarget | null = null
    for (const [t, raw] of frames) { const s = steadyAim(raw, was, t); was = { raw: s.raw, since: s.since }; shown = s.shown }
    return shown
  }
  it('the aim rests on a Bot 200 ms: sent; 100 ms: back', () => {
    expect(run([[0, mira], [100, mira], [200, mira]])).toEqual(mira)
    expect(run([[0, mira], [100, mira]])).toBeNull()
    expect(STEADY_MS).toBe(150)
  })
  it('an aim that crosses the line in the last frames shows back, and back is what is done', () => {
    expect(run([[0, mira], [200, mira], [216, null], [232, mira]])).toBeNull()
    expect(run([[0, null], [300, null], [316, mira]])).toBeNull()
  })
})

describe('the held card is drawn over Home (D105 3)', () => {
  it('held, it is in the held band, over Home\'s strip; at its place, in flight and sent, under it again', () => {
    expect(DRAW[cardLayer('held')]).toBeGreaterThan(DRAW.home + BAND - 1)
    for (const p of ['out', 'typing', 'flying', 'sending', 'sent'] as const) expect(DRAW[cardLayer(p)]).toBeLessThan(DRAW.home)
  })
})

describe('where a throw goes', () => {
  it('nowhere while the aim is on the card’s own place', () => {
    expect(aimTarget(EYE, toward(HOME), HOME, [JUNO, MIRA])).toBeNull()
    expect(aimTarget(EYE, toward([HOME[0] + 0.05, HOME[1], HOME[2]]), HOME, [JUNO])).toBeNull()
  })

  it('to the Bot that the aim passes nearest, within reach of its body', () => {
    expect(aimTarget(EYE, toward(JUNO.at), HOME, [JUNO, MIRA])).toEqual({ to: 'bot', slug: 'juno' })
    expect(aimTarget(EYE, toward([JUNO.at[0] + 0.2, JUNO.at[1], JUNO.at[2]]), HOME, [JUNO, MIRA])).toEqual({ to: 'bot', slug: 'juno' })
    expect(aimTarget(EYE, toward([-0.12, 1.1, -1.4]), HOME, [JUNO, MIRA])).toEqual({ to: 'bot', slug: 'mira' })
  })

  it('to a Bot that stands behind the card’s place when the aim is on its body, back when it is on the place', () => {
    // The body's middle 6 degrees under the place, as a desk of the room of places stands.
    const h = toward(HOME)
    const pitch = Math.atan2(h[1], Math.hypot(h[0], h[2])) - (6 * Math.PI) / 180
    const yaw = Math.atan2(h[0], -h[2])
    const under: [number, number, number] = [EYE[0] + 1.6 * Math.sin(yaw) * Math.cos(pitch), EYE[1] + 1.6 * Math.sin(pitch), EYE[2] - 1.6 * Math.cos(yaw) * Math.cos(pitch)]
    const behind: BotAt = { slug: 'mira', at: under, radiusM: 0.11 }
    expect(angleDeg(toward(under), toward(HOME))).toBeLessThan(CARD.homeDeg)
    expect(aimTarget(EYE, toward(under), HOME, [behind])).toEqual({ to: 'bot', slug: 'mira' })
    expect(aimTarget(EYE, toward(HOME), HOME, [behind])).toBeNull()
  })

  it('near the place, a Bot takes it only when the aim is nearer the Bot\'s middle than the place\'s (D104 1)', () => {
    // A Bot 5.1 degrees from the place, its body 3.9 degrees half wide at the eye (REVIEW-41 4.1): the old rule sent
    // from 0.6 degrees on the line and 1.4 degrees aside. Now the halfway point, 2.55 degrees, decides on the line.
    const h = toward(HOME)
    const yaw0 = Math.atan2(h[0], -h[2]), pitch0 = Math.atan2(h[1], Math.hypot(h[0], h[2]))
    const at = (yawDeg: number, pitchDeg: number, m = 1.6): [number, number, number] => {
      const y = yaw0 + (yawDeg * Math.PI) / 180, p = pitch0 + (pitchDeg * Math.PI) / 180
      return [EYE[0] + m * Math.sin(y) * Math.cos(p), EYE[1] + m * Math.sin(p), EYE[2] - m * Math.cos(y) * Math.cos(p)]
    }
    const near: BotAt = { slug: 'juno', at: at(5.1, 0), radiusM: 1.6 * Math.tan((3.9 * Math.PI) / 180) }
    const aim = (yawDeg: number, pitchDeg = 0) => aimTarget(EYE, toward(at(yawDeg, pitchDeg)), HOME, [near])
    expect(aim(0)).toBeNull()
    expect(aim(1)).toBeNull()
    expect(aim(2)).toBeNull()
    expect(aim(3)).toEqual({ to: 'bot', slug: 'juno' })
    expect(aim(4)).toEqual({ to: 'bot', slug: 'juno' })
    expect(aim(0, 1.4)).toBeNull()
    expect(aim(0, -1.4)).toBeNull()
    // The other side: a throw at its middle always sends, for this Bot and for one as wide as its distance.
    expect(aim(5.1)).toEqual({ to: 'bot', slug: 'juno' })
    const wide: BotAt = { ...near, radiusM: 1.6 * Math.tan((5.5 * Math.PI) / 180) }
    expect(aimTarget(EYE, toward(near.at), HOME, [wide])).toEqual({ to: 'bot', slug: 'juno' })
    expect(aimTarget(EYE, toward(HOME), HOME, [wide])).toBeNull()
  })

  it('near the place with two Bots, the one whose middle the aim is nearest takes it', () => {
    const h = toward(HOME)
    const yaw0 = Math.atan2(h[0], -h[2]), pitch0 = Math.atan2(h[1], Math.hypot(h[0], h[2]))
    const at = (yawDeg: number): [number, number, number] => { const y = yaw0 + (yawDeg * Math.PI) / 180; return [EYE[0] + 1.6 * Math.sin(y) * Math.cos(pitch0), EYE[1] + 1.6 * Math.sin(pitch0), EYE[2] - 1.6 * Math.cos(y) * Math.cos(pitch0)] }
    const left: BotAt = { slug: 'left', at: at(-5), radiusM: 0.1 }
    const right: BotAt = { slug: 'right', at: at(5), radiusM: 0.1 }
    expect(aimTarget(EYE, toward(at(4)), HOME, [left, right])).toEqual({ to: 'bot', slug: 'right' })
    expect(aimTarget(EYE, toward(at(-4)), HOME, [left, right])).toEqual({ to: 'bot', slug: 'left' })
    expect(aimTarget(EYE, toward(at(1)), HOME, [left, right])).toBeNull()
  })

  it('into the open past every Bot', () => {
    expect(aimTarget(EYE, [0, 0.3, -1], HOME, [JUNO, MIRA])).toEqual({ to: 'open' })
    expect(aimTarget(EYE, [1, 0, -0.2], HOME, [])).toEqual({ to: 'open' })
  })

  it('lands on its Bot, or ahead along the aim', () => {
    expect(landing(EYE, [0, 0, -1], { to: 'bot', slug: 'juno' }, [JUNO])).toEqual([...JUNO.at])
    expect(landing(EYE, [0, 0, -2], { to: 'open' }, [JUNO])).toEqual([0, 1.6, -CARD.openM])
    // A Bot that left: the card lands in the open.
    expect(landing(EYE, [0, 0, -1], { to: 'bot', slug: 'gone' }, [])).toEqual([0, 1.6, -CARD.openM])
  })

  it('flies slowly out of the hand and fast at the end', () => {
    expect(flyShare(0)).toBe(0)
    expect(flyShare(1)).toBe(1)
    expect(flyShare(0.5)).toBeLessThan(0.5)
    expect(flyShare(2)).toBe(1)
    expect(angleDeg([0, 0, -1], [1, 0, 0])).toBeCloseTo(90)
  })
})

describe('the page of the chat that a card started', () => {
  it('names its session, in the dashboard’s own spelling, and opens on a panel', () => {
    expect(chatSessionPath('chat-260-1795144489')).toBe('/chat?sid=chat-260-1795144489')
    expect(livePath('/chat?sid=member-mira.memory-member-mira-2a90')).toBe('/chat?sid=member-mira.memory-member-mira-2a90')
  })

  it('takes one key of a key’s own characters, and nothing after it', () => {
    for (const key of ['', 'a b', 'a&token=1', 'a#x', 'a/b', 'a%20b', 'x'.repeat(129)]) expect(chatSessionPath(key), key).toBeNull()
    for (const path of ['/chat?sid=a&autoSend=1', '/chat?sid=a&sid=b', '/chat?sid=', '/chat?token=x', '/chat?slot=a', '/chat?sid=a#b']) expect(livePath(path), path).toBeNull()
  })
})

describe('the card’s words', () => {
  it('joins runs with a space, and with none in a script that uses none', () => {
    expect(joinWords('ship the', ' fix ')).toBe('ship the fix')
    expect(joinWords('', 'fix')).toBe('fix')
    expect(joinWords('fix', '')).toBe('fix')
    expect(joinWords('今天', '发布')).toBe('今天发布')
    expect(joinWords('发布', 'v2')).toBe('发布v2')
  })

  it('wraps at words, cuts a run with no spaces by signs, and shows the last lines', () => {
    expect(cardLines('ship the fix today', 10, 3)).toEqual(['ship the', 'fix today'])
    expect(cardLines('今天把修复发布出去', 4, 3)).toEqual(['今天把修', '复发布出', '去'])
    const many = cardLines('one two three four five six seven eight', 9, 2)
    expect(many).toHaveLength(2)
    expect(many[1]).toBe('eight')
    expect(many[0].startsWith('…')).toBe(true)
    expect(cardLines('', 10, 3)).toEqual([])
  })
})
