/**
 * A stand-in field sends a key as the bytes xterm sends for it in its default
 * modes, so keys typed before xterm exists reach the shell the same way.
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('./platform', () => ({ isMac: false }))
vi.mock('./terminalRegistry', () => ({ typeIntoTerminal: vi.fn(), pasteIntoTerminal: vi.fn() }))

import { terminalKeyBytes } from './terminalStandInKeys'

const key = (k: string, mods: Partial<Record<'ctrlKey' | 'altKey' | 'shiftKey' | 'metaKey', boolean>> = {}) =>
  terminalKeyBytes({ key: k, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...mods })

describe('terminalKeyBytes', () => {
  it.each([
    ['Enter', {}, '\r'],
    ['Backspace', {}, '\x7f'],
    ['Tab', {}, '\t'],
    ['Tab', { shiftKey: true }, '\x1b[Z'],
    ['Escape', {}, '\x1b'],
    ['ArrowLeft', {}, '\x1b[D'],
    ['ArrowUp', {}, '\x1b[A'],
    ['ArrowRight', { ctrlKey: true }, '\x1b[1;5C'],
    ['Home', {}, '\x1b[H'],
    ['End', { shiftKey: true }, '\x1b[1;2F'],
    ['Delete', {}, '\x1b[3~'],
    ['PageUp', {}, '\x1b[5~'],
    ['F1', {}, '\x1bOP'],
    ['F5', {}, '\x1b[15~'],
    ['c', { ctrlKey: true }, '\x03'],
    ['E', { ctrlKey: true, shiftKey: true }, '\x05'],
    ['[', { ctrlKey: true }, '\x1b'],
    ['b', { altKey: true }, '\x1bb'],
  ] as const)('%s %j -> %j', (k, mods, bytes) => {
    expect(key(k, mods)).toBe(bytes)
  })

  it.each([
    ['a', {}],
    ['v', { ctrlKey: true }],
    ['c', { metaKey: true }],
    ['Shift', {}],
  ] as const)('leaves %s %j to the field or the browser', (k, mods) => {
    expect(key(k, mods)).toBeUndefined()
  })
})
