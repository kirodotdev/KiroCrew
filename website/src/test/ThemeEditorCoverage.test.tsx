/**
 * `components/themeEditor.tsx` — first tests for the custom-theme authoring
 * surface (the `useThemeEditor` state machine, the two colour widgets, and the
 * panel that wires them together).
 *
 * Two seams, both mocked, and nothing else: `useTheme` (the three theme-store
 * writes the editor performs) and `api` (the two calls that only exist on the
 * EDIT path). Everything else runs for real, including `i18nT`, because the
 * group headings and per-variable labels ARE the catalog keys resolved at render
 * time — a stub would prove nothing about what the user reads.
 *
 * The behaviours worth pinning are the ones a user can silently lose work to:
 *
 *  - which save path a click takes: create (`addCustomTheme`) versus update
 *    (`api.updateTheme` + `loadCustomThemes` + the change event that makes every
 *    other mounted consumer re-read), since taking the wrong one on an edit
 *    forks the theme instead of saving it;
 *  - every validation refusal in JSON mode, each of which must surface as text
 *    rather than a swallowed throw;
 *  - the picker/JSON hand-off in BOTH directions — a tab switch that dropped the
 *    other pane's edits would discard typing the user can still see;
 *  - delete, which is `confirm`-gated and must write nothing when declined.
 *
 * Fake timers are armed purely as a teardown guard: nothing in this tree
 * schedules one today, and an unhandled late callback would redden the run while
 * every test still reported as passing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'

import type { CustomThemeData } from '../hooks/useTheme'

/* ── seams ────────────────────────────────────────────────────── */

const themeStore = vi.hoisted(() => ({
  addCustomTheme: vi.fn<(data: { name: string }) => Promise<unknown>>(),
  deleteCustomTheme: vi.fn<(slug: string) => Promise<void>>(),
  loadCustomThemes: vi.fn<() => Promise<void>>(),
}))

vi.mock('../hooks/useTheme', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useTheme')>()
  return { ...actual, useTheme: () => themeStore as unknown as ReturnType<typeof actual.useTheme> }
})

const apiMocks = vi.hoisted(() => ({
  themeDetail: vi.fn<(slug: string) => Promise<Partial<CustomThemeData>>>(),
  updateTheme: vi.fn<(slug: string, body: object) => Promise<unknown>>(),
}))

vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, ...apiMocks } }
})

import {
  ColorModeEditor,
  ColorRow,
  ThemeEditorPanel,
  VAR_GROUPS,
  getCurrentThemeVars,
  toHex,
  useThemeEditor,
} from '../components/themeEditor'
import { CUSTOM_THEMES_CHANGED_EVENT } from '../hooks/useTheme'

/* ── fixtures ─────────────────────────────────────────────────── */

/** A theme as `api.themeDetail` hands one back for the edit path. */
const OCEAN: Partial<CustomThemeData> = {
  name: 'Ocean',
  emoji: '🌊',
  dark: { '--bg': '#0b1220', '--accent': '#3b82f6' },
  light: { '--bg': '#f8fafc', '--accent': '#2563eb' },
}

/** Minimum shape the JSON pane accepts: a name plus both mode maps. */
const VALID_JSON = JSON.stringify({
  name: 'Pasted',
  dark: { '--bg': '#000000' },
  light: { '--bg': '#ffffff' },
})

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.clearAllMocks()
  themeStore.addCustomTheme.mockResolvedValue({ slug: 'created' })
  themeStore.deleteCustomTheme.mockResolvedValue()
  themeStore.loadCustomThemes.mockResolvedValue()
  apiMocks.themeDetail.mockResolvedValue(OCEAN)
  apiMocks.updateTheme.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

/* ── harness ──────────────────────────────────────────────────── */

/**
 * Drives the real hook the way both call sites do: a trigger that opens a blank
 * theme, a trigger that loads one for editing, and the panel rendered only while
 * the editor is open (so a close is observable as the panel disappearing).
 */
function Harness({ slug = 'ocean' }: { slug?: string }) {
  const editor = useThemeEditor()
  return (
    <div>
      <button data-testid="open-new" onClick={editor.openNewTheme}>new</button>
      <button data-testid="open-edit" onClick={() => void editor.openEditTheme(slug)}>edit</button>
      {editor.editorOpen && <ThemeEditorPanel editor={editor} />}
    </div>
  )
}

/** Mount the harness and open a blank theme. */
function openNew() {
  const utils = render(<Harness />)
  fireEvent.click(screen.getByTestId('open-new'))
  return utils
}

/** Mount the harness and let the edit read settle. */
async function openEdit(slug = 'ocean') {
  const utils = render(<Harness slug={slug} />)
  fireEvent.click(screen.getByTestId('open-edit'))
  await waitFor(() => expect(apiMocks.themeDetail).toHaveBeenCalled())
  return utils
}

/** The one button whose accessible name is exactly `name`. */
function btn(name: string | RegExp): HTMLElement {
  return screen.getByRole('button', { name })
}

/* ── pure helpers ─────────────────────────────────────────────── */

describe('toHex', () => {
  it('returns opaque black for an empty value', () => {
    expect(toHex('')).toBe('#000000')
  })

  it('passes a six-digit hex through untouched', () => {
    expect(toHex('#AABBCC')).toBe('#AABBCC')
  })

  it('expands a three-digit hex to six digits', () => {
    expect(toHex('#abc')).toBe('#aabbcc')
  })

  it('resolves a non-hex colour through the computed style', () => {
    vi.spyOn(window, 'getComputedStyle').mockReturnValue(
      { color: 'rgb(1, 2, 250)' } as unknown as CSSStyleDeclaration,
    )
    expect(toHex('rebeccapurple')).toBe('#0102fa')
  })

  it('falls back to black when the computed colour is not rgb', () => {
    vi.spyOn(window, 'getComputedStyle').mockReturnValue(
      { color: 'oklch(0.7 0.1 200)' } as unknown as CSSStyleDeclaration,
    )
    expect(toHex('var(--nope)')).toBe('#000000')
  })

  it('falls back to black when resolving throws, leaving no probe node behind', () => {
    vi.spyOn(window, 'getComputedStyle').mockImplementation(() => { throw new Error('no layout') })
    const before = document.body.childElementCount
    expect(toHex('color-mix(in srgb, red, blue)')).toBe('#000000')
    expect(document.body.childElementCount).toBe(before)
  })
})

describe('getCurrentThemeVars', () => {
  it('reads every grouped variable plus the ungrouped extras', () => {
    const vars = getCurrentThemeVars()
    for (const group of VAR_GROUPS) {
      for (const key of group.vars) expect(vars).toHaveProperty(key)
    }
    expect(vars).toHaveProperty('--shadow-lg')
    expect(vars).toHaveProperty('--diff-add')
    expect(Object.values(vars).every(v => typeof v === 'string')).toBe(true)
  })

  it('returns the trimmed value the document actually carries', () => {
    vi.spyOn(window, 'getComputedStyle').mockReturnValue(
      { getPropertyValue: () => '  #123456  ' } as unknown as CSSStyleDeclaration,
    )
    expect(getCurrentThemeVars()['--bg']).toBe('#123456')
  })
})

/* ── useThemeEditor ───────────────────────────────────────────── */

describe('useThemeEditor', () => {
  it('opens a blank theme seeded from the live CSS variables', () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => result.current.openNewTheme())

    expect(result.current.editorOpen).toBe(true)
    expect(result.current.isEditing).toBe(false)
    expect(result.current.creatorMode).toBe('picker')
    expect(result.current.themeName).toBe('')
    expect(result.current.themeEmoji).toBe('✨')
    expect(Object.keys(result.current.darkVars).length).toBeGreaterThan(0)
    expect(result.current.lightVars).toEqual(result.current.darkVars)
  })

  it('loads a theme for editing and closes back to a clean slate', async () => {
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.openEditTheme('ocean') })

    expect(result.current.isEditing).toBe(true)
    expect(result.current.editingSlug).toBe('ocean')
    expect(result.current.themeName).toBe('Ocean')
    expect(result.current.themeEmoji).toBe('🌊')
    expect(result.current.darkVars).toEqual(OCEAN.dark)
    expect(result.current.jsonText).toContain('"name": "Ocean"')

    act(() => result.current.closeEditor())
    expect(result.current.editorOpen).toBe(false)
    expect(result.current.editingSlug).toBeNull()
  })

  it('substitutes defaults for the fields a stored theme omits', async () => {
    apiMocks.themeDetail.mockResolvedValue({})
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.openEditTheme('bare') })

    expect(result.current.themeName).toBe('')
    expect(result.current.themeEmoji).toBe('🎨')
    expect(result.current.darkVars).toEqual({})
    expect(result.current.lightVars).toEqual({})
  })

  it('opens the editor with an error when the theme cannot be read', async () => {
    apiMocks.themeDetail.mockRejectedValue(new Error('404'))
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.openEditTheme('gone') })

    expect(result.current.error).toBe('Failed to load theme for editing')
    expect(result.current.editorOpen).toBe(true)
    expect(result.current.isEditing).toBe(false)
  })

  it('refuses to save a picker theme with no name', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => result.current.setThemeName('   '))
    await act(async () => { await result.current.saveTheme() })

    expect(result.current.error).toBe('Theme name is required.')
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
    expect(result.current.saving).toBe(false)
  })

  it('creates from the picker with a trimmed name and the default emoji', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => {
      result.current.setThemeName('  Sunset  ')
      result.current.setThemeEmoji('  ')
      result.current.updateDarkVar('--bg', '#101010')
      result.current.updateLightVar('--bg', '#fefefe')
    })
    await act(async () => { await result.current.saveTheme() })

    expect(themeStore.addCustomTheme).toHaveBeenCalledWith({
      name: 'Sunset',
      emoji: '✨',
      dark: { '--bg': '#101010' },
      light: { '--bg': '#fefefe' },
    })
    expect(result.current.editorOpen).toBe(false)
  })

  it('reports a non-Error save rejection through the catalog fallback', async () => {
    themeStore.addCustomTheme.mockRejectedValue('nope')
    const { result } = renderHook(() => useThemeEditor())
    act(() => result.current.setThemeName('Sunset'))
    await act(async () => { await result.current.saveTheme() })

    expect(result.current.error).toBe('Failed to save theme')
    expect(result.current.editorOpen).toBe(false)
  })

  it('rejects malformed JSON before any write', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => { result.current.setCreatorMode('json'); result.current.setJsonText('{ nope') })
    await act(async () => { await result.current.saveTheme() })

    expect(result.current.error).toBe('Invalid JSON — check syntax and try again.')
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })

  it('rejects JSON without a name', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => { result.current.setCreatorMode('json'); result.current.setJsonText('{"dark":{},"light":{}}') })
    await act(async () => { await result.current.saveTheme() })

    expect(result.current.error).toBe('JSON must include a "name" field.')
  })

  it('rejects JSON missing either mode map', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => { result.current.setCreatorMode('json'); result.current.setJsonText('{"name":"X","dark":{}}') })
    await act(async () => { await result.current.saveTheme() })

    expect(result.current.error).toBe('JSON must include "dark" and "light" objects.')
  })

  it('creates from valid JSON, defaulting the emoji', async () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => { result.current.setCreatorMode('json'); result.current.setJsonText(VALID_JSON) })
    await act(async () => { await result.current.saveTheme() })

    expect(themeStore.addCustomTheme).toHaveBeenCalledWith({
      name: 'Pasted',
      emoji: '✨',
      dark: { '--bg': '#000000' },
      light: { '--bg': '#ffffff' },
    })
  })

  it('updates an existing theme from JSON and announces the change', async () => {
    const seen = vi.fn()
    window.addEventListener(CUSTOM_THEMES_CHANGED_EVENT, seen)
    try {
      const { result } = renderHook(() => useThemeEditor())
      await act(async () => { await result.current.openEditTheme('ocean') })
      act(() => { result.current.setCreatorMode('json'); result.current.setJsonText(VALID_JSON) })
      await act(async () => { await result.current.saveTheme() })

      expect(apiMocks.updateTheme).toHaveBeenCalledWith('ocean', {
        name: 'Pasted',
        emoji: '🎨',
        dark: { '--bg': '#000000' },
        light: { '--bg': '#ffffff' },
      })
      expect(themeStore.loadCustomThemes).toHaveBeenCalled()
      expect(seen).toHaveBeenCalled()
      expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(CUSTOM_THEMES_CHANGED_EVENT, seen)
    }
  })

  it('does nothing on delete when no theme is targeted', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.handleDelete() })

    expect(confirmSpy).not.toHaveBeenCalled()
    expect(themeStore.deleteCustomTheme).not.toHaveBeenCalled()
  })

  it('writes nothing when the delete confirmation is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.handleDelete('ocean') })

    expect(themeStore.deleteCustomTheme).not.toHaveBeenCalled()
  })

  it('surfaces a failed delete instead of closing the editor', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    themeStore.deleteCustomTheme.mockRejectedValue(new Error('in use'))
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.openEditTheme('ocean') })
    await act(async () => { await result.current.handleDelete() })

    expect(result.current.error).toBe('in use')
    expect(result.current.editorOpen).toBe(true)
  })

  it('reports a non-Error delete rejection through the catalog fallback', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    themeStore.deleteCustomTheme.mockRejectedValue('boom')
    const { result } = renderHook(() => useThemeEditor())
    await act(async () => { await result.current.handleDelete('ocean') })

    expect(result.current.error).toBe('Failed to delete theme')
  })

  it('adopts only the fields present in pasted JSON and ignores invalid text', () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => result.current.syncJsonToPicker('{"name":"Half","dark":{"--bg":"#010101"}}'))

    expect(result.current.themeName).toBe('Half')
    expect(result.current.themeEmoji).toBe('✨')
    expect(result.current.darkVars).toEqual({ '--bg': '#010101' })
    expect(result.current.lightVars).toEqual({})

    act(() => result.current.syncJsonToPicker('not json'))
    expect(result.current.themeName).toBe('Half')
  })

  it('projects the picker to JSON only once there is something to project', () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => result.current.switchMode('json'))
    expect(result.current.jsonText).toBe('')
    act(() => result.current.switchMode('picker'))

    act(() => { result.current.setThemeName('Named'); result.current.setThemeEmoji('') })
    act(() => result.current.switchMode('json'))
    expect(JSON.parse(result.current.jsonText)).toEqual({
      name: 'Named', emoji: '✨', dark: {}, light: {},
    })
    act(() => result.current.switchMode('picker'))

    act(() => { result.current.setThemeName(''); result.current.updateDarkVar('--bg', '#020202') })
    act(() => result.current.switchMode('json'))
    expect(JSON.parse(result.current.jsonText).name).toBe('My Theme')
  })

  it('keeps the picker as it was when leaving the JSON tab on text that does not parse', () => {
    const { result } = renderHook(() => useThemeEditor())
    act(() => result.current.setThemeName('Kept'))
    act(() => result.current.switchMode('json'))
    act(() => result.current.setJsonText('{"name":'))
    act(() => result.current.switchMode('picker'))

    expect(result.current.themeName).toBe('Kept')
    expect(result.current.creatorMode).toBe('picker')

    act(() => result.current.switchMode('json'))
    act(() => result.current.setJsonText('{"light":{"--bg":"#ffffff"}}'))
    act(() => result.current.switchMode('picker'))
    expect(result.current.themeName).toBe('Kept')
    expect(result.current.lightVars).toEqual({ '--bg': '#ffffff' })
    expect(result.current.darkVars).toEqual({})
  })
})

/* ── ColorRow ─────────────────────────────────────────────────── */

describe('ColorRow', () => {
  it('offers a native colour well for a simple colour and reports both edits', () => {
    const onChange = vi.fn()
    render(<ColorRow label="Background" value="#abc" onChange={onChange} />)

    const well = screen.getByLabelText('Background color picker') as HTMLInputElement
    expect(well.type).toBe('color')
    expect(well.value).toBe('#aabbcc')

    fireEvent.change(well, { target: { value: '#123456' } })
    expect(onChange).toHaveBeenLastCalledWith('#123456')

    fireEvent.change(screen.getByLabelText('Background'), { target: { value: 'salmon' } })
    expect(onChange).toHaveBeenLastCalledWith('salmon')
  })

  it('accepts a bare colour keyword as simple', () => {
    render(<ColorRow label="Accent" value="salmon" onChange={vi.fn()} />)
    expect(screen.getByLabelText('Accent color picker')).toBeInTheDocument()
  })

  it('falls back to a preview swatch for a value a colour well cannot hold', () => {
    render(<ColorRow label="Panel" value="linear-gradient(red, blue)" onChange={vi.fn()} />)

    expect(screen.queryByLabelText('Panel color picker')).toBeNull()
    expect(screen.getByLabelText('Panel')).toHaveValue('linear-gradient(red, blue)')
  })
})

/* ── ColorModeEditor ──────────────────────────────────────────── */

describe('ColorModeEditor', () => {
  const VARS = { '--bg': '#111111', '--text': '#eeeeee' }

  it('renders one collapsed accordion per variable group', () => {
    render(<ColorModeEditor label="Dark Mode Colors" vars={VARS} onChange={vi.fn()} />)

    expect(screen.getByText('Dark Mode Colors')).toBeInTheDocument()
    for (const heading of ['Backgrounds', 'Text & Muted', 'Borders', 'Accent', 'Status']) {
      // Regex, not an exact name: each accordion header's accessible name also
      // carries the five swatch previews and the ▲/▼ affordance beside it.
      expect(btn(new RegExp(heading))).toBeInTheDocument()
    }
    expect(screen.queryByLabelText('Background color picker')).toBeNull()
  })

  it('expands one group at a time and collapses it on a second click', () => {
    const onChange = vi.fn()
    render(<ColorModeEditor label="Dark Mode Colors" vars={VARS} onChange={onChange} />)

    fireEvent.click(btn(/Backgrounds/))
    expect(screen.getByLabelText('Background color picker')).toBeInTheDocument()
    expect(screen.getByLabelText('Panel Strong')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Bg Hover'), { target: { value: '#333333' } })
    expect(onChange).toHaveBeenCalledWith('--bg-hover', '#333333')

    fireEvent.click(btn(/Text & Muted/))
    expect(screen.queryByLabelText('Background color picker')).toBeNull()
    expect(screen.getByLabelText('Muted Strong')).toBeInTheDocument()

    fireEvent.click(btn(/Text & Muted/))
    expect(screen.queryByLabelText('Muted Strong')).toBeNull()
  })

  it('titles each swatch preview with the variable it stands for', () => {
    render(<ColorModeEditor label="Dark Mode Colors" vars={VARS} onChange={vi.fn()} />)
    const row = btn(/Backgrounds/)

    expect(within(row).getByTitle('Background: #111111')).toBeInTheDocument()
    expect(within(row).getByTitle('Card: ?')).toBeInTheDocument()
  })
})

/* ── ThemeEditorPanel ─────────────────────────────────────────── */

describe('ThemeEditorPanel', () => {
  it('opens on the picker with both mode editors and no destructive action', () => {
    openNew()

    expect(screen.getByLabelText('Theme Name')).toHaveValue('')
    expect(screen.getByLabelText('Emoji')).toHaveValue('✨')
    expect(screen.getByText('Dark Mode Colors')).toBeInTheDocument()
    expect(screen.getByText('Light Mode Colors')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Backgrounds/ })).toHaveLength(2)
    expect(btn('Save Theme')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete Theme/ })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('carries the picker into the JSON pane and the JSON edits back out', () => {
    openNew()

    fireEvent.change(screen.getByLabelText('Theme Name'), { target: { value: 'Draft' } })
    fireEvent.click(btn(/Paste JSON/))

    const area = screen.getByLabelText('Theme JSON') as HTMLTextAreaElement
    expect(JSON.parse(area.value).name).toBe('Draft')

    fireEvent.change(area, { target: { value: '{"name":"Renamed","emoji":"🌙"}' } })
    fireEvent.click(btn(/Color Picker/))

    expect(screen.getByLabelText('Theme Name')).toHaveValue('Renamed')
    expect(screen.getByLabelText('Emoji')).toHaveValue('🌙')
  })

  it('adopts JSON edits on blur without leaving the pane', () => {
    openNew()
    fireEvent.click(btn(/Paste JSON/))

    const area = screen.getByLabelText('Theme JSON')
    fireEvent.change(area, { target: { value: '{"name":"Blurred"}' } })
    fireEvent.blur(area)
    fireEvent.click(btn(/Color Picker/))

    expect(screen.getByLabelText('Theme Name')).toHaveValue('Blurred')
  })

  it('creates the theme and dismisses itself on save', async () => {
    openNew()
    fireEvent.change(screen.getByLabelText('Theme Name'), { target: { value: 'Fresh' } })
    fireEvent.change(screen.getByLabelText('Emoji'), { target: { value: '🌞' } })
    fireEvent.click(btn('Save Theme'))

    await waitFor(() => expect(themeStore.addCustomTheme).toHaveBeenCalled(), { timeout: 5_000 })
    expect(themeStore.addCustomTheme.mock.calls[0][0]).toMatchObject({ name: 'Fresh', emoji: '🌞' })
    await waitFor(() => expect(screen.queryByLabelText('Theme Name')).toBeNull())
  })

  it('shows the in-flight label and blocks a second save while one is running', async () => {
    let release = (): void => {}
    themeStore.addCustomTheme.mockImplementation(
      () => new Promise(resolve => { release = () => resolve({}) }),
    )

    openNew()
    fireEvent.change(screen.getByLabelText('Theme Name'), { target: { value: 'Slow' } })
    fireEvent.click(btn('Save Theme'))

    const saving = await screen.findByRole('button', { name: /Saving/ }, { timeout: 5_000 })
    expect(saving).toBeDisabled()

    await act(async () => { release() })
    await waitFor(() => expect(screen.queryByLabelText('Theme Name')).toBeNull())
  })

  it('surfaces a validation refusal as an alert and keeps the form open', async () => {
    openNew()
    fireEvent.click(btn(/Paste JSON/))
    fireEvent.change(screen.getByLabelText('Theme JSON'), { target: { value: '{oops}' } })
    fireEvent.click(btn('Save Theme'))

    const alert = await screen.findByRole('alert', undefined, { timeout: 5_000 })
    expect(alert).toHaveTextContent('Invalid JSON')
    expect(screen.getByLabelText('Theme JSON')).toBeInTheDocument()
  })

  it('closes without writing when cancelled', () => {
    openNew()
    fireEvent.click(btn('Cancel'))

    expect(screen.queryByLabelText('Theme Name')).toBeNull()
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
  })

  it('names the theme under edit and offers update plus delete', async () => {
    await openEdit()

    expect(screen.getByText(/Editing:/)).toBeInTheDocument()
    expect(await screen.findByLabelText('Theme Name')).toHaveValue('Ocean')
    expect(btn('Update Theme')).toBeInTheDocument()
    expect(btn(/Delete Theme/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save Theme' })).toBeNull()
  })

  it('falls back to the slug while an edited theme has no name', async () => {
    apiMocks.themeDetail.mockResolvedValue({ dark: {}, light: {} })
    await openEdit('seafoam')

    await waitFor(() => expect(screen.getByText(/seafoam/)).toBeInTheDocument())
  })

  it('takes the update path rather than creating a second theme', async () => {
    const seen = vi.fn()
    window.addEventListener(CUSTOM_THEMES_CHANGED_EVENT, seen)
    try {
      await openEdit()
      await waitFor(() => expect(screen.getByLabelText('Theme Name')).toHaveValue('Ocean'))
      fireEvent.click(btn('Update Theme'))

      await waitFor(() => expect(apiMocks.updateTheme).toHaveBeenCalled(), { timeout: 5_000 })
      expect(apiMocks.updateTheme).toHaveBeenCalledWith('ocean', {
        name: 'Ocean', emoji: '🌊', dark: OCEAN.dark, light: OCEAN.light,
      })
      expect(themeStore.loadCustomThemes).toHaveBeenCalled()
      expect(seen).toHaveBeenCalled()
      expect(themeStore.addCustomTheme).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(CUSTOM_THEMES_CHANGED_EVENT, seen)
    }
  })

  it('deletes the edited theme once the confirmation is accepted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await openEdit()
    fireEvent.click(btn(/Delete Theme/))

    await waitFor(() => expect(themeStore.deleteCustomTheme).toHaveBeenCalledWith('ocean'), { timeout: 5_000 })
    await waitFor(() => expect(screen.queryByLabelText('Theme Name')).toBeNull())
  })

  it('reports a failed load in the panel it opens', async () => {
    apiMocks.themeDetail.mockRejectedValue(new Error('404'))
    await openEdit('gone')

    const alert = await screen.findByRole('alert', undefined, { timeout: 5_000 })
    expect(alert).toHaveTextContent('Failed to load theme for editing')
  })
})

/* ── ThemeEditorPanel: Paste CSS ─────────────────────────────── */

describe('ThemeEditorPanel Paste CSS', () => {
  const cssArea = () => screen.getByLabelText('Theme CSS') as HTMLTextAreaElement

  it('prefills the CSS pane with the picker variables as .dark / .light blocks', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))

    expect(cssArea().value).toMatch(/^\.dark \{/)
    expect(cssArea().value).toContain('.light {')
    // The name field stays on this tab: CSS has no place to carry one.
    expect(screen.getByLabelText('Theme Name')).toBeInTheDocument()
  })

  it('saves pasted per-mode and shared variables over the picker values', async () => {
    openNew()
    fireEvent.change(screen.getByLabelText('Theme Name'), { target: { value: 'Pasted CSS' } })
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(cssArea(), {
      target: {
        value: [
          ':root { --accent: #7c3aed; --bg: #444444; }',
          '[data-theme="dark"] { --bg: #101010; }',
          '@media (prefers-color-scheme: light) { :root { --bg: #fefefe !important; } }',
        ].join('\n'),
      },
    })

    expect(screen.getByText('Will set 1 dark, 1 light and 2 shared (both dark and light) colors.')).toBeInTheDocument()
    fireEvent.click(btn('Save Theme'))

    await waitFor(() => expect(themeStore.addCustomTheme).toHaveBeenCalled(), { timeout: 5_000 })
    const saved = themeStore.addCustomTheme.mock.calls[0][0] as CustomThemeData
    expect(saved.name).toBe('Pasted CSS')
    expect(saved.dark['--bg']).toBe('#101010')
    expect(saved.light['--bg']).toBe('#fefefe')
    expect(saved.dark['--accent']).toBe('#7c3aed')
    expect(saved.light['--accent']).toBe('#7c3aed')
  })

  it('names declarations it will drop before the user saves', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(cssArea(), {
      target: { value: ':root { --bg: #000; --brand: red; color: blue; --text: url(x); }' },
    })

    expect(screen.getByText('Skipped, the dashboard has no color named: --brand, color')).toBeInTheDocument()
    expect(screen.getByText('Skipped, value not allowed (use a color, size or shadow): --text: url(x)')).toBeInTheDocument()
    expect(screen.getByText('Save leaves the skipped lines out and keeps everything else.')).toBeInTheDocument()
    expect(btn('Save Theme')).toBeEnabled()
  })

  it('shows which value was refused, shortened, and forgets it once the mode redeclares it validly', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    const long = `url(${'a'.repeat(60)})`
    fireEvent.change(cssArea(), {
      target: { value: `.dark { --text: #fff; } .light { --text: ${long}; } :root { --bg: url(x); --bg: #000; }` },
    })

    const line = screen.getByText(/^Skipped, value not allowed/)
    expect(line).toHaveTextContent(`Skipped, value not allowed (use a color, size or shadow): --text: ${long.slice(0, 40)}…`)
    expect(line.textContent).not.toContain('--bg')
  })

  it('hints at letter case only when an unknown name differs from a theme color by case alone', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(cssArea(), { target: { value: ':root { --bg: #000; --brand: red; --Accent: red; }' } })
    expect(screen.getByText('Names are case-sensitive: use --accent, not --Accent.')).toBeInTheDocument()

    fireEvent.change(cssArea(), { target: { value: ':root { --bg: #000; --brand: red; }' } })
    expect(screen.queryByText(/case-sensitive/)).toBeNull()
  })

  it('says where the CSS comes from on the tab itself', () => {
    openNew()
    expect(btn(/Paste CSS/)).toHaveTextContent('🖌️ Paste CSS (from AI)')
  })

  it('promises nothing about Save while a paste has only skipped lines', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(cssArea(), { target: { value: ':root { --brand: red; }' } })

    expect(screen.getByText('Skipped, the dashboard has no color named: --brand')).toBeInTheDocument()
    expect(screen.queryByText(/keeps everything else/)).toBeNull()
    expect(btn('Save Theme')).toBeDisabled()
  })

  it('tells the two paste tabs apart in their tooltips', () => {
    openNew()

    expect(btn(/Paste JSON/)).toHaveAttribute('title', 'Paste a whole theme as JSON, with its name and its dark and light colors.')
    expect(btn(/Paste CSS/)).toHaveAttribute('title', 'Paste CSS color variables, for example from an AI reply.')
  })

  it('holds Save on a paste that sets no theme variable and says why inline', () => {
    openNew()
    fireEvent.change(screen.getByLabelText('Theme Name'), { target: { value: 'Empty' } })
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(cssArea(), { target: { value: 'body { color: red; }' } })

    expect(btn('Save Theme')).toBeDisabled()
    expect(btn('Save Theme')).toHaveAccessibleDescription(/^No theme colors found/)
    // A validation hint, not a save failure: nothing in the error notice.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(themeStore.addCustomTheme).not.toHaveBeenCalled()

    fireEvent.change(cssArea(), { target: { value: ':root { --bg: #000; }' } })
    expect(btn('Save Theme')).toBeEnabled()
  })

  it('caps a long ignored-name list to one readable line', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    const css = Array.from({ length: 13 }, (_, i) => `--x${i}: red;`).join(' ')
    fireEvent.change(cssArea(), { target: { value: `:root { --bg: #000; ${css} }` } })

    const line = screen.getByText(/--x9 and 3 more$/)
    expect(line).toHaveTextContent('Skipped, the dashboard has no color named: --x0, ')
    expect(line.textContent).not.toContain('--x10')
  })

  it('carries a paste into the picker and on into the JSON pane', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(cssArea(), { target: { value: '.dark { --bg: #123456; }' } })
    fireEvent.click(btn(/Paste JSON/))

    const json = JSON.parse((screen.getByLabelText('Theme JSON') as HTMLTextAreaElement).value)
    expect(json.dark['--bg']).toBe('#123456')
  })
})

/* ── ThemeEditorPanel: Design it with AI ─────────────────────── */

const clipboard = vi.hoisted(() => ({ copyToClipboard: vi.fn<(text: string) => Promise<boolean>>() }))
vi.mock('../utils/clipboard', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/clipboard')>()),
  ...clipboard,
}))

describe('ThemeEditorPanel AI prompt', () => {
  beforeEach(() => {
    clipboard.copyToClipboard.mockReset()
    vi.useFakeTimers({ shouldAdvanceTime: false })
  })

  it('copies the design prompt and briefly confirms a copy that landed', async () => {
    clipboard.copyToClipboard.mockResolvedValueOnce(true)
    openNew()
    fireEvent.click(btn(/Paste CSS/))

    await act(async () => { fireEvent.click(btn('Copy prompt')) })
    expect(btn('Prompt copied')).toBeInTheDocument()
    expect(clipboard.copyToClipboard.mock.calls[0][0]).toContain('Design a colour theme for Kiro Crew')
    expect(screen.queryByRole('alert')).toBeNull()

    act(() => { vi.runOnlyPendingTimers() })
    expect(btn('Copy prompt')).toBeInTheDocument()
  })

  it.each([false, true])('keeps copy failures in an alert until success (prior success: %s)', async priorSuccess => {
    if (priorSuccess) clipboard.copyToClipboard.mockResolvedValueOnce(true)
    clipboard.copyToClipboard.mockResolvedValueOnce(false).mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    openNew()
    fireEvent.click(btn(/Paste CSS/))

    if (priorSuccess) {
      await act(async () => { fireEvent.click(btn('Copy prompt')) })
      expect(btn('Prompt copied')).toBeInTheDocument()
    }
    await act(async () => { fireEvent.click(btn(priorSuccess ? 'Prompt copied' : 'Copy prompt')) })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Couldn’t copy. Select the prompt below instead.')
    expect(within(alert).queryByRole('button')).toBeNull()
    expect(btn('Copy prompt')).toBeInTheDocument()

    act(() => { vi.runOnlyPendingTimers() })
    expect(screen.getByRole('alert')).toBe(alert)
    await act(async () => { fireEvent.click(btn('Copy prompt')) })
    expect(screen.getByRole('alert')).toBe(alert)

    await act(async () => { fireEvent.click(btn('Copy prompt')) })
    expect(btn('Prompt copied')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    act(() => { vi.runOnlyPendingTimers() })
    expect(btn('Copy prompt')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('opens the collapsed prompt when a copy fails, and the user can still close it', async () => {
    clipboard.copyToClipboard.mockResolvedValueOnce(false)
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    const details = screen.getByText('Show prompt').closest('details')!
    expect(details.open).toBe(false)

    await act(async () => { fireEvent.click(btn('Copy prompt')) })
    expect(details.open).toBe(true)
    expect(within(details).getByText('Hide prompt').tagName).toBe('SUMMARY')

    await act(async () => { fireEvent.click(within(details).getByText('Hide prompt')) })
    expect(details.open).toBe(false)
    expect(within(details).getByText('Show prompt')).toBeInTheDocument()
  })

  it('names what the prompt toggle will do next', async () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    const details = screen.getByText('Show prompt').closest('details')!

    await act(async () => { fireEvent.click(within(details).getByText('Show prompt')) })
    expect(details.open).toBe(true)
    expect(within(details).getByText('Hide prompt')).toBeInTheDocument()
    expect(within(details).queryByText('Show prompt')).toBeNull()
  })

  it('fills the name and emoji from a pasted reply without clobbering a later rename', () => {
    openNew()
    fireEvent.click(btn(/Paste CSS/))
    const area = screen.getByLabelText('Theme CSS')
    const reply = '/* name: Harbor Dusk */\n/* emoji: ⚓ */\n.dark { --bg: #10151c; }'
    fireEvent.change(area, { target: { value: reply } })

    expect(screen.getByLabelText('Theme Name')).toHaveValue('Harbor Dusk')
    expect(screen.getByLabelText('Emoji')).toHaveValue('⚓')

    fireEvent.change(screen.getByLabelText('Theme Name'), { target: { value: 'Mine' } })
    fireEvent.change(area, { target: { value: `${reply}\n.light { --bg: #f6f8fa; }` } })
    expect(screen.getByLabelText('Theme Name')).toHaveValue('Mine')
  })

  it('fills the name again when the same reply is pasted into a reopened editor', () => {
    openNew()
    const reply = '/* name: Harbor Dusk */\n/* emoji: ⚓ */\n.dark { --bg: #10151c; }'
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(screen.getByLabelText('Theme CSS'), { target: { value: reply } })
    fireEvent.click(btn('Cancel'))

    fireEvent.click(screen.getByTestId('open-new'))
    fireEvent.click(btn(/Paste CSS/))
    fireEvent.change(screen.getByLabelText('Theme CSS'), { target: { value: reply } })

    expect(screen.getByLabelText('Theme Name')).toHaveValue('Harbor Dusk')
    expect(screen.getByLabelText('Emoji')).toHaveValue('⚓')
  })
})
