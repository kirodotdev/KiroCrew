/**
 * Rebinding app editor bindings: the trigger grammar a stored chord must meet,
 * how overrides resolve against the manifest, the recorder, and the Settings →
 * Shortcuts rows that drive it.
 */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { act, fireEvent, renderHook, screen } from '@testing-library/react'

vi.mock('../utils/platform', () => ({ isMac: false }))
const apps = vi.hoisted(() => ({ list: [] as unknown[] }))
vi.mock('../hooks/panelTabRegistry', () => ({
  useInstalledApps: () => ({ apps: apps.list, error: null, isError: false }),
}))

import { parseCanonicalTrigger, triggerCaps, triggerProblem } from '../apps/editorBindingTriggers'
import {
  EDITOR_BINDING_OVERRIDES_KEY,
  applyEditorBindingOverrides,
  loadEditorBindingOverrides,
  resetEditorBindingOverride,
  setEditorBindingOverride,
} from '../apps/editorBindingOverrides'
import { useEditorBindingOverrides, useEditorBindingRecorder } from '../apps/useEditorBindingOverrides'
import { contributedEditorBindings, type EditorBindingAppRecord } from '../apps/editorBindingContributions'
import { EditorBindingsConfig } from '../pages/settings/EditorBindingsConfig'
import { renderWithProviders } from './helpers'

const APP: EditorBindingAppRecord & { displayName: string } = {
  name: 'code-nav',
  displayName: 'Code Nav',
  enabled: true,
  manifest: {
    contributes: {
      editorBindings: [
        { id: 'definition', label: 'Go to definition', trigger: { key: 'F12' }, endpoint: '/api/apps/code-nav/binding', when: { extensions: ['py'] } },
        { id: 'references', label: 'Find references', trigger: { key: 'F12', modifiers: ['shift'] }, endpoint: '/api/apps/code-nav/binding', when: { extensions: ['py'] } },
      ],
    },
  },
}

beforeEach(() => {
  localStorage.clear()
  apps.list = [APP]
})

describe('trigger grammar', () => {
  it('parses a canonical spelling back to the trigger that writes it', () => {
    expect(parseCanonicalTrigger('alt+shift+f12')).toEqual({ key: 'f12', click: false, modifiers: ['alt', 'shift'], canonical: 'alt+shift+f12' })
    expect(parseCanonicalTrigger('click:primary')).toEqual({ key: '', click: true, modifiers: ['primary'], canonical: 'click:primary' })
    expect(parseCanonicalTrigger('f5')?.canonical).toBe('f5')
  })

  it('refuses what a manifest may not bind: reserved chords, bare letters, unordered or unknown parts', () => {
    for (const bad of ['primary+s', 'primary+k', 'o', 'click:', 'shift+alt+f12', 'meta+f12', 'primary+f13', '', 'click:primary+x']) {
      expect(parseCanonicalTrigger(bad), bad).toBeNull()
    }
    expect(triggerProblem('s', false, ['primary'])).toBe('reserved')
    expect(triggerProblem('o', false, [])).toBe('key-needs-modifier')
    expect(triggerProblem('f12', false, [])).toBeNull()
  })

  it("drops a stored override on a dashboard shortcut's chord, so the binding keeps its default", () => {
    localStorage.setItem(EDITOR_BINDING_OVERRIDES_KEY, JSON.stringify({ 'code-nav:definition': 'alt+k', 'code-nav:references': 'f7' }))
    expect(loadEditorBindingOverrides()).toEqual({ 'code-nav:references': 'f7' })
  })

  it('draws key caps in the dashboard order', () => {
    expect(triggerCaps(parseCanonicalTrigger('primary+shift+o')!, true, 'Click')).toEqual(['⌘', '⇧', 'O'])
    expect(triggerCaps(parseCanonicalTrigger('alt+f12')!, false, 'Click')).toEqual(['Alt', 'F12'])
    expect(triggerCaps(parseCanonicalTrigger('click:primary')!, false, 'Click')).toEqual(['Ctrl', 'Click'])
  })
})

describe('overrides', () => {
  const declared = () => contributedEditorBindings([APP])

  it('replace a binding\'s trigger, unbind it with null, and leave the rest alone', () => {
    setEditorBindingOverride('code-nav', 'definition', 'primary+b')
    setEditorBindingOverride('code-nav', 'references', null)
    const effective = applyEditorBindingOverrides(declared(), loadEditorBindingOverrides())
    expect(effective.map(b => [b.id, b.trigger.canonical])).toEqual([['definition', 'primary+b']])
  })

  it('reset returns a binding to the app\'s own chord', () => {
    setEditorBindingOverride('code-nav', 'definition', 'primary+b')
    resetEditorBindingOverride('code-nav', 'definition')
    expect(loadEditorBindingOverrides()).toEqual({})
    expect(applyEditorBindingOverrides(declared(), {})[0].trigger.canonical).toBe('f12')
  })

  it('refuse a chord a manifest could not bind, and leave storage as it was', () => {
    expect(setEditorBindingOverride('code-nav', 'definition', 'primary+s')).toBe(false)
    expect(setEditorBindingOverride('code-nav', 'definition', 'x')).toBe(false)
    expect(localStorage.getItem(EDITOR_BINDING_OVERRIDES_KEY)).toBeNull()
  })

  it('drop a corrupt or hand-edited entry on read, so it falls back to the default', () => {
    localStorage.setItem(EDITOR_BINDING_OVERRIDES_KEY, JSON.stringify({
      'code-nav:definition': 'primary+k', 'code-nav:references': 42, 'nocolon': null, 'code-nav:x': null,
    }))
    expect(loadEditorBindingOverrides()).toEqual({ 'code-nav:x': null })
    localStorage.setItem(EDITOR_BINDING_OVERRIDES_KEY, '[1,2]')
    expect(loadEditorBindingOverrides()).toEqual({})
    localStorage.setItem(EDITOR_BINDING_OVERRIDES_KEY, '{not json')
    expect(loadEditorBindingOverrides()).toEqual({})
  })

  it('reach a live reader when written', () => {
    const { result } = renderHook(() => useEditorBindingOverrides())
    act(() => { setEditorBindingOverride('code-nav', 'definition', 'f9') })
    expect(result.current).toEqual({ 'code-nav:definition': 'f9' })
  })
})

describe('the recorder', () => {
  const press = (init: KeyboardEventInit) => act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })) })

  it('stores the next chord and stops recording', () => {
    const { result } = renderHook(() => useEditorBindingRecorder(() => null))
    act(() => result.current.start('code-nav', 'definition'))
    press({ key: 'Shift' })
    expect(result.current.recording).toBe('code-nav:definition')
    press({ key: 'b', ctrlKey: true })
    expect(result.current.recording).toBeNull()
    expect(loadEditorBindingOverrides()).toEqual({ 'code-nav:definition': 'primary+b' })
  })

  it('records a modifier-click, and lets a plain click through', () => {
    const { result } = renderHook(() => useEditorBindingRecorder(() => null))
    act(() => result.current.start('code-nav', 'definition'))
    act(() => { window.dispatchEvent(new PointerEvent('pointerdown', { button: 0 })) })
    expect(result.current.recording).toBe('code-nav:definition')
    act(() => { window.dispatchEvent(new PointerEvent('pointerdown', { button: 0, altKey: true })) })
    expect(loadEditorBindingOverrides()).toEqual({ 'code-nav:definition': 'click:alt' })
  })

  it('refuses Save and a chord another binding holds, and keeps recording', () => {
    const { result } = renderHook(() => useEditorBindingRecorder(c => (c === 'shift+f12' ? 'Find references' : null)))
    act(() => result.current.start('code-nav', 'definition'))
    press({ key: 's', ctrlKey: true })
    expect(result.current.refusal).toEqual({ kind: 'reserved', canonical: 'primary+s' })
    press({ key: 'F12', shiftKey: true })
    expect(result.current.refusal).toEqual({ kind: 'in-use', canonical: 'shift+f12', by: 'Find references' })
    expect(result.current.recording).toBe('code-nav:definition')
    expect(loadEditorBindingOverrides()).toEqual({})
  })

  it('cancels on Escape without changing anything', () => {
    const { result } = renderHook(() => useEditorBindingRecorder(() => null))
    act(() => result.current.start('code-nav', 'definition'))
    press({ key: 'Escape' })
    expect(result.current.recording).toBeNull()
    expect(loadEditorBindingOverrides()).toEqual({})
  })
})

describe('the editor', () => {
  it('fires a binding on the reader\'s chord and not on the one it replaced', async () => {
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { createElement } = await import('react')
    const { useEditorBindings } = await import('../apps/useEditorBindings')
    const client = new QueryClient()
    client.setQueryData(['apps'], [APP])
    setEditorBindingOverride('code-nav', 'definition', 'primary+b')
    const { result } = renderHook(
      () => useEditorBindings({ filePath: '/p/a.py', dirty: false, text: () => '', open: () => {}, onStale: () => {}, onError: () => {} }),
      { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
    )
    expect(result.current.dispatcher?.bound('primary+b')).toBe(true)
    expect(result.current.dispatcher?.bound('f12')).toBe(false)
    expect(result.current.dispatcher?.bound('shift+f12')).toBe(true)
    act(() => { setEditorBindingOverride('code-nav', 'references', null) })
    expect(result.current.dispatcher?.bound('shift+f12')).toBe(false)
  })
})

describe('a failed binding', () => {
  it('names the app, the binding and the cause', async () => {
    const { bindingFailure } = await import('../apps/useEditorBindings')
    const { ApiError } = await import('../api/apiError')
    const [binding] = contributedEditorBindings([APP])
    const unmounted = new ApiError(404, 'not found', JSON.stringify({ error: 'not found' }))
    expect(bindingFailure(binding, 'Code Nav', unmounted)).toBe(
      'Code Nav: Go to definition failed. This gateway has no route at /api/apps/code-nav/binding, so the app\'s backend is not loaded. Restart the gateway.')
    const refused = new ApiError(403, 'code-nav is disabled', JSON.stringify({ error: 'code-nav is disabled', code: 'app_disabled' }))
    expect(bindingFailure(binding, 'Code Nav', refused)).toBe('Code Nav: Go to definition failed. code-nav is disabled')
  })
})

describe('the shortcuts reference', () => {
  it('lists each enabled app\'s editor bindings with the chord in force, and filters them', async () => {
    const { default: ShortcutsModal } = await import('../components/ShortcutsModal')
    setEditorBindingOverride('code-nav', 'definition', 'alt+f7')
    setEditorBindingOverride('code-nav', 'references', null)
    renderWithProviders(<ShortcutsModal onClose={() => {}} />)
    expect(screen.getByText('Code editor')).toBeTruthy()
    const row = screen.getByText('Go to definition').closest('div.flex') as HTMLElement
    expect(row.textContent).toContain('Code Nav')
    expect(row.textContent).toContain('F7')
    expect(screen.queryByText('Find references')).toBeNull()
    fireEvent.change(screen.getAllByLabelText('Search')[0], { target: { value: 'code nav' } })
    expect(screen.getByText('Go to definition')).toBeTruthy()
  })

  it('has no Code editor section when no enabled app binds anything', async () => {
    const { default: ShortcutsModal } = await import('../components/ShortcutsModal')
    apps.list = []
    renderWithProviders(<ShortcutsModal onClose={() => {}} />)
    expect(screen.queryByText('Code editor')).toBeNull()
  })
})

describe('Settings → Shortcuts → Editor shortcuts', () => {
  it('lists each enabled app\'s bindings under its name with the chord in force', () => {
    setEditorBindingOverride('code-nav', 'references', 'alt+f7')
    renderWithProviders(<EditorBindingsConfig />)
    expect(screen.getByText('Editor shortcuts')).toBeTruthy()
    expect(screen.getByText('Code Nav')).toBeTruthy()
    expect(screen.getByText('Go to definition')).toBeTruthy()
    expect(screen.getByText('F7')).toBeTruthy()
    // Reset only where the reader changed something.
    expect(screen.getAllByText('Reset')).toHaveLength(1)
  })

  it('clears a binding to Not set, and resets it back', () => {
    renderWithProviders(<EditorBindingsConfig />)
    fireEvent.click(screen.getAllByText('Clear')[0])
    expect(screen.getByText('Not set')).toBeTruthy()
    fireEvent.click(screen.getByText('Reset'))
    expect(screen.queryByText('Not set')).toBeNull()
    expect(loadEditorBindingOverrides()).toEqual({})
  })

  it('says why a recorded chord was refused', () => {
    renderWithProviders(<EditorBindingsConfig />)
    fireEvent.click(screen.getAllByLabelText('Press keys…')[0])
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F12', shiftKey: true, cancelable: true })) })
    expect(screen.getByRole('status').textContent).toBe('Shift+F12 is already used by Find references.')
  })

  it("refuses a dashboard shortcut's chord, naming the shortcut", () => {
    renderWithProviders(<EditorBindingsConfig />)
    fireEvent.click(screen.getAllByLabelText('Press keys…')[0])
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', altKey: true, cancelable: true })) })
    expect(screen.getByRole('status').textContent).toBe('Alt+K is already used by Open shortcuts help.')
    expect(loadEditorBindingOverrides()).toEqual({})
  })

  it('renders nothing when no enabled app binds anything', () => {
    apps.list = [{ ...APP, enabled: false }]
    const { container } = renderWithProviders(<EditorBindingsConfig />)
    expect(container.textContent).toBe('')
  })
})
