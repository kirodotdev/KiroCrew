import { describe, expect, it } from 'vitest'
import { WAYS, answerOf, handBack, onCardForWord, pointAndSay, stopsYes, tipOnCard, turnKey, whereLet, type AskMoment, type LetGoAt, type WordOnCard } from './approval'
import { KeyHold } from './gestures'
import { TUNE } from './tune'

const W = { yesWords: 'yes|yeah|ok|okay|go ahead|approve|do it', noWords: "no|nope|don't|do not", waitWords: 'wait|hold on|stop' }
const moment = (m: Partial<AskMoment>): AskMoment => ({ onCard: false, words: '', letGo: '', ...m })

describe('point and say', () => {
  it('is a yes only with both: the finger or hand on the card, and a yes in any words', () => {
    for (const words of ['Yes.', 'ok', 'Okay, go.', 'Go ahead!', 'approve', 'Do it, please']) {
      expect(answerOf(moment({ onCard: true, words }), W)).toBe('yes')
      expect(answerOf(moment({ onCard: false, words }), W)).toBe('nothing')
    }
  })

  it('is nothing with a finger on the card and no word, or a word that is not a yes', () => {
    expect(answerOf(moment({ onCard: true }), W)).toBe('nothing')
    for (const words of ['Summarise what changed.', 'What is shard 3?', 'Is this ok', 'yesterday it failed']) {
      expect(answerOf(moment({ onCard: true, words }), W)).toBe('nothing')
    }
  })

  it('is a no when the sentence begins with a no, with or without a finger on the card', () => {
    for (const onCard of [true, false]) {
      expect(answerOf(moment({ onCard, words: 'No, use shard 2.' }), W)).toBe('no')
      expect(answerOf(moment({ onCard, words: "Don't approve that" }), W)).toBe('no')
    }
  })

  it('never allows a sentence that has a no anywhere in it', () => {
    for (const words of ['Ok, no, wait.', 'Yes... do not run it', 'ok nope']) {
      expect(answerOf(moment({ onCard: true, words }), W)).toBe('nothing')
    }
  })
})

describe('hand back', () => {
  it('is a yes only when the card is let go on its Bot', () => {
    expect(answerOf(moment({ onCard: true, letGo: 'bot' }), W)).toBe('yes')
    expect(answerOf(moment({ onCard: true, letGo: 'away' }), W)).toBe('nothing')
    expect(answerOf(moment({ onCard: true, letGo: '' }), W)).toBe('nothing')
  })

  it('is a no when the card lands in the bin', () => {
    expect(answerOf(moment({ onCard: true, letGo: 'bin' }), W)).toBe('no')
  })

  it('reads a throw as the bin wherever it was aimed, so a flick at the Bot is never a yes', () => {
    expect(whereLet('throw', inHand)).toBe('bin')
    expect(whereLet('throw', { ...inHand, botM: null })).toBe('bin')
    expect(whereLet('place', { ...inHand, bin: true })).toBe('bin')
    expect(whereLet('place', inHand)).toBe('bot')
    expect(whereLet('place', { ...inHand, botM: null })).toBe('away')
  })
})

/** A card let go at rest, 2 cm from its Bot, with a reach of 3 cm. */
const inHand: LetGoAt = { bin: false, botM: 0.02, reachM: TUNE.handBackM, speedMps: 0.05 }
/** A real point: the card rested a second ago, the finger landed 0.2 s before the word, stayed 0.4 s after it, and was slow. */
const point: WordOnCard = { wordAt: 10_000, restAt: 9_000, onSince: 9_800, onUntil: 10_400, fastest: 0.05 }

describe('C1: pointing at the card and saying yes is the main yes', () => {
  it('is the first way in use, and needs one finger and one word', () => {
    expect(WAYS[0]).toBe(pointAndSay)
    expect(answerOf(moment({ onCard: onCardForWord(point), words: 'Ok.' }), W)).toBe('yes')
  })
})

describe('C2: the card must have rested before a yes word can count', () => {
  it('counts a word that starts 0.6 s after the card came to rest, and not one that starts sooner', () => {
    expect(TUNE.cardRestMs).toBe(600)
    expect(onCardForWord({ ...point, restAt: point.wordAt - 600 })).toBe(true)
    expect(onCardForWord({ ...point, restAt: point.wordAt - 599 })).toBe(false)
  })

  it('does not count a word that was already being said when the Bot arrived', () => {
    expect(onCardForWord({ ...point, restAt: point.wordAt + 200 })).toBe(false)
    // A card that is still on its way has not rested at all.
    expect(onCardForWord({ ...point, restAt: 0 })).toBe(false)
  })
})

describe('C3: the finger is on the card when the word starts, still on it a moment later, and slow', () => {
  it('counts a real point', () => {
    expect([TUNE.cardStayMs, TUNE.cardStillMps, TUNE.cardTouchM]).toEqual([350, 0.15, 0.025])
    expect(onCardForWord(point)).toBe(true)
  })

  it('does not count a finger that was not on the card as the word started', () => {
    expect(onCardForWord({ ...point, onSince: 0 })).toBe(false)
    // It landed after the word began.
    expect(onCardForWord({ ...point, onSince: point.wordAt + 50 })).toBe(false)
  })

  it('does not count a hand that passes through: it has left 0.35 s later', () => {
    expect(onCardForWord({ ...point, onUntil: point.wordAt + 349 })).toBe(false)
    expect(onCardForWord({ ...point, onUntil: point.wordAt + 350 })).toBe(true)
  })

  it('does not count a finger that moves at 0.15 m/s or faster', () => {
    expect(onCardForWord({ ...point, fastest: 0.15 })).toBe(false)
    expect(onCardForWord({ ...point, fastest: 0.149 })).toBe(true)
  })

  it('reads "on" as the fingertip inside the card and within 2.5 cm of its face', () => {
    const [w, h] = [0.32, 0.23]
    expect(tipOnCard([0, 0, 0.025], w, h, 1)).toBe(true)
    expect(tipOnCard([0, 0, -0.02], w, h, 1)).toBe(true)
    expect(tipOnCard([0, 0, 0.026], w, h, 1)).toBe(false)
    // A hand that rests 4 cm outside the card, and a finger aimed 1.2 cm short of its edge.
    expect(tipOnCard([w / 2 + 0.04, 0, 0], w, h, 1)).toBe(false)
    expect(tipOnCard([0, -h / 2 - 0.012, 0], w, h, 1)).toBe(false)
    // The card is drawn at 0.6 size: 4 cm in its own units is 2.4 cm in the room.
    expect(tipOnCard([0, 0, 0.04], w, h, 0.6)).toBe(true)
    expect(tipOnCard([0, 0, 0.043], w, h, 0.6)).toBe(false)
  })

  it('so a word with the hand beside the card approves nothing', () => {
    const beside = onCardForWord({ ...point, onSince: 0 })
    expect(answerOf(moment({ onCard: beside, words: 'Yeah, sounds good.' }), W)).toBe('nothing')
  })
})

describe('C4: a hand-back counts only when the card ends in the Bot\'s hand and is let go', () => {
  it('counts a card let go at rest within reach of its Bot', () => {
    expect(whereLet('place', inHand)).toBe('bot')
    expect(whereLet('place', { ...inHand, botM: TUNE.handBackM })).toBe('bot')
    expect(answerOf(moment({ onCard: true, letGo: whereLet('place', inHand) }), W)).toBe('yes')
  })

  it('does not count a bump toward the Bot: one that ends short, or one that is still moving as it passes', () => {
    expect(whereLet('place', { ...inHand, botM: 0.08 })).toBe('away')
    expect(whereLet('place', { ...inHand, botM: TUNE.handBackM + 0.001 })).toBe('away')
    expect(whereLet('place', { ...inHand, speedMps: TUNE.cardStillMps })).toBe('away')
    expect(whereLet('place', { ...inHand, speedMps: 0.4 })).toBe('away')
    expect(answerOf(moment({ onCard: true, letGo: 'away' }), W)).toBe('nothing')
  })

  it('does not count a card that is still in the hand', () => {
    // Nothing was let go: there is no place it ended in.
    expect(answerOf(moment({ onCard: true, letGo: '' }), W)).toBe('nothing')
  })
})

describe('C5: after a yes the Bot reads the command and waits; "no" or "wait" stops it', () => {
  it('waits 0.6 s', () => {
    expect(TUNE.yesBeatMs).toBe(600)
  })

  it('is stopped by a no or a wait, anywhere in what is heard', () => {
    for (const heard of ['No.', 'wait', 'Wait, not that one', 'Hold on.', 'uh, no', 'STOP']) expect(stopsYes(heard, W)).toBe(true)
  })

  it('is not stopped by anything else', () => {
    for (const heard of ['', 'ok', 'Go ahead.', 'nowhere near done', 'waiting room', 'yeah']) expect(stopsYes(heard, W)).toBe(false)
  })
})

describe('C7: the known risk, and the only thing that catches it', () => {
  it('reads "yeah" said to someone else, with the card in the hand, as a yes', () => {
    // A hand that holds the card is on it for as long as it holds it.
    const held = onCardForWord({ ...point, onSince: 5_000, onUntil: 12_000, fastest: 0 })
    expect(answerOf(moment({ onCard: held, words: 'Yeah, sounds good.' }), W)).toBe('yes')
  })

  it('can still be stopped in the beat, if the person notices', () => {
    expect(stopsYes('no, wait', W)).toBe(true)
  })
})

describe('the ways as a list', () => {
  it('uses point-and-say and hand-back, and not the key', () => {
    expect(WAYS.map(w => w.id)).toEqual(['point-and-say', 'hand-back'])
    const k = new KeyHold(0)
    k.feed(80, 0, 0)
    // A turned key means nothing to the ways in use.
    expect(answerOf(moment({ onCard: true, turn: k.end }), W)).toBe('nothing')
  })

  it('keeps the key as a way that can be put back without touching the route', () => {
    const turned = new KeyHold(0)
    turned.feed(80, 0, 0)
    expect(answerOf(moment({ turn: turned.end }), W, [turnKey])).toBe('yes')
    const pulled = new KeyHold(0)
    pulled.feed(0, 20, 0)
    expect(answerOf(moment({ turn: pulled.end }), W, [turnKey])).toBe('no')
    expect(answerOf(moment({ turn: new KeyHold(0).end }), W, [turnKey])).toBe('nothing')
  })

  it('lets a no from one way win over a yes from another', () => {
    expect(answerOf(moment({ onCard: true, words: 'No.', letGo: 'bot' }), W, [pointAndSay, handBack])).toBe('no')
    expect(answerOf(moment({ onCard: true, words: 'ok', letGo: 'bin' }), W, [pointAndSay, handBack])).toBe('no')
  })
})
