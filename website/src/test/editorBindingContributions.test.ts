/**
 * `contributes.editorBindings` on the host side: what the dashboard accepts from a
 * manifest, which event a trigger matches, and what crosses to the app when one fires.
 * Mirrors the rules `test/test_editor_bindings.py` pins on the manifest.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

const invokeApi = vi.fn()
vi.mock('../api/client', () => ({
  api: { invokeEditorBinding: (...a: unknown[]) => invokeApi(...a) },
}))
let activeSlot: string | undefined = 'slot-7'
vi.mock('../store', () => ({
  store: { getState: () => ({ chat: { activeSlot } }) },
}))

import {
  MAX_EDITOR_BINDINGS_PER_APP,
  canonicalTrigger,
  contributedEditorBindings,
  bindingFor,
  bindingMatchesFile,
  invokeEditorBinding,
  triggerOfEvent,
  type BindingCardView,
  type BindingDispatchHost,
  type ContributedEditorBinding,
  type EditorBindingAppRecord,
} from '../apps/editorBindingContributions'

const GESTURE = {
  id: 'goto-def',
  label: 'Go to definition',
  trigger: { key: 'F12' },
  endpoint: '/api/apps/code-nav/definition',
  when: { extensions: ['.java', 'KT'] },
}
const CLICK = {
  id: 'peek',
  label: 'Peek',
  trigger: { click: true, modifiers: ['alt'] },
  endpoint: '/api/apps/code-nav/peek',
}

function app(overrides: Partial<EditorBindingAppRecord> & { bindings?: unknown } = {}): EditorBindingAppRecord {
  const { bindings, ...rest } = overrides
  return {
    name: 'code-nav',
    enabled: true,
    manifest: { contributes: { editorBindings: bindings ?? [GESTURE, CLICK] } },
    ...rest,
  }
}

const noEvent = { shiftKey: false, altKey: false, metaKey: false, ctrlKey: false }

beforeEach(() => { invokeApi.mockReset().mockResolvedValue({}); activeSlot = 'slot-7' })
afterEach(() => { vi.restoreAllMocks() })

describe('contributedEditorBindings', () => {
  it('reads valid bindings from enabled apps, in app-name order', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const out = contributedEditorBindings([
      app({ name: 'zeta', bindings: [{ ...CLICK, endpoint: '/api/apps/zeta/peek' }] }),
      app(),
      app({ name: 'off', enabled: false }),
    ])
    expect(out.map(g => `${g.app}:${g.id}`)).toEqual(['code-nav:goto-def', 'code-nav:peek', 'zeta:peek'])
    expect(out[0].trigger).toEqual({ key: 'f12', click: false, modifiers: [], canonical: 'f12' })
    expect(out[0].when.extensions).toEqual(['java', 'kt'])
    expect(out[1].trigger.canonical).toBe('click:alt')
  })

  it('skips what the manifest would refuse, with a warning, and keeps the rest', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const bad = [
      { ...GESTURE, id: 'Bad Id' },
      { ...GESTURE, id: 'no-label', label: '' },
      { ...GESTURE, id: 'core-route', endpoint: '/api/apps/code-nav/uninstall' },
      { ...GESTURE, id: 'other-app', endpoint: '/api/apps/other/x' },
      { ...GESTURE, id: 'both', trigger: { key: 'F12', click: true } },
      { ...GESTURE, id: 'bare-letter', trigger: { key: 'g' } },
      { ...GESTURE, id: 'bare-click', trigger: { click: true } },
      { ...GESTURE, id: 'editing-key', trigger: { key: 'Enter' } },
      { ...GESTURE, id: 'save-chord', trigger: { key: 's', modifiers: ['primary'] } },
      { ...GESTURE, id: 'shortcuts-chord', trigger: { key: 'k', modifiers: ['alt'] } },
      { ...GESTURE, id: 'meta', trigger: { key: 'F12', modifiers: ['meta'] } },
      { ...GESTURE, id: 'same-chord', trigger: { key: 'f12' } },
      'nope',
      { ...GESTURE, id: 'goto-def' },
    ]
    const out = [
      ...contributedEditorBindings([app({ bindings: [GESTURE, ...bad.slice(0, 7)] })]),
      ...contributedEditorBindings([app({ bindings: [GESTURE, ...bad.slice(7)] })]),
    ]
    expect(out.map(g => g.id)).toEqual(['goto-def', 'goto-def'])
    expect(warn).toHaveBeenCalledTimes(bad.length)
    expect(warn.mock.calls.map(c => String(c[0]))).toEqual(expect.arrayContaining([
      expect.stringContaining("primary+s is the host's own chord"),
      expect.stringContaining("alt+k is the dashboard's shortcuts-modal shortcut"),
      expect.stringContaining('f12 is already bound by this app'),
      expect.stringContaining('duplicate id'),
    ]))
  })

  it('bounds the work per app at the manifest cap', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const many = Array.from({ length: MAX_EDITOR_BINDINGS_PER_APP + 5 }, (_, i) => ({
      ...GESTURE, id: `g${i}`, trigger: { key: `f${(i % 12) + 1}`, modifiers: i >= 12 ? ['alt'] : [] },
    }))
    expect(contributedEditorBindings([app({ bindings: many })])).toHaveLength(MAX_EDITOR_BINDINGS_PER_APP)
  })

  it('tolerates a non-array cache value and a non-array declaration', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(contributedEditorBindings({} as unknown as EditorBindingAppRecord[])).toEqual([])
    expect(contributedEditorBindings([app({ bindings: { id: 'x' } })])).toEqual([])
  })
})

describe('triggers', () => {
  it('spells a chord canonically, modifiers sorted', () => {
    expect(canonicalTrigger('f12', false, ['shift', 'primary'])).toBe('primary+shift+f12')
    expect(canonicalTrigger('', true, ['alt'])).toBe('click:alt')
  })

  it('reads a key event into a trigger, primary by platform, never both', () => {
    expect(triggerOfEvent({ ...noEvent, key: 'F12' }, true)).toBe('f12')
    expect(triggerOfEvent({ ...noEvent, key: 'F12', shiftKey: true }, true)).toBe('shift+f12')
    expect(triggerOfEvent({ ...noEvent, key: 'O', metaKey: true, shiftKey: true }, true)).toBe('primary+shift+o')
    expect(triggerOfEvent({ ...noEvent, key: 'o', ctrlKey: true, shiftKey: true }, false)).toBe('primary+shift+o')
    // The other platform's modifier is not primary, and Cmd+Ctrl together is nothing.
    expect(triggerOfEvent({ ...noEvent, key: 'o', ctrlKey: true }, true)).toBeNull()
    expect(triggerOfEvent({ ...noEvent, key: 'o', ctrlKey: true, metaKey: true }, false)).toBeNull()
    // Typing, and the editor's own keys.
    expect(triggerOfEvent({ ...noEvent, key: 'g' }, true)).toBeNull()
    expect(triggerOfEvent({ ...noEvent, key: 'Enter', altKey: true }, true)).toBeNull()
    expect(triggerOfEvent({ ...noEvent, key: 'ArrowDown', shiftKey: true }, true)).toBeNull()
  })

  it('reads a pointer event into a click trigger on the left button with a modifier', () => {
    expect(triggerOfEvent({ ...noEvent, button: 0, altKey: true }, true)).toBe('click:alt')
    expect(triggerOfEvent({ ...noEvent, button: 0, metaKey: true, shiftKey: true }, true)).toBe('click:primary+shift')
    expect(triggerOfEvent({ ...noEvent, button: 0 }, true)).toBeNull()
    expect(triggerOfEvent({ ...noEvent, button: 2, altKey: true }, true)).toBeNull()
  })

  it('matches a file by extension, and a trigger to its binding for that file', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const [goto, peek] = contributedEditorBindings([app()])
    expect(bindingMatchesFile(goto, '/p/A.java')).toBe(true)
    expect(bindingMatchesFile(goto, '/p/a.KT')).toBe(true)
    expect(bindingMatchesFile(goto, '/p/a.py')).toBe(false)
    expect(bindingMatchesFile(goto, '/p/Makefile')).toBe(false)
    expect(bindingMatchesFile(goto, '/p/.gitignore')).toBe(false)
    expect(bindingMatchesFile(peek, '/p/anything')).toBe(true)
    expect(bindingFor([goto, peek], 'f12', '/p/A.java')?.id).toBe('goto-def')
    expect(bindingFor([goto, peek], 'f12', '/p/a.py')).toBeNull()
    expect(bindingFor([goto, peek], 'click:alt', '/p/a.py')?.id).toBe('peek')
  })
})

describe('editor text grant', () => {
  it('follows permissions.editor until a stored approved set says otherwise', () => {
    const declared = app({ manifest: { permissions: { editor: ['content'] }, contributes: { editorBindings: [GESTURE] } } })
    expect(contributedEditorBindings([declared])[0].textGranted).toBe(true)
    expect(contributedEditorBindings([app()])[0].textGranted).toBe(false)
    // An update that adds the grant is held back: the manifest says yes, the owner has not.
    const held = { ...declared, approvedGrants: { editor: [] } }
    expect(contributedEditorBindings([held])[0].textGranted).toBe(false)
    const approved = { ...declared, approvedGrants: { editor: ['content'] } }
    expect(contributedEditorBindings([approved])[0].textGranted).toBe(true)
    // A binding-level field is not the grant.
    const stray = app({ bindings: [{ ...GESTURE, content: 'on-mismatch' }] })
    expect(contributedEditorBindings([stray])[0].textGranted).toBe(false)
  })
})

describe('invokeEditorBinding', () => {
  const binding: ContributedEditorBinding = {
    id: 'goto-def', app: 'code-nav', label: 'Go to definition', endpoint: '/api/apps/code-nav/definition',
    trigger: { key: 'f12', click: false, modifiers: [], canonical: 'f12' }, when: { extensions: [] }, textGranted: false,
  }
  const at = { path: '/p/A.java', line: 4, character: 9, dirty: true, trigger: 'f12' }
  // SHA-256 of the UTF-8 bytes of "class A {}".
  const TEXT = 'class A {}'
  const SHA = '107310d1668e0941284e7595573d77788d10959a91d6eb1a53c03b4faba0bc97'
  function host(over: Partial<BindingDispatchHost> = {}): BindingDispatchHost & { shown: (BindingCardView | null)[] } {
    const shown: (BindingCardView | null)[] = []
    return {
      shown,
      text: () => TEXT,
      open: vi.fn(),
      show: view => { shown.push(view) },
      onStale: vi.fn(),
      onError: vi.fn(),
      ...over,
    }
  }

  it('sends the path, caret and a hash of the buffer with the owning slot, never the text, and opens one answer', async () => {
    invokeApi.mockResolvedValue({ open: { path: '/p/B.java', line: 12, character: 3 }, message: 'Found B' })
    const h = host()
    const notified: unknown[] = []
    const listener = (e: Event) => notified.push((e as CustomEvent).detail)
    window.addEventListener('mc:notify', listener)
    await invokeEditorBinding(binding, at, h)
    window.removeEventListener('mc:notify', listener)
    expect(invokeApi).toHaveBeenCalledTimes(1)
    const [, sent, slot] = invokeApi.mock.calls[0]
    expect(slot).toBe('dashboard:slot-7')
    expect(sent).toEqual({ ...at, sha256: expect.stringMatching(/^[0-9a-f]{64}$/) })
    expect(sent.sha256).toBe(SHA)
    expect('text' in sent).toBe(false)
    expect(h.open).toHaveBeenCalledWith({ path: '/p/B.java', line: 12, character: 3 }, false)
    // Loading from the moment it fires, closed once the answer is a plain jump.
    expect(h.shown).toEqual([{ status: 'loading', title: 'Go to definition' }, null])
    expect(notified).toEqual([expect.objectContaining({ source: 'editor-binding', message: 'Found B' })])
  })

  it('treats an answer in the same file as a jump, and ignores a malformed open', async () => {
    const h = host()
    invokeApi.mockResolvedValue({ open: { path: '/p/A.java', line: 2 } })
    await invokeEditorBinding(binding, at, h)
    expect(h.open).toHaveBeenCalledWith({ path: '/p/A.java', line: 2, character: undefined }, true)
    for (const bad of [{ open: { path: '', line: 1 } }, { open: { path: '/x', line: 0 } }, { open: { path: '/x', line: '3' } }, { open: 'yes' }, null]) {
      const h2 = host()
      invokeApi.mockResolvedValue(bad)
      await invokeEditorBinding(binding, at, h2)
      expect(h2.open).not.toHaveBeenCalled()
      expect(h2.shown.at(-1)).toBeNull()
    }
  })

  it('offers several locations as a picker, labelled by the app or by file:line, and opens the pick', async () => {
    invokeApi.mockResolvedValue({ title: 'Definitions', open: [
      { path: '/p/A.java', line: 2, label: 'A.run()', detail: 'void run()' },
      { path: '/p/B.java', line: 7 },
      { path: '/x', line: 0 },
    ] })
    const h = host()
    await invokeEditorBinding(binding, at, h)
    expect(h.open).not.toHaveBeenCalled()
    const card = h.shown.at(-1)
    expect(card).toMatchObject({ status: 'ready', title: 'Definitions', markdown: '' })
    expect(card && card.status === 'ready' ? card.rows.map(r => [r.primary, r.secondary, r.sameFile]) : null)
      .toEqual([['A.run()', 'void run()', true], ['B.java:7', '', false]])
  })

  it('shows markdown in the card under the binding label when the app names no title', async () => {
    invokeApi.mockResolvedValue({ markdown: '**int** count' })
    const h = host()
    await invokeEditorBinding(binding, at, h)
    expect(h.shown.at(-1)).toEqual({ status: 'ready', title: 'Go to definition', rows: [], markdown: '**int** count' })
  })

  it('tells the reader to save when the app says stale and holds no editor content grant', async () => {
    invokeApi.mockResolvedValue({ stale: true })
    const h = host()
    await invokeEditorBinding(binding, at, h)
    expect(invokeApi).toHaveBeenCalledTimes(1)
    expect(h.onStale).toHaveBeenCalledTimes(1)
    expect(h.shown.at(-1)).toBeNull()
  })

  it('retries once with the text when the app holds the editor content grant', async () => {
    invokeApi.mockResolvedValueOnce({ stale: true }).mockResolvedValueOnce({ open: { path: '/p/B.java', line: 1 } })
    const h = host()
    await invokeEditorBinding({ ...binding, textGranted: true }, at, h)
    expect(invokeApi).toHaveBeenCalledTimes(2)
    expect(invokeApi.mock.calls[0][1].text).toBeUndefined()
    expect(invokeApi.mock.calls[1][1]).toEqual({ ...at, sha256: SHA, text: TEXT })
    expect(h.onStale).not.toHaveBeenCalled()
    expect(h.open).toHaveBeenCalledWith({ path: '/p/B.java', line: 1, character: undefined }, false)
  })

  it('does not loop on an app that answers stale to the text itself', async () => {
    invokeApi.mockResolvedValue({ stale: true })
    const h = host()
    await invokeEditorBinding({ ...binding, textGranted: true }, at, h)
    expect(invokeApi).toHaveBeenCalledTimes(2)
    expect(h.shown.at(-1)).toBeNull()
    expect(h.open).not.toHaveBeenCalled()
  })

  it('reports a refusal to the host rather than the console, and closes the card', async () => {
    const refusal = new Error('HTTP 403')
    invokeApi.mockRejectedValue(refusal)
    const h = host()
    await invokeEditorBinding(binding, at, h)
    expect(h.onError).toHaveBeenCalledWith(refusal)
    expect(h.shown).toEqual([{ status: 'loading', title: 'Go to definition' }, null])
  })

  it('sends no slot when the chat has none', async () => {
    activeSlot = undefined
    await invokeEditorBinding(binding, at, host())
    expect(invokeApi.mock.calls[0][2]).toBeUndefined()
  })
})
