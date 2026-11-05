/**
 * `components/themeEditor.tsx` — what a screen reader and a non-English reader
 * get from the custom-theme editor.
 *
 *  - Every JSON-mode refusal and the empty-name refusal reads from the catalog,
 *    so a German UI shows German text, and lands in the hint, not the error.
 *  - A JSON value that parses but is no object (`null`, a number, an array)
 *    gets the "needs a name" refusal, never a raw `TypeError` message.
 *  - Each colour-group header says whether its group is open and names the
 *    region it opens; the dark and light editors never share a region id.
 *  - The three mode buttons say which one is selected.
 *
 * Seams are the same two the coverage suite mocks: `useTheme` (the theme-store
 * writes) and `api` (the edit-path calls). `i18nT` runs for real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'

import { i18next } from '../i18n/all'

const themeStore = vi.hoisted(() => ({
  addCustomTheme: vi.fn<(data: { name: string }) => Promise<unknown>>(),
  deleteCustomTheme: vi.fn<(slug: string) => Promise<void>>(),
  loadCustomThemes: vi.fn<() => Promise<void>>(),
}))

vi.mock('../hooks/useTheme', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useTheme')>()
  return { ...actual, useTheme: () => themeStore as unknown as ReturnType<typeof actual.useTheme> }
})

vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, themeDetail: vi.fn(), updateTheme: vi.fn() } }
})

import { ColorModeEditor, ThemeEditorPanel, useThemeEditor } from '../components/themeEditor'

beforeEach(() => {
  vi.clearAllMocks()
  themeStore.addCustomTheme.mockResolvedValue({ slug: 'created' })
  themeStore.loadCustomThemes.mockResolvedValue()
})

afterEach(async () => {
  await i18next.changeLanguage('en')
})

/** Run one save in JSON mode with `text` and return the hint it left. */
async function jsonSaveError(text: string): Promise<string> {
  const { result } = renderHook(() => useThemeEditor())
  act(() => { result.current.setCreatorMode('json'); result.current.setJsonText(text) })
  await act(async () => { await result.current.saveTheme() })
  // A refusal from the editor's own checks is a hint, never an error.
  expect(result.current.error).toBe('')
  return result.current.hint
}

describe('theme editor refusals read from the catalog', () => {
  it('shows each JSON refusal in the active language', async () => {
    await i18next.changeLanguage('de')

    expect(await jsonSaveError('{ nope')).toBe(i18next.t('components.themeEditor.json_invalid'))
    expect(await jsonSaveError('{"dark":{},"light":{}}')).toBe(i18next.t('components.themeEditor.json_needs_name'))
    expect(await jsonSaveError('{"name":"X","dark":{}}')).toBe(i18next.t('components.themeEditor.json_needs_modes'))
    expect(i18next.t('components.themeEditor.json_invalid')).toMatch(/^Ungültiges JSON/)
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })

  it('shows the empty-name refusal in the active language', async () => {
    await i18next.changeLanguage('de')
    const { result } = renderHook(() => useThemeEditor())
    act(() => { result.current.openNewTheme(); result.current.setThemeName('   ') })
    await act(async () => { await result.current.saveTheme() })

    expect(result.current.hint).toBe('Der Theme-Name ist erforderlich.')
    expect(result.current.error).toBe('')
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })

  it('gives a hint, not a silent reject, when pasted JSON carries a non-string name into the picker', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => { result.current.openNewTheme() })
    act(() => { result.current.switchMode('json') })
    act(() => { result.current.setJsonText('{"name":42,"emoji":7,"dark":"x","light":[]}') })
    act(() => { result.current.switchMode('picker') })

    expect(result.current.themeName).toBe('')
    expect(result.current.themeEmoji).toBe('✨')
    await act(async () => { await result.current.saveTheme() })
    expect(result.current.hint).toBe('Theme name is required.')
    expect(result.current.error).toBe('')
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })

  it.each([
    ['{"name":42,"dark":{},"light":{}}', 'JSON must include a "name" field.'],
    ['{"name":"   ","dark":{},"light":{}}', 'JSON must include a "name" field.'],
    ['{"name":"X","dark":"x","light":{}}', 'JSON must include "dark" and "light" objects.'],
    ['{"name":"X","dark":{},"light":[]}', 'JSON must include "dark" and "light" objects.'],
  ])('refuses %s with a hint before any request', async (text, hint) => {
    expect(await jsonSaveError(text)).toBe(hint)
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })

  it.each(['null', '42', '"text"', '[]'])('refuses %s as JSON with no name, not a TypeError', async text => {
    expect(await jsonSaveError(text)).toBe('JSON must include a "name" field.')
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })
})

describe('colour group headers carry their open state', () => {
  it('reports collapsed, then expanded, then collapsed, and names the open region', () => {
    render(<ColorModeEditor label="Dark Mode Colors" vars={{ '--bg': '#111111' }} onChange={vi.fn()} />)
    const header = screen.getByRole('button', { name: /Backgrounds/ })

    expect(header).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    const region = document.getElementById(header.getAttribute('aria-controls')!)
    expect(region).not.toBeNull()
    expect(region).toContainElement(screen.getByLabelText('Background color picker'))

    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps the arrow out of the accessible name', () => {
    render(<ColorModeEditor label="Dark Mode Colors" vars={{}} onChange={vi.fn()} />)
    const header = screen.getByRole('button', { name: /Backgrounds/ })

    expect(header.textContent).not.toMatch(/[▲▼]/)
    for (const svg of header.querySelectorAll('svg')) expect(svg).toHaveAttribute('aria-hidden', 'true')
  })

  it('gives the dark and light editors different region ids', () => {
    render(
      <>
        <ColorModeEditor label="Dark Mode Colors" vars={{}} onChange={vi.fn()} />
        <ColorModeEditor label="Light Mode Colors" vars={{}} onChange={vi.fn()} />
      </>,
    )
    const [dark, light] = screen.getAllByRole('button', { name: /Backgrounds/ })

    expect(dark.getAttribute('aria-controls')).not.toBe(light.getAttribute('aria-controls'))
  })
})

describe('mode buttons carry their selected state', () => {
  function Harness() {
    const editor = useThemeEditor()
    return (
      <div>
        <button data-testid="open-new" onClick={editor.openNewTheme}>new</button>
        {editor.editorOpen && <ThemeEditorPanel editor={editor} />}
      </div>
    )
  }

  it('marks exactly the active mode as pressed', () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('open-new'))
    const picker = screen.getByRole('button', { name: /Color Picker/ })
    const json = screen.getByRole('button', { name: /Paste JSON/ })
    const css = screen.getByRole('button', { name: /Paste CSS/ })

    expect([picker, json, css].map(b => b.getAttribute('aria-pressed'))).toEqual(['true', 'false', 'false'])
    fireEvent.click(json)
    expect([picker, json, css].map(b => b.getAttribute('aria-pressed'))).toEqual(['false', 'true', 'false'])
    fireEvent.click(css)
    expect([picker, json, css].map(b => b.getAttribute('aria-pressed'))).toEqual(['false', 'false', 'true'])
  })
})
