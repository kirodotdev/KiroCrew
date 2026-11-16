/**
 * The default mute chord must not press the default push-to-talk key.
 *
 * The mute chord is only ever pressed while a dictation is recording, and a
 * press of the push-to-talk key during a recording stops it (and, with send on
 * stop, sends it). On macOS that key is a bare right Option by default, so a
 * mute chord with Option in it ends the dictation on its first keydown, before
 * the chord is complete.
 *
 * Every keydown the chord can produce is checked: each modifier on either side
 * of the keyboard, in every press order, then the letter.
 */
import { describe, expect, it } from 'vitest'

import { defaultBinding, matchesBinding, matchesBindingWithShift } from '../lib/pushToTalk'
import { shortcutEntry, type Chord, type ShortcutPlatform } from '../lib/shortcutRegistry'

type Flag = 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'
type Down = { code: string; altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }

const CODES: Record<Flag, [string, string]> = {
  altKey: ['AltLeft', 'AltRight'],
  ctrlKey: ['ControlLeft', 'ControlRight'],
  metaKey: ['MetaLeft', 'MetaRight'],
  shiftKey: ['ShiftLeft', 'ShiftRight'],
}

function modifierFlags(chord: Chord, platform: ShortcutPlatform): Flag[] {
  const flags: Flag[] = []
  if (chord.alt) flags.push('altKey')
  if (chord.ctrl) flags.push('ctrlKey')
  if (chord.mod) flags.push(platform === 'mac' ? 'metaKey' : 'ctrlKey')
  if (chord.shift) flags.push('shiftKey')
  return flags
}

function permutations<T>(items: T[]): T[][] {
  if (items.length <= 1) return [items]
  return items.flatMap((item, i) => permutations([...items.slice(0, i), ...items.slice(i + 1)]).map(rest => [item, ...rest]))
}

/** Every keydown event pressing `chord` can fire, over all orders and sides. */
function keydowns(chord: Chord, platform: ShortcutPlatform): Down[] {
  const out: Down[] = []
  for (const order of permutations(modifierFlags(chord, platform))) {
    for (let sides = 0; sides < 2 ** order.length; sides++) {
      const held: Down = { code: '', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false }
      order.forEach((flag, i) => {
        held[flag] = true
        out.push({ ...held, code: CODES[flag][(sides >> i) & 1] })
      })
      out.push({ ...held, code: `Key${chord.key.toUpperCase()}` })
    }
  }
  return out
}

const stopsARecording = (chord: Chord, platform: ShortcutPlatform) => {
  const ptt = defaultBinding(platform === 'mac')
  return keydowns(chord, platform).filter(e => matchesBinding(e, ptt) || matchesBindingWithShift(e, ptt))
}

describe('default mute chord against default push-to-talk', () => {
  it.each(['mac', 'other'] as const)('no keydown of the %s chord presses the push-to-talk key', platform => {
    const chord = shortcutEntry('toggle-dictation-mute')?.defaults[platform]
    expect(chord).toBeTruthy()
    expect(stopsARecording(chord!, platform)).toEqual([])
  })

  it('an Option chord on macOS would: the right Option keydown is the push-to-talk key', () => {
    // The check above has to be able to fail. This is the chord it exists to
    // keep out of the macOS default.
    const hits = stopsARecording({ key: 'q', alt: true, shift: true }, 'mac')
    expect(hits.some(e => e.code === 'AltRight')).toBe(true)
  })
})
