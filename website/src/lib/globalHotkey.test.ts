import { describe, expect, it } from 'vitest'
import { formatAcceleratorKeys } from './globalHotkey'

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
