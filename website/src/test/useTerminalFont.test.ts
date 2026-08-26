import { describe, it, expect, beforeEach } from 'vitest'
import {
  resolveTerminalFontFamily,
  getTerminalFont,
  setTerminalFontFamily,
  setTerminalFontSize,
  resetTerminalFont,
  __resetTerminalFontStore,
  DEFAULT_TERMINAL_FONT_FAMILY,
  DEFAULT_TERMINAL_FONT_SIZE,
  MIN_TERMINAL_FONT_SIZE,
  MAX_TERMINAL_FONT_SIZE,
} from '../hooks/useTerminalFont'

beforeEach(() => { __resetTerminalFontStore() })

describe('resolveTerminalFontFamily', () => {
  it('falls back to the default stack for empty / whitespace input', () => {
    expect(resolveTerminalFontFamily('')).toBe(DEFAULT_TERMINAL_FONT_FAMILY)
    expect(resolveTerminalFontFamily('   ')).toBe(DEFAULT_TERMINAL_FONT_FAMILY)
  })

  it('quotes a multi-word family (the Nerd Font case) and appends a monospace fallback', () => {
    expect(resolveTerminalFontFamily('JetBrainsMonoNL Nerd Font Mono'))
      .toBe("'JetBrainsMonoNL Nerd Font Mono', monospace")
  })

  it('leaves a single-token family unquoted and still adds the fallback', () => {
    expect(resolveTerminalFontFamily('Menlo')).toBe('Menlo, monospace')
  })

  it('does not double the monospace fallback when it is already present', () => {
    expect(resolveTerminalFontFamily('Fira Code, monospace')).toBe("'Fira Code', monospace")
  })

  it('preserves already-quoted tokens across a comma list', () => {
    expect(resolveTerminalFontFamily("'Cascadia Code', Menlo"))
      .toBe("'Cascadia Code', Menlo, monospace")
  })
})

describe('terminal font store', () => {
  it('starts at the defaults', () => {
    expect(getTerminalFont()).toEqual({ fontFamily: '', fontSize: DEFAULT_TERMINAL_FONT_SIZE })
  })

  it('sets the family and persists it to localStorage', () => {
    setTerminalFontFamily('Hack Nerd Font')
    expect(getTerminalFont().fontFamily).toBe('Hack Nerd Font')
    const persisted = JSON.parse(localStorage.getItem('mc-terminal-font') || '{}')
    expect(persisted.fontFamily).toBe('Hack Nerd Font')
  })

  it('clamps font size to the allowed bounds', () => {
    setTerminalFontSize(MAX_TERMINAL_FONT_SIZE + 50)
    expect(getTerminalFont().fontSize).toBe(MAX_TERMINAL_FONT_SIZE)
    setTerminalFontSize(MIN_TERMINAL_FONT_SIZE - 50)
    expect(getTerminalFont().fontSize).toBe(MIN_TERMINAL_FONT_SIZE)
  })

  it('rounds a fractional font size', () => {
    setTerminalFontSize(15.6)
    expect(getTerminalFont().fontSize).toBe(16)
  })

  it('reset restores the defaults', () => {
    setTerminalFontFamily('X')
    setTerminalFontSize(20)
    resetTerminalFont()
    expect(getTerminalFont()).toEqual({ fontFamily: '', fontSize: DEFAULT_TERMINAL_FONT_SIZE })
  })
})
