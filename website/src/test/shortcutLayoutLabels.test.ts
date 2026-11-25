/**
 * Chord labels follow the user's keyboard layout.
 *
 * A letter chord matches the letter the layout types, so its label is that
 * letter. A chord matched by PHYSICAL key (punctuation, digits, a macOS Option
 * chord, a letter no key of the layout types) is labelled with what that key
 * prints on the user's layout, read from `navigator.keyboard.getLayoutMap()`.
 * Without the API (Firefox, Safari, an insecure origin) the label is the US glyph.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { createElement } from 'react'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { keyCapGlyph, loadKeyboardLayout } from '../lib/shortcutRegistry'
import { formatChordCaps, formatShortcut, jumpLabelFor, resolveShortcutDef } from '../hooks/useKeyboardShortcuts'
import { chordReachable, formatChordKeys } from '../lib/quickSearchShortcut'
import { ariaKeyshortcutsFor, useNavShortcutHint } from '../hooks/useNavShortcutHint'
import { useActionShortcutHint } from '../hooks/useActionShortcutHint'
import ShortcutsModal, { displayChords, PANEL_TOGGLE_LABEL_KEY } from '../components/ShortcutsModal'
import { reclickPanelTitle } from '../pages/chat-sidebar/chrome'
import { i18nT } from '../i18n/t'

const AZERTY = new Map([
  ['BracketLeft', '^'], ['BracketRight', '$'], ['Digit1', '&'], ['Digit2', 'é'],
  ['KeyA', 'q'], ['KeyQ', 'a'], ['Backquote', '²'],
])
// No key of a Russian layout types a Latin letter.
const RUSSIAN = new Map([['KeyA', 'ф'], ['KeyC', 'с']])

function stubLayout(getLayoutMap: (() => Promise<ReadonlyMap<string, string>>) | undefined): void {
  Object.defineProperty(navigator, 'keyboard', {
    configurable: true,
    value: getLayoutMap ? { getLayoutMap } : undefined,
  })
}

function label(id: string): string {
  const def = resolveShortcutDef(id)
  if (!def) throw new Error(`no shortcut ${id}`)
  return formatShortcut(def)
}

afterEach(async () => {
  delete (navigator as { keyboard?: unknown }).keyboard
  await loadKeyboardLayout()
})

describe('shortcut labels on a non-US layout', () => {
  it('name the key the layout prints at each chord position', async () => {
    stubLayout(() => Promise.resolve(AZERTY))
    await loadKeyboardLayout()

    expect(label('chat-prev-bracket')).toBe('Ctrl + ^')
    expect(label('chat-next-bracket')).toBe('Ctrl + $')
    expect(label('chat-1')).toBe('Alt + &')
    expect(label('chat-2')).toBe('Alt + é')
    expect(label('cycle-agent')).toBe('Alt + Shift + A')
    expect(label('chat-mru')).toBe('Alt + ²')
    expect(jumpLabelFor(0)).toBe('&')
    expect(jumpLabelFor(1)).toBe('é')
    // The 18th session's letter is q: typed on AZERTY's A key, and the macOS Option jumps keep the glyph at Q.
    expect(jumpLabelFor(17, undefined, false)).toBe('q')
    expect(jumpLabelFor(17, undefined, true)).toBe('a')
    expect(formatChordCaps({ key: '[', mod: true })).toEqual(['Ctrl', '^'])
    expect(formatChordKeys({ key: ']', mod: true }, false)).toEqual(['Ctrl', '$'])
    // A macOS Option chord is matched by position, so it keeps the glyph there.
    expect(formatChordKeys({ key: 'a', alt: true }, true)).toEqual(['⌥', 'Q'])
    expect(formatChordKeys({ key: 'a', alt: true }, false)).toEqual(['Alt', 'A'])

    const cycle = resolveShortcutDef('cycle-agent')!
    expect(ariaKeyshortcutsFor(cycle, false)).toBe('Alt+Shift+A')
    expect(ariaKeyshortcutsFor(cycle, true)).toBe('Alt+Shift+Q')
    expect(ariaKeyshortcutsFor(resolveShortcutDef('chat-next-bracket')!, true)).toBe('Meta+$')
  })

  it('keeps a letter whose upper case would read as another key', async () => {
    stubLayout(() => Promise.resolve(new Map([['KeyW', 'ς'], ['KeyS', 'σ'], ['KeyI', 'ı']])))
    await loadKeyboardLayout()

    expect(keyCapGlyph('w')).toBe('ς')
    expect(keyCapGlyph('s')).toBe('Σ')
    expect(keyCapGlyph('i')).toBe('ı')
  })

  it('does not advertise a chord no key of the layout types, so its alias leads', async () => {
    // Dvorak: the US `/` and `,` positions type `z` and `w`; `/` is at BracketLeft, `,` at KeyW, `k` at KeyV.
    stubLayout(() => Promise.resolve(new Map([['Slash', 'z'], ['Comma', 'w'], ['BracketLeft', '/'], ['KeyW', ','], ['KeyV', 'k']])))
    await loadKeyboardLayout()

    expect(displayChords(resolveShortcutDef('shortcuts-modal')!).primary).toEqual(['Alt', 'K'])
    expect(displayChords(resolveShortcutDef('open-settings')!).primary).toEqual([])
    expect(displayChords(resolveShortcutDef('chat-prev-bracket')!).primary).toEqual(['Ctrl', '/'])
  })

  it('shows a panel toggle no key of the layout types as not set', async () => {
    // Turkish F types `x` at the US backslash position, and no key types `\` without AltGr.
    stubLayout(() => Promise.resolve(new Map([['Backslash', 'x'], ['Comma', 'b']])))
    await loadKeyboardLayout()

    expect(reclickPanelTitle()).toBe(i18nT('pages.chatSidebar.reclick_toggles_side_panel'))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      createElement(QueryClientProvider, { client }, createElement(ShortcutsModal, { onClose: () => {} })),
    )
    const row = screen.getByText(i18nT(PANEL_TOGGLE_LABEL_KEY['side-panel'])).parentElement!
    expect(row).toHaveTextContent(i18nT('components.shortcutsModal.unset'))
    unmount()
    // A key the positional reader has no token for (the ISO key's `<`, recorded by what it types) stays shown.
    expect(chordReachable({ key: '<', mod: true }, false)).toBe(true)
  })

  it.each([
    ['the API is missing', undefined],
    ['the API rejects', () => Promise.reject(new Error('not allowed'))],
  ])('keep the US glyph when %s', async (_name, getLayoutMap) => {
    stubLayout(getLayoutMap)
    await loadKeyboardLayout()

    expect(label('chat-prev-bracket')).toBe('Ctrl + [')
    expect(label('chat-next-bracket')).toBe('Ctrl + ]')
    expect(label('chat-1')).toBe('Alt + 1')
    expect(label('cycle-agent')).toBe('Alt + Shift + A')
    expect(label('chat-mru')).toBe('Alt + `')
    expect(jumpLabelFor(0)).toBe('1')
    expect(formatChordKeys({ key: ']', mod: true }, false)).toEqual(['Ctrl', ']'])
    expect(ariaKeyshortcutsFor(resolveShortcutDef('cycle-agent')!, false)).toBe('Alt+Shift+A')
  })

  it('updates a rail hint rendered before the layout arrived', async () => {
    const { result, unmount } = renderHook(() => useNavShortcutHint('/chat'))
    expect(result.current?.chord).toBe('Alt + C')

    stubLayout(() => Promise.resolve(RUSSIAN))
    await act(() => loadKeyboardLayout())

    expect(result.current?.chord).toBe('Alt + С')
    expect(result.current?.ariaKeyshortcuts).toBe('Alt+С')
    unmount()
  })

  it('drops a tooltip whose catalog chord names another key on this layout', async () => {
    const { result, unmount } = renderHook(() => useActionShortcutHint('cycle-agent'))
    expect(result.current).toEqual({ ariaKeyshortcuts: 'Alt+Shift+A', isFactory: true })

    stubLayout(() => Promise.resolve(RUSSIAN))
    await act(() => loadKeyboardLayout())

    expect(result.current).toEqual({ ariaKeyshortcuts: 'Alt+Shift+Ф', isFactory: false })
    unmount()
  })

  it('re-runs a shortcuts search typed before the layout arrived', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      createElement(QueryClientProvider, { client }, createElement(ShortcutsModal, { onClose: () => {} })),
    )
    // US glyphs label Previous chat `Ctrl + [`, which `^` does not match.
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '^' } })
    expect(screen.queryByText('Previous chat')).not.toBeInTheDocument()

    stubLayout(() => Promise.resolve(AZERTY))
    await act(() => loadKeyboardLayout())

    expect(screen.getByText('Previous chat')).toBeInTheDocument()
    unmount()
  })
})
