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
    expect(result.current.pickerToJson).toBe('')

    act(() => { result.current.setThemeName('Named'); result.current.setThemeEmoji('') })
    expect(JSON.parse(result.current.pickerToJson)).toEqual({
      name: 'Named', emoji: '✨', dark: {}, light: {},
    })

    act(() => { result.current.setThemeName(''); result.current.updateDarkVar('--bg', '#020202') })
    expect(JSON.parse(result.current.pickerToJson).name).toBe('My Theme')
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
