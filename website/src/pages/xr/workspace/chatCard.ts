/**
 * The new chat card, as plain rules: a card comes out of the palette's "new chat", the person speaks and the words
 * show on it, and a throw sends it. Thrown at a Bot it goes to that Bot; thrown into the open it starts a new chat.
 * Thrown with no words on it, it is not sent: a keyboard comes, the card stands by it with its target written on
 * it, and Enter sends it there. An empty card is never sent, and a card is sent once.
 *
 * A throw needs no raised hand. A pinch takes the card (the dot of the head lies on it, or the mouse does), the
 * head or the mouse turns to where it shall go, and letting go throws it there: it flies forward to its target.
 * Let go where it was taken, it stays. Nothing here draws or reads a device; `ChatCard.tsx` draws the card and
 * `useChatCard.ts` holds it, listens (`chatSpeech.ts`) and sends it (`writes.ts`).
 */
export type V3 = readonly [number, number, number]

export const CARD = {
  /** The card's size, and how far from the eye it stands when it comes out. */
  widthM: 0.2,
  heightM: 0.125,
  outM: 0.65,
  /** Where it comes out with no desk: this far to the left of where the person looks, and this far down. */
  outYawDeg: -18,
  outPitchDeg: 2,
  /** With a desk it stands upright over the palette's tiles: across, in from the near edge, and over the top. */
  deskU: -0.28,
  deskV: 0.33,
  deskUpM: 0.17,
  /** Held, it rides this far from the eye and this far over the aim, at this share of its size. */
  heldM: 0.5,
  heldOverDeg: 12,
  heldScale: 0.6,
  /** The aim has left the card's own place when it is this far from the card's middle: only then is a let-go a throw. */
  homeDeg: 9,
  /** A Bot takes a card whose aim passes within this angle of its body. */
  botDeg: 4,
  /** Thrown into the open, it lands this far ahead. */
  openM: 1.4,
  /** How long it flies, and how long it shows that it was sent before it goes. */
  flyMs: 420,
  sentMs: 700,
  /** The most characters a card holds. */
  mostChars: 2000,
} as const

/** Where a card goes: to a Bot, or into the open (a new chat). */
export type CardTarget = { to: 'bot'; slug: string } | { to: 'open' }

/** The microphone: it listens, it does not (yet, or any more), or there is no speech recognition here. */
export type MicState = 'on' | 'off' | 'none'

/**
 * `out`: it stands where it came out. `held`: a pinch has it. `typing`: it was thrown with no words: it stands by
 * the keyboard with its target. `flying`: on its way. `sending`: at its target, the send is under way. `sent`: done.
 */
export type CardPhase = 'out' | 'held' | 'typing' | 'flying' | 'sending' | 'sent'

export interface Card {
  /** The one id of this card's send: a send with the same id goes out once (`writes.ts`). */
  id: string
  phase: CardPhase
  /** The words on it, and the words that the recognizer still works on. */
  text: string
  interim: string
  mic: MicState
  /** The microphone was turned off, by its quiet time or by a pinch on its mark: it stays off until a pinch turns it on. */
  paused: boolean
  /** The language it listens in, as the recognizer names it (`chatSpeech.ts`, `speechLangs`). */
  lang: string
  target: CardTarget | null
  /** Where a pinch took it from: let go on its own place, it is that again. */
  from: 'out' | 'typing'
  /**
   * Its last send did not go through: it came back frozen, with its words, its target and its id as they were. It
   * takes no new words and does not listen; a throw of it, or a press on it, sends exactly that again. To say
   * something else he puts it away and takes a new card.
   */
  failed: boolean
}

export type CardEvent =
  /** Words from the recognizer. `lang`: the language that this recognizer listens in. */
  | { type: 'heard'; final: string; interim: string; lang?: string }
  | { type: 'mic'; state: MicState }
  /** The microphone goes off: no words for its quiet time, the page was hidden, or the session ended. */
  | { type: 'quiet' }
  /** A pinch on the microphone's mark: off when it is on, on when it is off. */
  | { type: 'micPress' }
  /** A pinch on the language's mark chose this language. */
  | { type: 'lang'; lang: string }
  /** One character, `Backspace` or `Enter`, from the keyboard in the room or a real one. */
  | { type: 'key'; key: string }
  | { type: 'grab' }
  /** The pinch let go: at a target, or (`null`) on the card's own place. */
  | { type: 'release'; target: CardTarget | null }
  /** The pinch was called off (the hand was lost). */
  | { type: 'drop' }
  | { type: 'landed' }
  | { type: 'result'; ok: boolean }

/**
 * The band of `drawOrder.ts` the card is drawn in (D105 3). Held, it is in his hand as a held board is: `held`, over
 * Home's strip (50 over 30), so the top line that says what the let-go will do is never under Home. At its place, in
 * flight and sent, it is the room's own thing (`page`), and Home is drawn over it again.
 */
export const cardLayer = (phase: CardPhase): 'held' | 'page' => (phase === 'held' ? 'held' : 'page')

export const newCard = (id: string, speech: boolean, lang = ''): Card => ({ id, phase: 'out', text: '', interim: '', mic: speech ? 'off' : 'none', paused: false, lang, target: null, from: 'out', failed: false })

/** Whether the card has words to send. */
export const hasWords = (card: Card): boolean => card.text.trim().length > 0

/** Whether the recognizer shall listen: the card is out or in the hand, speech recognition is there, and the microphone was not turned off. */
export const listens = (card: Card | null): boolean => !!card && card.mic !== 'none' && !card.paused && !card.failed && (card.phase === 'out' || card.phase === 'held')

/** Whether the card shows its language's mark: wherever it may listen. */
export const showsLang = (card: Card | null): boolean => !!card && card.mic !== 'none' && !card.failed && (card.phase === 'out' || card.phase === 'held')

/** Whether the keyboard shows: after a throw with no words, or at once where there is no speech recognition. */
export const keysShown = (card: Card | null): boolean => !!card && !card.failed && (card.phase === 'typing' || (card.mic !== 'on' && (card.phase === 'out' || card.phase === 'held')))

const CJK = /[\u2E80-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF]/
const cut = (text: string): string => [...text].slice(0, CARD.mostChars).join('')

/** Two runs of words as one: a space between them, and none where either side is a sign of a script that uses none. */
export function joinWords(a: string, b: string): string {
  const [x, y] = [a.trimEnd(), b.trim()]
  if (!x || !y) return x || y
  return CJK.test(x.slice(-1)) || CJK.test(y[0]) ? x + y : `${x} ${y}`
}

/** The card after something happened. What does not fit the card's phase changes nothing. */
export function cardStep(card: Card, e: CardEvent): Card {
  const listening = card.phase === 'out' || card.phase === 'held'
  // A card whose send failed is frozen: it is taken, let go or dropped, and nothing else changes it.
  if (card.failed && e.type !== 'grab' && e.type !== 'release' && e.type !== 'drop') return card
  switch (e.type) {
    case 'heard':
      // A result of a recognizer that listens in another language than the card's is late: the language was
      // switched, that recognizer is not stopped yet, and its word is on the card already.
      if (e.lang !== undefined && e.lang !== card.lang) return card
      return listening ? { ...card, text: cut(joinWords(card.text, e.final)), interim: e.interim.trim() } : card
    case 'mic':
      // A recognizer that says it listens after the microphone was turned off is not believed.
      if (card.paused && e.state === 'on') return card
      return card.mic === e.state || card.mic === 'none' ? card : { ...card, mic: e.state, interim: e.state === 'on' ? card.interim : '' }
    case 'quiet':
      // The words stay, with the ones still worked on: he said them.
      return listens(card) ? { ...card, paused: true, mic: 'off', text: cut(joinWords(card.text, card.interim)), interim: '' } : card
    case 'micPress':
      if (card.mic === 'none' || !listening) return card
      return card.paused ? { ...card, paused: false } : { ...card, paused: true, mic: 'off', text: cut(joinWords(card.text, card.interim)), interim: '' }
    case 'lang':
      // The word still worked on is the card's before the recognizer is stopped for the other language: he said it.
      return card.lang === e.lang || !e.lang ? card : { ...card, lang: e.lang, text: cut(joinWords(card.text, card.interim)), interim: '' }
    case 'key': {
      if (!keysShown(card)) return card
      if (e.key === 'Enter') return card.phase === 'typing' && card.target && hasWords(card) ? { ...card, phase: 'flying', failed: false } : card
      if (e.key === 'Backspace') return { ...card, text: [...card.text].slice(0, -1).join('') }
      return [...e.key].length === 1 ? { ...card, text: cut(card.text + e.key) } : card
    }
    case 'grab':
      return card.phase === 'out' || card.phase === 'typing' ? { ...card, phase: 'held', from: card.phase } : card
    case 'drop':
      return card.phase === 'held' ? { ...card, phase: card.from } : card
    case 'release': {
      if (card.phase !== 'held') return card
      // A frozen card goes to its own target again, wherever it is let go, with the words it has.
      if (card.failed) return card.target && hasWords(card) ? { ...card, phase: 'flying', failed: false } : { ...card, phase: card.from }
      if (!e.target) return { ...card, phase: card.from }
      // The words that the recognizer still worked on at the throw are the card's: he said them.
      const text = cut(joinWords(card.text, card.interim))
      return { ...card, text, interim: '', target: e.target, failed: false, phase: text.trim() ? 'flying' : 'typing' }
    }
    case 'landed':
      // An empty card is never sent, whatever brought it here.
      return card.phase === 'flying' ? (hasWords(card) && card.target ? { ...card, phase: 'sending' } : { ...card, phase: 'typing' }) : card
    case 'result':
      if (card.phase !== 'sending') return card
      return e.ok ? { ...card, phase: 'sent' } : { ...card, phase: 'out', from: 'out', failed: true, interim: '', mic: card.mic === 'none' ? 'none' : 'off' }
  }
}

/** A press that went down on one of the card's marks: which mark, and which pointer. */
export interface MarkDown { name: string; pointerId: number }

/** Whether an up on the mark `name` ends a press that went down on that same mark with that same pointer: only then does the mark act. */
export const ownUp = (down: MarkDown | null, name: string, pointerId: number): boolean => !!down && down.name === name && down.pointerId === pointerId

/** What a pointer does on one of the card's marks. */
export interface MarkEvent { type: 'down' | 'up' | 'leave' | 'cancel'; name: string; pointerId: number }

/**
 * The kept down after a pointer did something on a mark, and whether the mark acts now. A down is kept; a leave of
 * that mark and a cancel (a long press that ends as one, a lost hand) drop it, so that no later up finds it; an up
 * drops it and acts when it is that down's own.
 */
export function markStep(down: MarkDown | null, e: MarkEvent): { down: MarkDown | null; acts: boolean } {
  if (e.type === 'down') return { down: { name: e.name, pointerId: e.pointerId }, acts: false }
  if (e.type === 'up') return { down: null, acts: ownUp(down, e.name, e.pointerId) }
  if (e.type === 'cancel') return { down: null, acts: false }
  return { down: down?.name === e.name ? null : down, acts: false }
}

/** A Bot as a throw reads it: its body's middle and how far its body reaches from there across (half its width, as its press box). */
export interface BotAt { slug: string; at: V3; radiusM: number; /** How far the mark round it reaches: half its larger side. */ markM?: number }

const RAD = Math.PI / 180
const sub = (a: V3, b: V3): [number, number, number] => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
const len = (a: V3): number => Math.hypot(a[0], a[1], a[2])

/** The angle between two directions, in degrees. */
export function angleDeg(a: V3, b: V3): number {
  const d = len(a) * len(b)
  if (d < 1e-9) return 180
  return Math.acos(Math.max(-1, Math.min(1, (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / d))) / RAD
}

/**
 * Where a card that is let go now would go, for an eye at `eye` that aims along `dir`: to the Bot that the aim passes
 * nearest, within `botDeg` of its body; else into the open. Within `homeDeg` of the card's own place `home` it goes
 * back there (`null`), unless the aim is nearer a Bot's MIDDLE than the place's middle (D104 1): a send is an act and a
 * put-back is not, so in doubt the card goes back. Like with like: a Bot's body reaches toward the place, the place's
 * card reaches toward the Bot, and neither edge counts there. A throw at a Bot's middle always sends.
 */
export function aimTarget(eye: V3, dir: V3, home: V3, bots: readonly BotAt[]): CardTarget | null {
  const homeOff = angleDeg(dir, sub(home, eye))
  let best: BotAt | null = null
  let bestOff = Infinity
  // Near the place: of the Bots the aim lies on, the one whose middle it is nearest.
  let mid: BotAt | null = null
  let midOff = Infinity
  for (const bot of bots) {
    const to = sub(bot.at, eye)
    const toMid = angleDeg(dir, to)
    const off = toMid - Math.atan(bot.radiusM / Math.max(len(to), 1e-6)) / RAD
    if (off < bestOff) { best = bot; bestOff = off }
    if (off <= CARD.botDeg && toMid < midOff) { mid = bot; midOff = toMid }
  }
  if (homeOff < CARD.homeDeg) return mid && midOff < homeOff ? { to: 'bot', slug: mid.slug } : null
  return best && bestOff <= CARD.botDeg ? { to: 'bot', slug: best.slug } : { to: 'open' }
}

/** A send needs the same target to have stood this long before the let-go (M77, D106 4); else the card goes back. */
export const STEADY_MS = 150

/**
 * The one reading of the aim that the words, the lit mark and the act all use (M77). `raw` is `aimTarget` this frame;
 * `was` what stood before and since when. The shown aim is `raw` once the same target has stood `STEADY_MS`, and
 * `null` (back to its place) until then: in doubt back, and the card says so in the same reading.
 */
export function steadyAim(raw: CardTarget | null, was: { raw: CardTarget | null; since: number }, now: number): { shown: CardTarget | null; raw: CardTarget | null; since: number } {
  const same = JSON.stringify(raw) === JSON.stringify(was.raw)
  const since = same ? was.since : now
  return { shown: raw !== null && now - since >= STEADY_MS ? raw : null, raw, since }
}

/** Where a thrown card lands: at its Bot's body, or `openM` ahead along the aim. */
export function landing(eye: V3, dir: V3, target: CardTarget, bots: readonly BotAt[]): [number, number, number] {
  const bot = target.to === 'bot' ? bots.find(b => b.slug === target.slug) : undefined
  if (bot) return [bot.at[0], bot.at[1], bot.at[2]]
  const k = CARD.openM / Math.max(len(dir), 1e-6)
  return [eye[0] + dir[0] * k, eye[1] + dir[1] * k, eye[2] + dir[2] * k]
}

/** How far along its flight a card is at share `k` of the time: slow out of the hand, fast at the end, as a thing thrown forward. */
export const flyShare = (k: number): number => { const c = Math.max(0, Math.min(1, k)); return c * c }

/** The lines of the card's words that fit `most` lines of `perLine` characters: the last ones, since the newest words matter. */
export function cardLines(text: string, perLine: number, most: number): string[] {
  const lines: string[] = []
  let line = ''
  const push = () => { if (line) lines.push(line); line = '' }
  for (const word of text.split(/(\s+)/)) {
    if (!word) continue
    if (/^\s+$/.test(word)) { if (line && [...line].length < perLine) line += ' '; continue }
    // A word that does not fit goes to the next line; one longer than a line (a script with no spaces) is cut by signs.
    if ([...line].length + [...word].length > perLine) { line = line.trimEnd(); push() }
    let rest = [...word]
    while (rest.length > perLine) { lines.push(rest.slice(0, perLine).join('')); rest = rest.slice(perLine) }
    line += rest.join('')
  }
  line = line.trimEnd()
  push()
  return lines.length > most ? ['…' + lines[lines.length - most].slice(1), ...lines.slice(lines.length - most + 1)] : lines
}
