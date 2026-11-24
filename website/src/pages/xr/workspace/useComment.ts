/**
 * His note on a board in his hand (TALK slice 2; D4 "没有comment就还给bot", D18 "就像领导批示工作一样", D14 "语音转文字能用"):
 * a pinch on the paper's foot opens the line to write on (`useBoards.foot`). While it is open the card's speech
 * listens (`chatSpeech.ts`, in the card's language) and the card's keyboard stands under the board (`DeskKeys`); his
 * words go onto the paper (`useBoards.note`). When he lets the board go its note is sent (`useBoards.release`):
 * here, through the same send as the new chat card's (`cardSend`, with the line that asks for a short first answer).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Vector3 } from 'three'
import { activeLocale } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { mintSendId } from '../../../utils/sendDelivery'
import type { XrBot } from '../model'
import { cardSend } from '../writes'
import { LANG_KEY, listen, startLang } from './chatSpeech'
import type { KeysDrawn } from './DeskKeys'
import { KEYS } from './deskKeys'
import { placeAt } from './layout'
import type { KeysPlace } from './useChatCard'

/** Where words sent from XR were written: a note on a board, a hold's sheet, or a choice printed on a Bot's words. */
export type SentFrom = 'note' | 'speak' | 'choice'

/** How much a note holds, and where the keyboard stands for it: under the board in his hand. */
export const NOTE_WRITE = { mostChars: 400, keysPitchDeg: -36, keysM: 0.6 } as const

export interface CommentHost {
  head: () => Vector3
  lookYawDeg: () => number
  bots: () => readonly XrBot[]
  say: (line: string) => void
}

/**
 * What the board that he writes on is, and how its note is written. `keysAlways`: the keyboard stands under it always
 * (a note); else only when the browser has no speech recognizer (a hold's sheet, P2).
 */
interface Writing { id: string; comment: string; keysAlways?: boolean }

/** The next note after a key: one character more, one less (`Backspace`), or the line closed (`Enter`, `null`). */
export function noteAfterKey(text: string, key: string): string | null {
  if (key === 'Enter') return null
  if (key === 'Backspace') return [...text].slice(0, -1).join('')
  return [...key].length === 1 ? [...(text + key)].slice(0, NOTE_WRITE.mostChars).join('') : text
}

/** Words heard after the note's words, with one space between. */
export const noteHeard = (text: string, heard: string): string => [...`${text}${text && heard.trim() ? ' ' : ''}${heard.trim()}`].slice(0, NOTE_WRITE.mostChars).join('')

export function useComment(writing: Writing | null, note: (id: string, text: string) => void, close: (id: string) => void, host: CommentHost) {
  const hostNow = useRef(host)
  hostNow.current = host
  const writingNow = useRef(writing)
  writingNow.current = writing
  const [interim, setInterim] = useState('')
  const [listening, setListening] = useState(false)
  const [keys, setKeys] = useState<KeysPlace | null>(null)
  const [noSpeech, setNoSpeech] = useState(false)
  const sends = useRef({ sent: 0, ok: 0, last: '' })
  const drawn = useMemo<{ keys: KeysDrawn | null }>(() => ({ keys: null }), [])
  const id = writing?.id ?? ''

  // The line opens: the keyboard stands under the board, and the recognizer listens until it closes.
  useEffect(() => {
    setInterim('')
    setNoSpeech(false)
    if (!id) { setKeys(null); setListening(false); return }
    const h = hostNow.current
    const yaw = h.lookYawDeg()
    const at = placeAt(h.head(), yaw, NOTE_WRITE.keysPitchDeg, NOTE_WRITE.keysM)
    setKeys({ at: [at.x, at.y, at.z], yawDeg: yaw, flat: false, stepM: KEYS.standStepM })
    let kept: string | null = null
    try { kept = window.localStorage.getItem(LANG_KEY) } catch { /* no store: the dashboard's language */ }
    const heard = listen(startLang(kept, activeLocale()), {
      started: () => setListening(true),
      heard: (final, words) => {
        const w = writingNow.current
        if (!w || w.id !== id) return
        if (final.trim()) note(id, noteHeard(w.comment, final))
        setInterim(words)
      },
      none: () => { setListening(false); setNoSpeech(true) },
      quiet: () => setListening(false),
    })
    // No recognizer in this browser at all: the keyboard is the way to write.
    if (!heard) setNoSpeech(true)
    return () => { heard?.stop(); setListening(false) }
  }, [id, note])

  /** A key of the keyboard under the board, or of a real keyboard: it writes on the note; Enter closes the line. */
  const key = useCallback((k: string) => {
    const w = writingNow.current
    if (!w) return false
    const next = noteAfterKey(w.comment, k)
    if (next === null) close(w.id)
    else note(w.id, next)
    return true
  }, [note, close])
  /** A real keyboard's key: taken while the line is open (as the card's keyboard takes it). */
  const typed = useCallback((k: string) => (!!writingNow.current && (k === 'Enter' || k === 'Backspace' || [...k].length === 1) ? key(k) : false), [key])

  /**
   * His words to the Bot's own thread, as one send of words from XR; whether they went. `from`: a note, a hold's sheet,
   * or a choice printed on its words (the line names the choice: the first line of `text`).
   */
  const send = useCallback((slug: string, text: string, from: SentFrom = 'note', id: string = mintSendId()): Promise<boolean> => {
    const h = hostNow.current
    const bot = h.bots().find(b => b.slug === slug)
    // A Bot that left the crew: nothing is sent, and he is told why his words come back (T1).
    if (!bot) { h.say(i18nT('xr.note_no_bot')); return Promise.resolve(false) }
    sends.current.sent += 1
    sends.current.last = text
    return cardSend(text, { bot: bot.slug, thread: bot.thread }, id).then(
      () => { sends.current.ok += 1; h.say(from === 'choice' ? i18nT('xr.choice_sent', { name: bot.label, choice: text.split('\n')[0] }) : i18nT(from === 'speak' ? 'xr.speak_sent' : 'xr.note_sent', { name: bot.label })); return true },
      () => { h.say(i18nT('xr.note_failed')); return false },
    )
  }, [])
  const shown = writing && !writing.keysAlways && !noSpeech ? null : keys
  const read = useCallback(() => ({ ...sends.current, writing: writingNow.current?.id ?? '', interim, listening, keys: shown && drawn.keys ? { ...shown, ...drawn.keys } : null }), [interim, listening, shown, drawn])
  return { hearing: { interim, listening }, keys: shown, key, typed, send, drawn, read }
}
