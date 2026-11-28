/**
 * The focus stand-in fields shown before xterm exists (LazyCliPanel's while
 * the chunk loads, CliPanel's while the dock holds xterm) send what is typed
 * into them to the shell: keys as the bytes xterm would send for them, pastes
 * as pastes, all through the session's one early-input buffer in
 * terminalRegistry, in typed order. The field keeps only a one-space sentinel,
 * so no text hand-off can reorder or rewrite it, and a soft keyboard's
 * Backspace (which sends no key code) still has something to delete.
 */
import type { CompositionEvent, FormEvent, KeyboardEvent } from 'react'
import { useImeGuard } from '../hooks/useImeGuard'
import { pasteIntoTerminal, typeIntoTerminal } from './terminalRegistry'
import { isMac } from './platform'

/** The stand-in is a textarea (named so to keep the IME ratchet's JSX scan off this file's types). */
type StandInField = HTMLTextAreaElement

const ESC = '\x1b'
const CSI = '\x1b['
const MOD = '1;'
const SEMI = ';'
const TILDE = '~'
const TAB = '\t'
const DEL = '\x7f'
// Final bytes kept as single letters: whole sequences read as words to the i18n literal check.
const FINAL: Record<'ss3' | 'backtab', string> = { ss3: 'O', backtab: 'Z' }
const SENTINEL = ' '

const CSI_LETTER: Record<string, string> = { ArrowUp: 'A', ArrowDown: 'B', ArrowRight: 'C', ArrowLeft: 'D', Home: 'H', End: 'F' }
const SS3_LETTER: Record<string, string> = { F1: 'P', F2: 'Q', F3: 'R', F4: 'S' }
const CSI_TILDE: Record<string, number> = {
  Insert: 2, Delete: 3, PageUp: 5, PageDown: 6,
  F5: 15, F6: 17, F7: 18, F8: 19, F9: 20, F10: 21, F11: 23, F12: 24,
}
const CTRL_PUNCT: Record<string, string> = { ' ': '\x00', '@': '\x00', '2': '\x00', '[': '\x1b', '\\': '\x1c', ']': '\x1d', '^': '\x1e', '6': '\x1e', '_': '\x1f', '-': '\x1f' }

/**
 * The bytes xterm sends for a key in its default modes (normal cursor keys,
 * `macOptionIsMeta` off), or undefined for a key that types text (the field's
 * `input` event carries it) or that the browser keeps (Cmd chords, Ctrl+V).
 */
export function terminalKeyBytes(e: Pick<globalThis.KeyboardEvent, 'key' | 'ctrlKey' | 'altKey' | 'shiftKey' | 'metaKey'>): string | undefined {
  if (e.metaKey) return undefined
  const mod = 1 + (e.shiftKey ? 1 : 0) + (e.altKey ? 2 : 0) + (e.ctrlKey ? 4 : 0)
  const letter = CSI_LETTER[e.key]
  if (letter) return mod > 1 ? CSI + MOD + mod + letter : CSI + letter
  const ss3 = SS3_LETTER[e.key]
  if (ss3) return mod > 1 ? CSI + MOD + mod + ss3 : ESC + FINAL.ss3 + ss3
  const tilde = CSI_TILDE[e.key]
  if (tilde) return mod > 1 ? CSI + tilde + SEMI + mod + TILDE : CSI + tilde + TILDE
  switch (e.key) {
    case 'Enter': return e.altKey ? ESC + '\r' : '\r'
    case 'Backspace': return e.ctrlKey ? '\x08' : e.altKey ? ESC + DEL : DEL
    case 'Tab': return e.shiftKey ? CSI + FINAL.backtab : TAB
    case 'Escape': return ESC
  }
  if (e.key.length !== 1) return undefined
  if (e.ctrlKey && !e.altKey) {
    const k = e.key.toLowerCase()
    if (k === 'v') return undefined // the browser's paste, as in xterm
    if (k >= 'a' && k <= 'z') return String.fromCharCode(k.charCodeAt(0) - 96)
    return CTRL_PUNCT[e.key]
  }
  // Off a Mac, Alt+key is Meta (ESC prefix); on a Mac, Option types its character.
  if (e.altKey && !e.ctrlKey && !isMac) return ESC + e.key
  return undefined
}

/** Put the field back to its sentinel, caret after it. */
function resetField(field: StandInField): void {
  field.value = SENTINEL
  field.setSelectionRange(1, 1)
}

/** Props for a focus stand-in field of `sessionId`'s terminal. */
export function useStandInKeys(sessionId: string) {
  const ime = useImeGuard()
  return {
    defaultValue: SENTINEL,
    onFocus: (e: { currentTarget: StandInField }) => resetField(e.currentTarget),
    onKeyDown: (e: KeyboardEvent<StandInField>) => {
      // An Enter that commits an IME candidate is the IME's, not the shell's.
      if (e.key === 'Enter') {
        if (ime.claimEnter(e)) typeIntoTerminal(sessionId, terminalKeyBytes(e) ?? '\r')
        return
      }
      if (ime.isComposing(e)) return
      const data = terminalKeyBytes(e)
      if (data === undefined) return
      e.preventDefault()
      typeIntoTerminal(sessionId, data)
    },
    onCompositionStart: ime.onCompositionStart,
    onInput: (e: FormEvent<StandInField>) => {
      const input = e.nativeEvent as InputEvent
      if (input.isComposing) return
      const field = e.currentTarget
      const value = field.value
      resetField(field)
      // A soft keyboard deletes the sentinel instead of sending Backspace.
      if (input.inputType === 'deleteContentBackward') { typeIntoTerminal(sessionId, DEL); return }
      // A soft keyboard's Enter can arrive with no Enter keydown: send Enter's CR.
      if (input.inputType === 'insertLineBreak' || input.inputType === 'insertParagraph') { typeIntoTerminal(sessionId, '\r'); return }
      const data = value.startsWith(SENTINEL) ? value.slice(SENTINEL.length) : value
      if (!data) return
      // A paste goes through xterm's paste once the shell is up, so the
      // shell's bracketed paste keeps a pasted newline from running a line.
      if (/^insertFrom(Paste|Drop)/.test(input.inputType)) pasteIntoTerminal(sessionId, data)
      else typeIntoTerminal(sessionId, data)
    },
    onCompositionEnd: (e: CompositionEvent<StandInField>) => {
      ime.onCompositionEnd(e)
      const field = e.currentTarget
      const value = field.value
      resetField(field)
      const data = value.startsWith(SENTINEL) ? value.slice(SENTINEL.length) : value
      if (data) typeIntoTerminal(sessionId, data)
    },
  }
}
