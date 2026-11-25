import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { acceleratorFromEvent, formatAcceleratorKeys } from './globalHotkey'

type RecorderEvent = Parameters<typeof acceleratorFromEvent>[0]
const keydown = (init: Partial<RecorderEvent>): RecorderEvent => ({
  code: '', key: '', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...init,
})

describe('acceleratorFromEvent', () => {
  it('spells the modifiers in a fixed order, then the key', () => {
    expect(acceleratorFromEvent(keydown({ code: 'KeyK', key: 'K', altKey: true, shiftKey: true }), false)).toBe('Alt+Shift+K')
    expect(acceleratorFromEvent(keydown({ code: 'KeyK', key: 'k', metaKey: true, shiftKey: true }), true)).toBe('Cmd+Shift+K')
    expect(acceleratorFromEvent(keydown({ code: 'Space', key: ' ', ctrlKey: true, altKey: true }), false)).toBe('Ctrl+Alt+Space')
    expect(acceleratorFromEvent(keydown({ code: 'F12', key: 'F12', metaKey: true, ctrlKey: true, altKey: true, shiftKey: true }), true))
      .toBe('Cmd+Ctrl+Alt+Shift+F12')
    expect(acceleratorFromEvent(keydown({ code: 'Digit7', key: '7', ctrlKey: true }), false)).toBe('Ctrl+7')
    expect(acceleratorFromEvent(keydown({ code: 'ArrowUp', key: 'ArrowUp', altKey: true }), false)).toBe('Alt+Up')
  })

  it('reads a letter the macOS Option layer turned into a glyph from its position', () => {
    expect(acceleratorFromEvent(keydown({ code: 'KeyK', key: '\u02da', altKey: true }), true)).toBe('Alt+K')
  })

  it.each([
    ['a bare key', { code: 'KeyK', key: 'k' }, false],
    ['Shift alone, which only types a capital', { code: 'KeyK', key: 'K', shiftKey: true }, false],
    ['a modifier pressed by itself', { code: 'AltLeft', key: 'Alt', altKey: true }, false],
    ['a punctuation key', { code: 'Comma', key: ',', ctrlKey: true }, false],
    ['a numpad key', { code: 'Numpad5', key: '5', ctrlKey: true }, false],
    ['the Windows key', { code: 'KeyK', key: 'k', metaKey: true, altKey: true }, false],
  ])('keeps waiting on %s', (_case, init, mac) => {
    expect(acceleratorFromEvent(keydown(init), mac)).toBeNull()
  })

  it('builds only chords the desktop shell accepts over IPC', () => {
    // The shell bounds what the recorder may send (parseRecordedHotkey). Read
    // its pattern from the source: importing the module would load Electron.
    const shell = readFileSync(resolve(process.cwd(), 'electron/global-hotkey.js'), 'utf8')
    const pattern = shell.match(/const RECORDED_HOTKEY_RE =\s*\/(.+)\/;/)
    expect(pattern, 'RECORDED_HOTKEY_RE is missing from electron/global-hotkey.js').not.toBeNull()
    const accepted = new RegExp(pattern![1])

    const keys: Array<[string, string]> = [
      ['KeyA', 'a'], ['KeyZ', 'Z'], ['Digit0', '0'], ['Digit9', '9'], ['F1', 'F1'], ['F24', 'F24'],
      ['Space', ' '], ['Tab', 'Tab'], ['Enter', 'Enter'], ['Backspace', 'Backspace'], ['Delete', 'Delete'],
      ['Insert', 'Insert'], ['Home', 'Home'], ['End', 'End'], ['PageUp', 'PageUp'], ['PageDown', 'PageDown'],
      ['ArrowUp', 'ArrowUp'], ['ArrowDown', 'ArrowDown'], ['ArrowLeft', 'ArrowLeft'], ['ArrowRight', 'ArrowRight'],
    ]
    let recorded = 0
    for (const [code, key] of keys) {
      for (let mods = 0; mods < 16; mods++) {
        const event = keydown({ code, key, metaKey: !!(mods & 1), ctrlKey: !!(mods & 2), altKey: !!(mods & 4), shiftKey: !!(mods & 8) })
        const accelerator = acceleratorFromEvent(event, true)
        if (accelerator === null) continue
        recorded += 1
        expect(accelerator).toMatch(accepted)
        expect(accelerator).toMatch(/^(Cmd|Ctrl|Alt)\+/)
      }
    }
    // Every key with every modifier set that holds Command, Control or Alt.
    expect(recorded).toBe(keys.length * 14)
  })
})

describe('formatAcceleratorKeys', () => {
  it('maps CommandOrControl to the platform cap', () => {
    expect(formatAcceleratorKeys('CommandOrControl+Shift+K', true)).toEqual(['\u2318', '\u21e7', 'K'])
    expect(formatAcceleratorKeys('CommandOrControl+Shift+K', false)).toEqual(['Ctrl', 'Shift', 'K'])
  })

  it('maps the Alt+Shift default used on Windows/Linux', () => {
    expect(formatAcceleratorKeys('Alt+Shift+K', false)).toEqual(['Alt', 'Shift', 'K'])
    expect(formatAcceleratorKeys('Alt+Shift+K', true)).toEqual(['\u2325', '\u21e7', 'K'])
  })

  it('treats every Electron modifier ALIAS as a modifier', () => {
    expect(formatAcceleratorKeys('CmdOrCtrl+Option+j', true)).toEqual(['\u2318', '\u2325', 'J'])
    expect(formatAcceleratorKeys('Ctrl+Alt+p', false)).toEqual(['Ctrl', 'Alt', 'P'])
  })

  it('uppercases single-character keys and keeps named keys verbatim', () => {
    expect(formatAcceleratorKeys('Alt+Shift+Space', false)).toEqual(['Alt', 'Shift', 'Space'])
    expect(formatAcceleratorKeys('Alt+F12', false)).toEqual(['Alt', 'F12'])
    // AltGr has no table row on purpose: its display form is the token itself.
    expect(formatAcceleratorKeys('AltGr+Shift+K', false)).toEqual(['AltGr', 'Shift', 'K'])
  })

  it('returns [] for an unbound accelerator so callers can hide the row', () => {
    expect(formatAcceleratorKeys('', true)).toEqual([])
    expect(formatAcceleratorKeys('', false)).toEqual([])
  })
})
