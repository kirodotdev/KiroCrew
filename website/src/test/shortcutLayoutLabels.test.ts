/**
 * Chord labels follow the user's keyboard layout (#38429).
 *
 * Every chord is matched by PHYSICAL key (`KeyboardEvent.code`), so its label
 * must name what that key prints on the user's layout, read from
 * `navigator.keyboard.getLayoutMap()`. Without the API (Firefox, Safari, an
 * insecure origin) the label stays the US glyph it always was.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { createElement } from 'react'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { keyCapGlyph, loadKeyboardLayout } from '../lib/shortcutRegistry'
import { formatChordCaps, formatShortcut, jumpLabelFor, resolveShortcutDef } from '../hooks/useKeyboardShortcuts'
import { formatChordKeys } from '../lib/quickSearchShortcut'
import { ariaKeyshortcutsFor, useNavShortcutHint } from '../hooks/useNavShortcutHint'
import { useActionShortcutHint } from '../hooks/useActionShortcutHint'
import ShortcutsModal from '../components/ShortcutsModal'

const AZERTY = new Map([
  ['BracketLeft', '^'], ['BracketRight', '$'], ['Digit1', '&'], ['Digit2', 'é'],
  ['KeyA', 'q'], ['Backquote', '²'],
])

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
    expect(label('cycle-agent')).toBe('Alt + Shift + Q')
    expect(label('chat-mru')).toBe('Alt + ²')
    expect(jumpLabelFor(0)).toBe('&')
    expect(jumpLabelFor(1)).toBe('é')
    expect(formatChordCaps({ key: '[', mod: true })).toEqual(['Ctrl', '^'])
    expect(formatChordKeys({ key: ']', mod: true }, false)).toEqual(['Ctrl', '$'])
    expect(formatChordKeys({ key: 'a', alt: true }, true)).toEqual(['⌥', 'Q'])

    const cycle = resolveShortcutDef('cycle-agent')!
    expect(ariaKeyshortcutsFor(cycle, false)).toBe('Alt+Shift+Q')
    expect(ariaKeyshortcutsFor(resolveShortcutDef('chat-next-bracket')!, true)).toBe('Meta+$')
  })

  it('keeps a letter whose upper case would read as another key', async () => {
    stubLayout(() => Promise.resolve(new Map([['KeyW', 'ς'], ['KeyS', 'σ'], ['KeyI', 'ı']])))
    await loadKeyboardLayout()

    expect(keyCapGlyph('w')).toBe('ς')
    expect(keyCapGlyph('s')).toBe('Σ')
    expect(keyCapGlyph('i')).toBe('ı')
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

    // Dvorak prints `j` at the US `c` position.
    stubLayout(() => Promise.resolve(new Map([['KeyC', 'j']])))
    await act(() => loadKeyboardLayout())

    expect(result.current?.chord).toBe('Alt + J')
    expect(result.current?.ariaKeyshortcuts).toBe('Alt+J')
    unmount()
  })

  it('drops a tooltip whose catalog chord names another key on this layout', async () => {
    const { result, unmount } = renderHook(() => useActionShortcutHint('cycle-agent'))
    expect(result.current).toEqual({ ariaKeyshortcuts: 'Alt+Shift+A', isFactory: true })

    stubLayout(() => Promise.resolve(AZERTY))
    await act(() => loadKeyboardLayout())

    expect(result.current).toEqual({ ariaKeyshortcuts: 'Alt+Shift+Q', isFactory: false })
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
