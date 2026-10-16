import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  resolveTerminalFontFamily,
  getTerminalFont,
  setTerminalFontFamily,
  setTerminalFontSize,
  setTerminalCursorStyle,
  setTerminalCopyOnSelect,
  resetTerminalFont,
  __resetTerminalFontStore,
  DEFAULT_TERMINAL_FONT_FAMILY,
  DEFAULT_TERMINAL_FONT_SIZE,
  DEFAULT_TERMINAL_CURSOR_STYLE,
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
    expect(getTerminalFont()).toEqual({
      fontFamily: '',
      fontSize: DEFAULT_TERMINAL_FONT_SIZE,
      cursorStyle: DEFAULT_TERMINAL_CURSOR_STYLE,
      copyOnSelect: false,
    })
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
    setTerminalCursorStyle('bar')
    setTerminalCopyOnSelect(true)
    resetTerminalFont()
    expect(getTerminalFont()).toEqual({
      fontFamily: '',
      fontSize: DEFAULT_TERMINAL_FONT_SIZE,
      cursorStyle: DEFAULT_TERMINAL_CURSOR_STYLE,
      copyOnSelect: false,
    })
  })
})

describe('terminal cursor style', () => {
  it('defaults to block', () => {
    expect(DEFAULT_TERMINAL_CURSOR_STYLE).toBe('block')
    expect(getTerminalFont().cursorStyle).toBe('block')
  })

  it('sets the cursor style and persists it to localStorage', () => {
    setTerminalCursorStyle('bar')
    expect(getTerminalFont().cursorStyle).toBe('bar')
    const persisted = JSON.parse(localStorage.getItem('mc-terminal-font') || '{}')
    expect(persisted.cursorStyle).toBe('bar')
  })

  it('accepts underline', () => {
    setTerminalCursorStyle('underline')
    expect(getTerminalFont().cursorStyle).toBe('underline')
  })

  it('ignores an invalid persisted cursor style, falling back to the default on load', async () => {
    localStorage.setItem('mc-terminal-font', JSON.stringify({ cursorStyle: 'diamond' }))
    vi.resetModules()
    const fresh = await import('../hooks/useTerminalFont')
    expect(fresh.getTerminalFont().cursorStyle).toBe('block')
  })

  it('loads a valid persisted cursor style on a fresh module load', async () => {
    localStorage.setItem('mc-terminal-font', JSON.stringify({ cursorStyle: 'underline' }))
    vi.resetModules()
    const fresh = await import('../hooks/useTerminalFont')
    expect(fresh.getTerminalFont().cursorStyle).toBe('underline')
  })

  it('leaves the font family and size untouched when only the cursor changes', () => {
    setTerminalFontFamily('Menlo')
    setTerminalFontSize(18)
    setTerminalCursorStyle('underline')
    const s = getTerminalFont()
    expect(s.fontFamily).toBe('Menlo')
    expect(s.fontSize).toBe(18)
    expect(s.cursorStyle).toBe('underline')
  })
})

describe('terminal copy on select', () => {
  it('is off by default', () => {
    expect(getTerminalFont().copyOnSelect).toBe(false)
  })

  it('turns on and persists to localStorage', () => {
    setTerminalCopyOnSelect(true)
    expect(getTerminalFont().copyOnSelect).toBe(true)
    const persisted = JSON.parse(localStorage.getItem('mc-terminal-font') || '{}')
    expect(persisted.copyOnSelect).toBe(true)
  })

  it('loads a persisted true on a fresh module load', async () => {
    localStorage.setItem('mc-terminal-font', JSON.stringify({ copyOnSelect: true }))
    vi.resetModules()
    const fresh = await import('../hooks/useTerminalFont')
    expect(fresh.getTerminalFont().copyOnSelect).toBe(true)
  })

  it('reads anything but a literal true as off', async () => {
    for (const value of ['true', 1, 'yes', null]) {
      localStorage.setItem('mc-terminal-font', JSON.stringify({ copyOnSelect: value }))
      vi.resetModules()
      const fresh = await import('../hooks/useTerminalFont')
      expect(fresh.getTerminalFont().copyOnSelect, JSON.stringify(value)).toBe(false)
    }
  })

  it('leaves the font and cursor untouched', () => {
    setTerminalFontFamily('Menlo')
    setTerminalCursorStyle('bar')
    setTerminalCopyOnSelect(true)
    const s = getTerminalFont()
    expect(s.fontFamily).toBe('Menlo')
    expect(s.cursorStyle).toBe('bar')
  })
})
