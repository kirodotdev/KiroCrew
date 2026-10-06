import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import PortabilityTab, { IMPORT_RESULT_KEY, chatImportLines, importedItemCount, keptSettingsFiles, refusalText, settingsFileLabel, refusedItems, unbundledTemplates } from './PortabilityTab'
import { __resetUiPrefsSyncForTests, resumeUiPrefsSync } from '../../lib/uiPrefs'

// Spied, not replaced: the tab must hand the sync back exactly when it does not
// reload, and the real pause/adopt behaviour still runs underneath.
vi.mock('../../lib/uiPrefs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/uiPrefs')>()
  return { ...actual, resumeUiPrefsSync: vi.fn(actual.resumeUiPrefsSync) }
})

describe('refusalText', () => {
  const fallback = 'Import failed.'

  it('prefers the localized fallback over a coded 5xx boilerplate message', () => {
    // What these handlers actually answer with: opaque English produced in
    // Python, which says no more than the catalog string already says.
    expect(refusalText(500, { error: 'Import failed', code: 'import_failed' }, fallback))
      .toBe(fallback)
    expect(refusalText(500, { error: 'Preview failed', code: 'preview_failed' }, fallback))
      .toBe(fallback)
  })

  it('keeps the validator detail a coded 4xx carries', () => {
    // `import_archive_invalid` reports the archive validator's own finding.
    // That prose is the whole value of the message, so it must survive.
    expect(refusalText(
      400,
      { error: 'manifest.json is missing', code: 'import_archive_invalid' },
      fallback,
    )).toBe('manifest.json is missing')
  })

  it('keeps the prose of an uncoded refusal at any status', () => {
    // No machine-readable identity means the refusal may not be from these
    // handlers at all — a proxy, an edge, a gateway — and there the message can
    // be the only detail there is.
    expect(refusalText(500, { error: 'Bad gateway' }, fallback)).toBe('Bad gateway')
    expect(refusalText(400, { error: 'nope' }, fallback)).toBe('nope')
  })

  it('falls back when the body carries no message at all', () => {
    expect(refusalText(500, {}, fallback)).toBe(fallback)
    expect(refusalText(400, { error: '' }, fallback)).toBe(fallback)
  })
})

describe('unbundledTemplates', () => {
  it('reads the header list and drops anything that is not a name', () => {
    expect(unbundledTemplates('["a", 3, "b"]')).toEqual({ names: ['a', 'b'], more: 0 })
    expect(unbundledTemplates('["a", "+12"]')).toEqual({ names: ['a'], more: 12 })
    expect(unbundledTemplates(null)).toEqual({ names: [], more: 0 })
    expect(unbundledTemplates('{not json')).toEqual({ names: [], more: 0 })
    expect(unbundledTemplates('{"a": 1}')).toEqual({ names: [], more: 0 })
  })
})

describe('settingsFileLabel', () => {
  it('names each kept file by what it holds, and keeps an unknown name as-is', () => {
    expect(['config.json', 'config.local.json', 'ui-prefs.json', 'notification_settings.json', 'other.json'].map(settingsFileLabel))
      .toEqual(['agent and gateway settings', 'local settings overrides', 'display preferences', 'notification settings', 'other.json'])
  })
})

describe('keptSettingsFiles', () => {
  it('keeps distinct file names only, at most the four settings documents', () => {
    expect(keptSettingsFiles(['config.json', 3, '', 'config.json', 'ui-prefs.json'])).toEqual(['config.json', 'ui-prefs.json'])
    expect(keptSettingsFiles(['a', 'b', 'c', 'd', 'e'])).toEqual(['a', 'b', 'c', 'd'])
    expect(keptSettingsFiles(undefined)).toEqual([])
    expect(keptSettingsFiles({ a: 1 })).toEqual([])
  })
})

describe('refusedItems', () => {
  it('names distinct labels and counts the overflow', () => {
    expect(refusedItems(['config', 'config', 7, 'crons'])).toEqual({ names: ['config', 'crons'], more: 0 })
    const eight = Array.from({ length: 8 }, (_, i) => `item${i}`)
    expect(refusedItems(eight)).toEqual({ names: eight.slice(0, 6), more: 2 })
    expect(refusedItems(null)).toEqual({ names: [], more: 0 })
  })
})

describe('importedItemCount', () => {
  it('does not count an item the import refused', () => {
    expect(importedItemCount(['config (restored)', 'ui-prefs (skipped: not JSON)', 'notifications (SKIPPED: x)', 'crons (merged)'])).toBe(2)
    expect(importedItemCount(undefined)).toBe(0)
  })

  // Every non-applied shape portability.py emits, verbatim. Each used to be
  // counted as imported while the panel listed the same file as left untouched.
  it.each([
    'config (kept this install\'s; import with Replace to restore the archive\'s)',
    'ui-prefs (kept this install\'s; import with Replace to restore the archive\'s)',
    'config.local (not installed: a Merge never installs the overlay, which would outrank this install\'s config.json; import with Replace to restore the archive\'s)',
    'ui-prefs (nothing to restore)',
    'ui-prefs (nothing to restore; 3 unstorable entries dropped)',
    'hooks (skipped, already exists)',
    'crons (skipped: unreadable or invalid cron store)',
    'memory_stores/work (kept the existing store; the archive\'s copy was not merged into it)',
  ])('does not count an item the import left alone: %s', (item) => {
    expect(importedItemCount([item])).toBe(0)
    expect(importedItemCount(['config (restored)', item])).toBe(1)
  })

  it('counts every applied shape, including one whose note mentions a skip inside', () => {
    expect(importedItemCount([
      'full replace',
      'memory (copied)',
      'crons (merged)',
      'ui-prefs (restored; 1 unstorable entry dropped)',
      'notification-settings (restored)',
      'skills (merged, auto/ skipped)',
    ])).toBe(6)
  })
})

describe('PortabilityTab template warnings', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('names the templates an export leaves out, and still downloads', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(new Blob(['PK']), {
      status: 200,
      headers: { 'X-Kirocrew-Unbundled-Templates': '["reviewer", "writer", "+3"]' },
    })))
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = () => 'blob:x'
      static revokeObjectURL = () => {}
    })
    render(<PortabilityTab />)
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    const warning = await screen.findByTestId('portability-export-warning')
    expect(warning.textContent).toContain('reviewer, writer, 3 more')
    expect(warning.textContent).not.toContain('+3')
    expect(screen.getByText('Download started.')).toBeTruthy()
  })

  it('names each imported crew whose template is missing, beside the success line', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(
      url.includes('preview')
        ? { ok: true, manifest: { version: 1, created_at: 't', hostname: 'h', user: 'u', contents: {} } }
        : { ok: true, summary: { items: ['config (restored)'], missing_agent_templates: [{ crew: 'triage', kiro_agent: 'local-only' }] } },
    ), { status: 200 })))
    render(<PortabilityTab />)
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    const warning = await screen.findByTestId('portability-import-warning')
    expect(warning.textContent).toContain('triage \u2192 local-only')
    expect(screen.getByText(/Import complete/)).toBeTruthy()
  })
})

describe('PortabilityTab restoring browser settings', () => {
  const originalLocation = window.location
  let reload: ReturnType<typeof vi.fn>

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __resetUiPrefsSyncForTests()
    vi.mocked(resumeUiPrefsSync).mockClear()
    reload = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...originalLocation, reload } })
  })

  afterEach(() => {
    Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    sessionStorage.clear()
    __resetUiPrefsSyncForTests()
  })

  async function importWith(summary: Record<string, unknown>) {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(
      url.includes('preview')
        ? { ok: true, manifest: { version: 1, created_at: 't', hostname: 'h', user: 'u', contents: {} } }
        : { ok: true, summary },
    ), { status: 200 })))
    const view = render(<PortabilityTab />)
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    await screen.findByText(/Import complete/)
    return view
  }

  /** What the reloaded page shows: a fresh mount, with no import run on it. */
  function remount(view: ReturnType<typeof render>) {
    view.unmount()
    vi.unstubAllGlobals()
    render(<PortabilityTab />)
  }

  function carried(): { msg: string; warnings: string[]; errors?: string[] } | null {
    return JSON.parse(sessionStorage.getItem(IMPORT_RESULT_KEY) ?? 'null')
  }

  it('re-arms the host hydrate and reloads when the import restored browser settings', async () => {
    localStorage.setItem('mc-ui-prefs-synced', '{}')
    await importWith({ items: ['ui-prefs (merged)'], ui_prefs_restored: true })

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(localStorage.getItem('mc-ui-prefs-synced')).toBeNull()
    expect(JSON.parse(localStorage.getItem('mc-ui-prefs-hydrate-pending') ?? 'null')).toEqual([])
    // Paused until the reload adopts the host copy: resuming would let this
    // page's values overwrite the restored ones.
    expect(resumeUiPrefsSync).not.toHaveBeenCalled()
  })

  it('keeps the success line and warnings visible without carrying or reloading when adoption fails', async () => {
    localStorage.setItem('mc-ui-prefs-synced', '{}')
    const setItem = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, value) {
      if (key === 'mc-ui-prefs-hydrate-pending') throw new DOMException('full', 'QuotaExceededError')
      setItem.call(this, key, value)
    })

    await importWith({
      items: ['ui-prefs (restored)'],
      ui_prefs_restored: true,
      settings_kept: ['config.json'],
    })

    await waitFor(() => expect(resumeUiPrefsSync).toHaveBeenCalledTimes(1))
    expect(reload).not.toHaveBeenCalled()
    expect(carried()).toBeNull()
    expect(localStorage.getItem('mc-ui-prefs-synced')).toBe('{}')
    expect(screen.getByText('Import complete (1 item). To apply every change, restart the gateway: Settings → About → Restart gateway.')).toBeTruthy()
    expect(screen.getByTestId('portability-import-warning').textContent).toContain("This install kept its own agent and gateway settings.")
    expect(screen.queryByText('Import complete — reloading to apply your display settings…')).toBeNull()
  })

  it('reloads even with warnings to show, and shows them one per line after the reload', async () => {
    localStorage.setItem('mc-ui-prefs-synced', '{}')
    const view = await importWith({
      items: ['ui-prefs (merged)'],
      ui_prefs_restored: true,
      missing_agent_templates: [{ crew: 'triage', kiro_agent: 'local-only' }],
      settings_kept: ['config.json'],
    })

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(resumeUiPrefsSync).not.toHaveBeenCalled()
    const stored = carried()
    expect(stored?.msg).toMatch(/Import complete \(1 item\)\. To apply every change, restart the gateway: Settings → About → Restart gateway\./)
    expect(stored?.warnings).toHaveLength(2)
    expect(stored?.warnings[0]).toContain('triage → local-only')
    expect(stored?.warnings[1]).toContain("This install kept its own agent and gateway settings.")

    remount(view)
    expect(screen.getByText(/Import complete \(1 item\)\. To apply every change, restart the gateway: Settings → About → Restart gateway\./)).toBeTruthy()
    const lines = screen.getAllByTestId('portability-import-warning')
    expect(lines).toHaveLength(2)
    expect(lines[0].textContent).toContain('triage → local-only')
    expect(lines[1].textContent).toContain("This install kept its own agent and gateway settings.")
    expect(screen.queryByText(/Reload the page/)).toBeNull()
    expect(sessionStorage.getItem(IMPORT_RESULT_KEY)).toBeNull()
  })

  it('reloads after a clean restore and shows the success line after the reload', async () => {
    const view = await importWith({ items: ['ui-prefs (merged)', 'config (merged)'], ui_prefs_restored: true })

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(carried()).toEqual({ msg: expect.stringMatching(/Import complete \(2 items\)/), warnings: [], errors: [] })

    remount(view)
    expect(screen.getByText(/Import complete \(2 items\)\. To apply every change, restart the gateway: Settings → About → Restart gateway\./)).toBeTruthy()
    expect(screen.queryByTestId('portability-import-warning')).toBeNull()
    expect(sessionStorage.getItem(IMPORT_RESULT_KEY)).toBeNull()
  })

  it('says it is reloading before the reload, not the result it carries', async () => {
    await importWith({ items: ['ui-prefs (restored)'], ui_prefs_restored: true })

    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(screen.getByText('Import complete — reloading to apply your display settings…')).toBeTruthy()
    expect(carried()?.msg).toMatch(/Import complete \(1 item\)\. To apply every change, restart the gateway/)
  })

  it('carries a refused item across the reload and shows it as an error', async () => {
    localStorage.setItem('mc-ui-prefs-synced', '{}')
    await importWith({ items: ['ui-prefs (restored)', 'crons (skipped: x)'], ui_prefs_restored: true, refused_merges: ['crons'] })
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(carried()?.errors).toEqual(['Some items could not be imported and were left unchanged: crons.'])
    cleanup()
    render(<PortabilityTab />)
    expect(screen.getByTestId('portability-import-refused').textContent).toContain('crons')
    expect(screen.queryAllByTestId('portability-import-warning')).toHaveLength(0)
  })

  it('shows a carried result once: a later mount starts empty', () => {
    sessionStorage.setItem(IMPORT_RESULT_KEY, JSON.stringify({ msg: 'Import complete (1 item).', warnings: ['w'] }))
    const first = render(<PortabilityTab />)
    expect(screen.getByText('Import complete (1 item).')).toBeTruthy()
    first.unmount()
    render(<PortabilityTab />)
    expect(screen.queryByText(/Import complete/)).toBeNull()
    expect(screen.queryByTestId('portability-import-warning')).toBeNull()
  })

  it('leaves the profile synced, resumes the sync and does not reload when browser settings were untouched', async () => {
    localStorage.setItem('mc-ui-prefs-synced', '{}')
    await importWith({ items: ['config (merged)'] })

    await waitFor(() => expect(resumeUiPrefsSync).toHaveBeenCalledTimes(1))
    expect(localStorage.getItem('mc-ui-prefs-synced')).toBe('{}')
    expect(localStorage.getItem('mc-ui-prefs-hydrate-pending')).toBeNull()
    expect(sessionStorage.getItem(IMPORT_RESULT_KEY)).toBeNull()
    expect(reload).not.toHaveBeenCalled()
  })

  it('shows refused items as an error and each caveat on its own warning line', async () => {
    await importWith({
      items: ['config (merged)', 'ui-prefs (skipped: the archive\'s ui-prefs.json is not valid JSON)', 'crons (merged)'],
      missing_agent_templates: [{ crew: 'triage', kiro_agent: 'local-only' }],
      refused_merges: ['ui-prefs'],
      settings_kept: ['config.json', 'config.local.json', 'notification_settings.json'],
    })
    const [missing, keptLine, ...rest] = await screen.findAllByTestId('portability-import-warning')
    expect(rest).toHaveLength(0)
    expect(missing.textContent).toContain('triage → local-only')
    // A refused item is a failure: it renders through ErrorNotice, not as a warning.
    expect(screen.getByTestId('portability-import-refused').textContent)
      .toContain('Some items could not be imported and were left unchanged: ui-prefs.')
    expect(keptLine.textContent).toBe(
      "This install kept its own agent and gateway settings, local settings overrides, notification settings. "
      + "To use the archive's instead, change Merge to Replace next to Choose file, then import again. "
      + "Replace backs up this install first, then overwrites its settings with the archive's.",
    )
    // The refused item is not counted as imported.
    expect(screen.getByText(/Import complete \(2 items\)\. To apply every change, restart the gateway: Settings → About → Restart gateway\./)).toBeTruthy()
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('chatImportLines', () => {
  it('says nothing for an archive that carried no chats', () => {
    expect(chatImportLines({ items: ['config (restored)'] })).toEqual([])
  })

  it('reports added and kept chats, and the withheld ones only when there are any', () => {
    expect(chatImportLines({ sessions_added: 3, sessions_skipped_existing: 1, sessions_withheld: 0 })).toEqual([
      'Chats added: 3. Already on this install: 1.',
    ])
    expect(chatImportLines({ sessions_added: 0, sessions_skipped_existing: 0, sessions_withheld: 2 })).toEqual([
      'Chats added: 0. Already on this install: 0.',
      'Chats not imported because they are Incognito, Temporary or unreadable: 2.',
    ])
  })

  it('ignores a malformed count rather than rendering it', () => {
    expect(chatImportLines({ sessions_added: '3' })).toEqual([])
    expect(chatImportLines({ sessions_added: 1, sessions_skipped_existing: -4 })).toEqual([
      'Chats added: 1. Already on this install: 0.',
    ])
  })
})

describe('PortabilityTab chat history', () => {
  afterEach(() => vi.unstubAllGlobals())

  function stubDownload(): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async (_url: string) => new Response(new Blob(['PK']), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = () => 'blob:x'
      static revokeObjectURL = () => {}
    })
    return fetchMock
  }

  it('exports without chats unless the box is ticked', async () => {
    const fetchMock = stubDownload()
    render(<PortabilityTab />)
    const box = screen.getByRole('checkbox', { name: 'Include chat history' }) as HTMLInputElement
    expect(box.checked).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    await screen.findByText('Download started.')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/portability/export')

    fireEvent.click(box)
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(fetchMock.mock.calls[1][0]).toBe('/api/portability/export?include_sessions=true')
  })

  it('explains what a chat archive holds next to the box', () => {
    render(<PortabilityTab />)
    const box = screen.getByRole('checkbox', { name: 'Include chat history' })
    const hint = document.getElementById(box.getAttribute('aria-describedby') ?? '')
    expect(hint?.textContent).toContain('Adds every chat in your Sessions list.')
    expect(hint?.textContent).toContain('Incognito and Temporary chats are never included.')
  })

  async function preview(contents: Record<string, number>) {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(
      { ok: true, manifest: { version: 1, created_at: 't', hostname: 'h', user: 'u', contents } },
    ), { status: 200 })))
    render(<PortabilityTab />)
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    await screen.findByText('Archive contents:')
  }

  it('says when the chosen archive has no chats', async () => {
    await preview({ skill_count: 1 })
    expect(screen.getByTestId('portability-preview-no-chats').textContent)
      .toBe('This archive has no chats. To bring chats, export again with Include chat history ticked.')
  })

  async function exportWith(headers: Record<string, string>, tickChats: boolean) {
    const fetchMock = vi.fn(async () => new Response(new Blob(['PK']), { status: 200, headers }))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = () => 'blob:x'
      static revokeObjectURL = () => {}
    })
    render(<PortabilityTab />)
    if (tickChats) fireEvent.click(screen.getByRole('checkbox', { name: 'Include chat history' }))
    fireEvent.click(screen.getByRole('button', { name: /download export/i }))
    await screen.findByText('Download started.')
    return fetchMock
  }

  it('says how many older chats an export with chats left out for size', async () => {
    const fetchMock = await exportWith({ 'X-Kirocrew-Sessions-Skipped-Size': '3' }, true)
    expect(String(fetchMock.mock.calls[0][0])).toContain('include_sessions=true')
    expect(screen.getByTestId('portability-export-chats-skipped').textContent)
      .toBe("Older chats left out because they didn't fit in this archive: 3.")
  })

  it.each([
    ['nothing was left out', { 'X-Kirocrew-Sessions-Skipped-Size': '0' }, true],
    ['the header is not a bare integer', { 'X-Kirocrew-Sessions-Skipped-Size': '3; x' }, true],
    ['the export carried no chats', { 'X-Kirocrew-Sessions-Skipped-Size': '3' }, false],
  ])('shows no left-out line when %s', async (_why, headers, tickChats) => {
    await exportWith(headers, tickChats)
    expect(screen.queryByTestId('portability-export-chats-skipped')).toBeNull()
  })

  it('counts the chats the chosen archive carries', async () => {
    await preview({ session_count: 4, sessions_withheld: 1 })
    expect(screen.queryByTestId('portability-preview-no-chats')).toBeNull()
    expect(screen.getByText('Chats: 4')).toBeTruthy()
  })

  it('reports what the import did with the chats', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(
      url.includes('preview')
        ? { ok: true, manifest: { version: 1, created_at: 't', hostname: 'h', user: 'u', contents: { session_count: 3 } } }
        : { ok: true, summary: { items: ['sessions (merged: 2 added, 1 already here, 0 withheld)'], sessions_added: 2, sessions_skipped_existing: 1, sessions_withheld: 0 } },
    ), { status: 200 })))
    render(<PortabilityTab />)
    const input = screen.getByLabelText(/choose import file/i) as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['PK'], 'e.zip')] } })
    const importButton = screen.getByRole('button', { name: /^import$/i })
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(importButton)
    const line = await screen.findByTestId('portability-import-chats')
    expect(line.textContent).toBe('Chats added: 2. Already on this install: 1.')
  })
})
