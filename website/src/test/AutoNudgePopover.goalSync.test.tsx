import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import {
  __resetForTests,
  LEGACY_GOAL_DRAFTS_KEY,
  loadGoalDraft,
  loadGoalDraftSnapshot,
  saveGoalDraft,
} from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'
import {
  SLOT,
  goalRecordKeys,
  goalStorageSnapshot,
  isGoalRecordKey,
  makeLoop,
  renderPopover,
} from './autoNudgePopoverFixtures'

const testNowMs = () => Date.now() // flake-ok: relative stamps are compared synchronously within each test

describe('AutoNudgePopover goal persistence and cross-device sync', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    // The popover fetches on OPEN (reads /api/crons to list this slot's
    // watches) and on Save/Stop. Stub so nothing escapes the test.
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement

  it('remembers the user-typed goal and restores it after the loop is gone (the reported bug)', () => {
    vi.useFakeTimers()
    // 1. User opens the popover (no loop yet) and types a custom goal.
    const first = renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: 'Ship the BYOA gate harness' } })
    // Debounced: not written synchronously. Advancing past the debounce persists it.
    expect(loadGoalDraft(SLOT)).toBeNull()
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    expect(loadGoalDraft(SLOT)?.message).toBe('Ship the BYOA gate harness')
    first.unmount()

    // 2. The loop is stopped elsewhere → ChatPage passes loop={null} on re-open;
    //    the popover restores the stored draft, not the default template.
    renderPopover(null)
    expect(goalBox().value).toBe('Ship the BYOA gate harness')
  })

  it('flushes a pending debounced edit on unmount (a fast close does not lose the last keystrokes)', () => {
    vi.useFakeTimers()
    const view = renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: 'closing fast' } })
    // Close BEFORE the debounce fires — the unmount flush must still persist it.
    expect(loadGoalDraft(SLOT)).toBeNull()
    view.unmount()
    expect(loadGoalDraft(SLOT)?.message).toBe('closing fast')
  })

  it('does not persist the pristine default (an untouched popover pins nothing, on open or close)', () => {
    vi.useFakeTimers()
    const view = renderPopover(null)
    // Opened, never edited → the edit-guard means no write, on debounce OR unmount.
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    expect(loadGoalDraft(SLOT)).toBeNull()
    view.unmount()
    expect(loadGoalDraft(SLOT)).toBeNull()
  })

  it('starting an untouched default does not mirror it into the draft store', async () => {
    renderPopover(null)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Start loop/i }))
    })

    expect(loadGoalDraftSnapshot(SLOT)).toEqual({ draft: null, updatedAt: 0 })
  })

  it('opening with an existing stored draft does not rewrite it (a mere view must not touch the store)', () => {
    // Seed a draft, snapshot the raw storage, then open (no edit) and close.
    // The stored bytes must be identical — no TTL refresh, no LRU bump.
    saveGoalDraft(SLOT, { message: 'remembered goal', idleSecs: 120, maxCycles: 5 })
    const recordsBefore = goalStorageSnapshot()

    const view = renderPopover(null)
    expect(goalBox().value).toBe('remembered goal') // restored on open
    view.unmount() // close without editing

    expect(goalStorageSnapshot()).toEqual(recordsBefore)
  })

  it('prefers the live loop message over a stored draft when a loop is running', () => {
    saveGoalDraft(SLOT, { message: 'stale draft goal', idleSecs: 60, maxCycles: 0 })
    renderPopover(makeLoop({ message: 'active loop goal' }))
    expect(goalBox().value).toBe('active loop goal')
  })

  it('hydrates a newer desktop draft from the server over stale mobile local storage', async () => {
    const now = testNowMs()
    saveGoalDraft(SLOT, { message: 'stale mobile goal', idleSecs: 60, maxCycles: 0 }, now - 5_000)
    vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(
        String(url).startsWith('/api/autonudge/draft/')
          ? {
              draft: { message: 'latest desktop goal', idle_secs: 90, max_cycles: 4 },
              updated_at: now,
            }
          : { jobs: [] },
      ),
    })) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(goalBox().value).toBe('latest desktop goal'))
    expect(loadGoalDraft(SLOT)).toEqual({
      message: 'latest desktop goal', idleSecs: 90, maxCycles: 4,
    })
  })

  it('hydrates a remote canonical for a previously unseen inherited slot key', async () => {
    const slot = 'toString'
    const canonicalAt = testNowMs()
    vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(
        String(url).startsWith('/api/crons')
          ? { jobs: [] }
          : {
              draft: { message: 'remote inherited-slot goal', idle_secs: 90, max_cycles: 4 },
              updated_at: canonicalAt,
            },
      ),
    })) as unknown as typeof fetch)

    renderPopover(null, slot)

    await waitFor(() => expect(goalBox().value).toBe('remote inherited-slot goal'))
    expect(loadGoalDraftSnapshot(slot)).toEqual({
      draft: { message: 'remote inherited-slot goal', idleSecs: 90, maxCycles: 4 },
      updatedAt: canonicalAt,
    })
  })

  it('migrates a newer sparse legacy draft for an inherited slot key', async () => {
    const slot = 'constructor'
    localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify({
      [slot]: { message: 'local inherited-slot goal', idleSecs: 120, maxCycles: 6 },
    }))
    const puts: Record<string, unknown>[] = []
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    renderPopover(null, slot)

    await waitFor(() => expect(puts).toHaveLength(1))
    expect(goalBox().value).toBe('local inherited-slot goal')
    expect((puts[0].draft as Record<string, unknown>).message).toBe(
      'local inherited-slot goal',
    )
    expect(Number.isFinite(Number(puts[0].updated_at))).toBe(true)
  })

  it('offers an in-place retry after reconciliation fails', async () => {
    const now = testNowMs()
    let reads = 0
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      reads += 1
      if (reads === 1) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'recovered desktop goal', idle_secs: 90, max_cycles: 4 },
          updated_at: now,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    const notice = await screen.findByTestId('goal-draft-sync-error')
    expect(notice).toHaveTextContent("Couldn't sync this draft")
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(goalBox().value).toBe('recovered desktop goal'))
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeEnabled()
    expect(reads).toBe(2)
  })

  it('migrates a newer local draft without letting the stale server copy win', async () => {
    const now = testNowMs()
    saveGoalDraft(SLOT, { message: 'latest desktop goal', idleSecs: 120, maxCycles: 6 }, now)
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body))
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'stale mobile goal', idle_secs: 60, max_cycles: 0 },
          updated_at: now - 5_000,
        }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(fetchMock.mock.calls.some(call => call[1]?.method === 'PUT')).toBe(true))
    const put = fetchMock.mock.calls.find(call => call[1]?.method === 'PUT')!
    expect(JSON.parse(String(put[1]?.body)).draft.message).toBe('latest desktop goal')
    expect(goalBox().value).toBe('latest desktop goal')
  })

  it('shows cross-device reconciliation and blocks Start until the canonical draft arrives', async () => {
    const now = testNowMs()
    saveGoalDraft(SLOT, { message: 'stale mobile goal', idleSecs: 60, maxCycles: 0 }, now - 5_000)
    let resolveDraft!: (value: { ok: boolean; json: () => Promise<unknown> }) => void
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      return new Promise(resolve => { resolveDraft = resolve })
    }) as unknown as typeof fetch)

    renderPopover(null)
    const status = screen.getByTestId('goal-draft-sync-status')
    expect(status).toHaveTextContent('Checking saved goal…')
    const start = screen.getByRole('button', { name: /Start loop/i })
    expect(start).toBeDisabled()
    // The reason Start is disabled is programmatically the status line, which
    // sits in Start's own row.
    expect(status).toHaveAttribute('id')
    expect(start).toHaveAttribute('aria-describedby', status.getAttribute('id'))
    expect(start).toHaveAccessibleDescription('Checking saved goal…')
    expect(start).toHaveAttribute('title', 'Start loop')
    // Same action row as Start (main's row wraps the icon control in its own group).
    expect(status.parentElement).toBe(screen.getByTestId('auto-nudge-actions'))
    expect(screen.getByTestId('auto-nudge-actions')).toContainElement(start)

    await act(async () => {
      resolveDraft({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'latest desktop goal', idle_secs: 90, max_cycles: 4 },
          updated_at: now,
        }),
      })
    })

    await waitFor(() => expect(goalBox().value).toBe('latest desktop goal'))
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Updated from another device.')
    expect(screen.getByRole('button', { name: /Start loop/i })).not.toBeDisabled()
    // Once Start is enabled the status is ordinary context again, not its description.
    expect(screen.getByRole('button', { name: /Start loop/i })).not.toHaveAttribute('aria-describedby')
    expect(screen.getByRole('button', { name: /Start loop/i })).toHaveAttribute('title', 'Start loop')
  })

  it('keeps the status line mounted through checking and idle so the form does not shift', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ draft: null, updated_at: 0 }) })
    }) as unknown as typeof fetch)

    renderPopover(null)
    const status = screen.getByTestId('goal-draft-sync-status')
    expect(status).toHaveTextContent('Checking saved goal…')
    expect(status).toHaveAttribute('role', 'status')
    expect(status).toHaveAttribute('aria-live', 'polite')

    // Idle empties the same element instead of unmounting it: the live region
    // predates its next announcement and the footer row keeps its height.
    await waitFor(() => expect(status).toBeEmptyDOMElement())
    expect(status.isConnected).toBe(true)
    expect(screen.getByTestId('goal-draft-sync-status')).toBe(status)
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeEnabled()
  })

  it('renders no sync status while a live loop is authoritative', () => {
    renderPopover(makeLoop())
    expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull()
  })

  it('serializes a first-sync migration before a live edit from the same slot', async () => {
    const now = testNowMs()
    saveGoalDraft(SLOT, { message: 'future-skewed local goal', idleSecs: 60, maxCycles: 0 }, now + 60_000)
    const puts: Record<string, unknown>[] = []
    let resolveMigration!: () => void
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (sent.migration === true) {
          return new Promise(resolve => {
            resolveMigration = () => resolve({
              ok: true,
              json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
            })
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: now + 60_001 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'older server goal', idle_secs: 60, max_cycles: 0 },
          updated_at: now,
        }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    const view = renderPopover(null)
    await waitFor(() => expect(puts).toHaveLength(1))
    fireEvent.change(goalBox(), { target: { value: 'live edit while migration is pending' } })
    view.unmount()
    expect(puts).toHaveLength(1)

    resolveMigration()
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[0].migration).toBe(true)
    expect(puts[1].migration).toBe(false)
    expect((puts[1].draft as Record<string, unknown>).message).toBe('live edit while migration is pending')
  })

  it('keeps per-slot write ordering across a popover remount', async () => {
    const now = testNowMs()
    const puts: Record<string, unknown>[] = []
    let resolveFirstPut!: () => void
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return new Promise(resolve => {
            resolveFirstPut = () => resolve({
              ok: true,
              json: () => Promise.resolve({ draft: sent.draft, updated_at: now + 1 }),
            })
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: now + 2 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    const first = renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'first mount edit' } })
    first.unmount()
    await waitFor(() => expect(puts).toHaveLength(1))

    saveGoalDraft(SLOT, { message: 'second mount local copy', idleSecs: 60, maxCycles: 0 }, now + 10_000)
    const second = renderPopover(null)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(puts).toHaveLength(1)

    resolveFirstPut()
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[0].migration).toBe(false)
    expect(puts[1].migration).toBe(true)
    second.unmount()
  })

  it('Start completion does not overwrite a newer autosaved draft', async () => {
    let finishStart!: () => void
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (String(url) === '/api/autonudge' && init?.method === 'POST') {
        return new Promise(resolve => {
          finishStart = () => resolve({
            ok: true,
            json: () => Promise.resolve({ loop: makeLoop({ message: 'start submission' }) }),
          })
        })
      }
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ draft: puts.at(-1)?.draft, updated_at: testNowMs() }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ draft: null, updated_at: 0 }) })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'start submission' } })
    fireEvent.click(screen.getByRole('button', { name: /Start loop/i }))
    await waitFor(() => expect(finishStart).toBeTypeOf('function'))

    const newer = { message: 'newer autosaved draft', idleSecs: 60, maxCycles: 0 }
    saveGoalDraft(SLOT, newer, testNowMs() + 1_000)
    await act(async () => { finishStart(); await Promise.resolve() })

    expect(loadGoalDraft(SLOT)).toEqual(newer)
    expect(puts).toHaveLength(0)
  })

  it('keeps a focused raw number field when canonical writeback matches the submitted draft', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ draft: sent.draft, updated_at: Number(sent.updated_at) + 1 }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ draft: null, updated_at: 0 }) })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    const idle = screen.getAllByRole('spinbutton')[0] as HTMLInputElement
    idle.focus()
    fireEvent.change(idle, { target: { value: '' } })
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(idle).toHaveFocus()
    expect(idle.value).toBe('')
  })

  it('does not let an in-flight save erase a newer typed edit before its debounce fires', async () => {
    const puts: Record<string, unknown>[] = []
    let resolveFirstPut!: (value: { ok: boolean; json: () => Promise<unknown> }) => void
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return new Promise(resolve => { resolveFirstPut = resolve })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            draft: sent.draft,
            updated_at: Number(sent.updated_at) + 1,
          }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())

    fireEvent.change(goalBox(), { target: { value: 'first submitted edit' } })
    await waitFor(() => expect(puts).toHaveLength(1))

    // This value exists only in the live form until its debounce fires. The
    // older response must not reset it or clear the edit flag that owns its timer.
    fireEvent.change(goalBox(), { target: { value: 'newer typed edit' } })
    await act(async () => {
      resolveFirstPut({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'first submitted edit', idle_secs: 60, max_cycles: 0 },
          updated_at: Number(puts[0].updated_at) + 1,
        }),
      })
      await Promise.resolve()
    })

    expect(goalBox().value).toBe('newer typed edit')
    await waitFor(() => expect(puts).toHaveLength(2))
    expect((puts[1].draft as Record<string, unknown>).message).toBe('newer typed edit')
  })

  it('an older failed write cannot expose Retry over a newer live edit', async () => {
    const puts: Record<string, unknown>[] = []
    let failFirstPut!: () => void
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return new Promise(resolve => {
            failFirstPut = () => resolve({
              ok: false,
              status: 503,
              json: () => Promise.resolve({ error: 'temporarily unavailable' }),
            })
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            draft: sent.draft,
            updated_at: Number(sent.updated_at) + 1,
          }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'older submitted edit' } })
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'newer live edit' } })
    await act(async () => {
      failFirstPut()
      await Promise.resolve()
    })

    expect(goalBox().value).toBe('newer live edit')
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    await waitFor(() => expect(puts).toHaveLength(2))
    expect((puts[1].draft as Record<string, unknown>).message).toBe('newer live edit')
  })

  it('surfaces a live write failure while preserving the local fallback', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    fireEvent.change(goalBox(), { target: { value: 'local edit stays here' } })

    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
      "Couldn't sync this draft",
    ))
    expect(loadGoalDraft(SLOT)?.message).toBe('local edit stays here')
  })

  it('surfaces Retry when clearing the textarea fails to sync', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'goal to clear', idle_secs: 60, max_cycles: 0 },
          updated_at: testNowMs(),
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(goalBox().value).toBe('goal to clear'))
    fireEvent.change(goalBox(), { target: { value: '' } })

    const notice = await screen.findByTestId('goal-draft-sync-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent("Couldn't clear this goal")
    expect(screen.getByRole('button', { name: /^Retry$/i })).toBeEnabled()
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
  })

  it('keeps a conflicting edit and retries it against the newly read canonical base', async () => {
    const originalStamp = testNowMs()
    const newerStamp = originalStamp + 100
    saveGoalDraft(SLOT, { message: 'original shared goal', idleSecs: 60, maxCycles: 0 }, originalStamp)
    let reads = 0
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return Promise.resolve({
            ok: false,
            status: 409,
            json: () => Promise.resolve({
              code: 'goal_draft_conflict',
              error: 'goal draft changed since it was loaded',
              current: {
                draft: { message: 'newer device goal', idle_secs: 60, max_cycles: 0 },
                updated_at: newerStamp,
              },
            }),
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: newerStamp + 1 }),
        })
      }
      reads += 1
      const remote = reads === 1
        ? { message: 'original shared goal', idle_secs: 60, max_cycles: 0 }
        : { message: 'newer device goal', idle_secs: 60, max_cycles: 0 }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: remote,
          updated_at: reads === 1 ? originalStamp : newerStamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'my conflicting edit' } })

    const notice = await screen.findByTestId('goal-draft-sync-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('the goal was changed or reloaded')
    expect(goalBox().value).toBe('my conflicting edit')
    expect(puts[0].base_updated_at).toBe(originalStamp)

    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[1].base_updated_at).toBe(newerStamp)
    expect((puts[1].draft as Record<string, unknown>).message).toBe('my conflicting edit')
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect(goalBox().value).toBe('my conflicting edit')
  })

  it('preserves a pending edit as a conflict when reopening sees a newer canonical', async () => {
    const originalStamp = testNowMs()
    let reads = 0
    let newerStamp = originalStamp + 100
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      reads += 1
      const current = reads === 1
        ? { message: 'original canonical', idle_secs: 60, max_cycles: 0 }
        : { message: 'newer device goal', idle_secs: 60, max_cycles: 0 }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: current,
          updated_at: reads === 1 ? originalStamp : newerStamp,
        }),
      })
    }) as unknown as typeof fetch)

    const first = renderPopover(null)
    await waitFor(() => expect(goalBox().value).toBe('original canonical'))
    fireEvent.change(goalBox(), { target: { value: 'pending offline edit' } })
    await screen.findByTestId('goal-draft-sync-error')
    newerStamp = loadGoalDraftSnapshot(SLOT).updatedAt + 100
    first.unmount()

    renderPopover(null)

    await waitFor(() => expect(reads).toBe(2))
    expect(goalBox().value).toBe('pending offline edit')
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(
      'the goal was changed or reloaded',
    )
    expect(screen.getByRole('button', { name: /^Retry$/i })).toBeEnabled()
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()
  })

  it('retries from the in-memory edit when local and remote persistence both fail', async () => {
    const now = testNowMs()
    saveGoalDraft(SLOT, { message: 'older stored copy', idleSecs: 60, maxCycles: 0 }, now)
    const puts: Record<string, unknown>[] = []
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return Promise.resolve({
            ok: false,
            status: 503,
            json: () => Promise.resolve({ error: 'temporarily unavailable' }),
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: Number(sent.updated_at) + 1 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'older stored copy', idle_secs: 60, max_cycles: 0 },
          updated_at: now,
        }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())

    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.change(goalBox(), { target: { value: 'only in-memory edit' } })
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    } finally {
      Storage.prototype.setItem = orig
    }

    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(((puts[1].draft as Record<string, unknown>).message)).toBe('only in-memory edit')
    expect(goalBox().value).toBe('only in-memory edit')
  })

  it('retains a first server canonical in memory when its local write-back fails', async () => {
    const slot = 'chat-canonical-write-fallback'
    const oldStamp = testNowMs()
    const canonicalStamp = oldStamp + 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    let reads = 0
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      reads += 1
      if (reads === 1) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            draft: { message: 'server canonical', idle_secs: 90, max_cycles: 4 },
            updated_at: canonicalStamp,
          }),
        })
      }
      return Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ error: 'temporarily unavailable' }),
      })
    }) as unknown as typeof fetch)

    let writeAttempts = 0
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) {
        writeAttempts += 1
        throw new Error('QuotaExceeded')
      }
      return orig.call(this, k, v)
    }
    const first = renderPopover(null, slot)
    try {
      await waitFor(() => expect(goalBox().value).toBe('server canonical'))
      await waitFor(() => expect(writeAttempts).toBeGreaterThan(0))
    } finally {
      Storage.prototype.setItem = orig
    }

    first.unmount()
    renderPopover(null, slot)
    expect(goalBox().value).toBe('server canonical')
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(goalBox().value).toBe('server canonical')
  })

  it('Retry keeps a Start-path edit when local and remote writes both fail', async () => {
    const slot = 'chat-start-write-fallback'
    const oldStamp = testNowMs()
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    let reads = 0
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'POST') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
      }
      if (init?.method === 'PUT') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      reads += 1
      if (reads === 1) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            draft: { message: 'stale durable goal', idle_secs: 60, max_cycles: 0 },
            updated_at: oldStamp,
          }),
        })
      }
      return Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ error: 'temporarily unavailable' }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null, slot)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    fireEvent.change(goalBox(), { target: { value: 'only in-memory started goal' } })

    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: /Start loop/i }))
        for (let i = 0; i < 6; i++) await Promise.resolve()
      })
      expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument()
    } finally {
      Storage.prototype.setItem = orig
    }

    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    expect(goalBox().value).toBe('only in-memory started goal')
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(goalBox().value).toBe('only in-memory started goal')
  })

  it('keeps the only local draft when a full server store immediately evicts its migration', async () => {
    const stamp = testNowMs() - 1_000
    const localDraft = { message: 'only local copy', idleSecs: 120, maxCycles: 3 }
    saveGoalDraft(SLOT, localDraft, stamp)
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent("Couldn't sync this draft"))
    expect(screen.getByTestId('goal-draft-sync-error')).toHaveAttribute('role', 'alert')
    expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement()
    expect(goalBox().value).toBe('only local copy')
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({ draft: localDraft, updatedAt: stamp })
  })

  it('does not create a local tombstone when neither side has a draft', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve({
      ok: true,
      json: () => Promise.resolve(
        String(url).startsWith('/api/crons')
          ? { jobs: [] }
          : { draft: null, updated_at: 0 },
      ),
    })) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    expect(goalRecordKeys()).toEqual([])
  })

  it('caches a lower canonical timestamp when it answers the submitted edit', async () => {
    const canonicalAt = testNowMs() - 1_000
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body))
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: canonicalAt }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: canonicalAt - 1 }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    const view = renderPopover(null)
    await waitFor(() => expect(loadGoalDraftSnapshot(SLOT).updatedAt).toBe(canonicalAt - 1))
    fireEvent.change(goalBox(), { target: { value: 'edit from a future-skewed browser' } })
    view.unmount()

    await waitFor(() => expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: { message: 'edit from a future-skewed browser', idleSecs: 60, maxCycles: 0 },
      updatedAt: canonicalAt,
    }))
  })

  it('keeps a lower-timestamp server-confirmed canonical in memory when its write-back fails, so a future-dated stale copy cannot win during a GET outage', async () => {
    // F1 (residual/crash-data-loss): a future-dated stale storage record exists
    // -> a new edit reaches the server -> the lower-timestamp canonical write-back
    // fails -> remount GET also fails. Provenance, not the skewed clock, must keep
    // the confirmed canonical in the editor so Start cannot launch stale text.
    const staleFutureStamp = testNowMs() + 5 * 60_000
    const canonicalStamp = testNowMs() + 1_000
    // A stale copy already sits in localStorage; it must never resurface once the
    // server has confirmed a newer edit.
    saveGoalDraft(SLOT, { message: 'stale stored copy', idleSecs: 60, maxCycles: 0 }, staleFutureStamp)

    let getShouldFail = false
    let resolvePut!: () => void
    const puts: Record<string, unknown>[] = []
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
        // Hold the canonical until the test has counted the failed edit write.
        return new Promise(resolve => {
          resolvePut = () => resolve({
            ok: true,
            json: () => Promise.resolve({
              draft: { message: 'canonical from server', idle_secs: 60, max_cycles: 0 },
              updated_at: canonicalStamp,
            }),
          })
        })
      }
      // GET: returns the stale copy on first mount, then fails on remount.
      if (getShouldFail) {
        return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ error: 'down' }) })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'stale stored copy', idle_secs: 60, max_cycles: 0 },
          updated_at: staleFutureStamp,
        }),
      })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    const first = renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())

    // localStorage now refuses every write, so both the edit and the
    // server-confirmed canonical can only live in the module's in-memory snapshot.
    // Count body-write attempts. The PUT stays held until the edit attempt is
    // recorded, so any later increment belongs to cacheCanonical's write-back.
    let setItemAttempts = 0
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) {
        setItemAttempts += 1
        throw new Error('QuotaExceeded')
      }
      return orig.call(this, k, v)
    }
    try {
      fireEvent.change(goalBox(), { target: { value: 'recoverable edit' } })
      await waitFor(() => expect(puts).toHaveLength(1))
      const editAttempts = setItemAttempts
      await act(async () => {
        resolvePut()
        await Promise.resolve()
      })
      // Wait until cacheCanonical attempts its own failing write-back.
      await waitFor(() => expect(setItemAttempts).toBeGreaterThan(editAttempts))
    } finally {
      Storage.prototype.setItem = orig
    }

    // localStorage never took a new write, so it still holds only the stale copy.
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: { message: 'stale stored copy', idleSecs: 60, maxCycles: 0 },
      updatedAt: staleFutureStamp,
    })

    // Remount with the shared server copy unreachable: the only surviving newest
    // copy is the in-memory canonical. Under the bug (unconditional delete after a
    // failed re-save) this would show 'stale stored copy'.
    first.unmount()
    getShouldFail = true
    renderPopover(null)
    expect(goalBox().value).toBe('canonical from server')
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(goalBox().value).toBe('canonical from server')
  })

  it('a delayed tab\'s older canonical never downgrades a newer edit that landed meanwhile (delayed tab + quota + GET failure)', async () => {
    // F1 (residual/crash-data-loss-corruption, span f9eb25b1dcc6): tab A edits,
    // its localStorage write fails (quota) so the edit lives only in the module's
    // in-memory fallback (older), and its PUT is slow. Tab B (same browser) writes
    // a NEWER draft to shared localStorage. Tab A's delayed PUT then returns the
    // canonical for its OLDER submission. It must NOT overwrite tab B's newer
    // localStorage copy, and a reopen during the outage (GET fails) must show the
    // newer copy -- never tab A's stale fallback.
    const olderStamp = testNowMs()
    const newerStamp = olderStamp + 5_000

    // The shared server copy is unreachable throughout (outage): every GET fails,
    // so reconcile-on-open can only fall back to the local record.
    let resolvePut!: (v: { ok: boolean; json: () => Promise<unknown> }) => void
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        return new Promise(resolve => { resolvePut = resolve })
      }
      return Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ error: 'down' }) })
    })
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch)

    const first = renderPopover(null)
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())

    // Tab A edits while localStorage refuses writes: the edit survives only in the
    // in-memory fallback (older), and its PUT is enqueued but held unresolved.
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.change(goalBox(), { target: { value: 'tab A older edit' } })
      await waitFor(() => expect(
        fetchMock.mock.calls.some(c => (c[1] as RequestInit | undefined)?.method === 'PUT'),
      ).toBe(true))
    } finally {
      Storage.prototype.setItem = orig
    }
    // The edit never reached localStorage; it lives only in the in-memory fallback.
    expect(loadGoalDraftSnapshot(SLOT).updatedAt).toBe(0)

    // Tab B is a separate JS realm: update the shared localStorage keys
    // directly so tab A's module-local timestamp cache remains stale.
    localStorage.setItem(
      `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      JSON.stringify({ [SLOT]: newerStamp }),
    )
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({
        [SLOT]: { message: 'tab B newer edit', idleSecs: 60, maxCycles: 0 },
      }),
    )
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: { message: 'tab B newer edit', idleSecs: 60, maxCycles: 0 },
      updatedAt: newerStamp,
    })

    // Tab A's delayed PUT now resolves with the canonical for its OLDER submission.
    await act(async () => {
      resolvePut({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'tab A older edit', idle_secs: 60, max_cycles: 0 },
          updated_at: olderStamp + 1,
        }),
      })
      // Drain the react-query mutation resolution AND the `.then(cacheCanonical)`
      // tail (a macrotask, not just microtasks) so the retain/drop decision is
      // deterministically made before we assert or remount.
      await new Promise(resolve => setTimeout(resolve, 0))
      await Promise.resolve()
    })

    // The newer localStorage copy must be intact -- not downgraded to tab A's
    // older canonical. Under the bug the pending-timestamp branch fires and
    // overwrites the durable copy with the delayed older canonical. This is the
    // durable record a fresh tab (or a reload/Retry) reads while the server is
    // unreachable -- the GET-failure reopen the finding describes -- so keeping
    // it correct is what stops Start from launching the older goal.
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: { message: 'tab B newer edit', idleSecs: 60, maxCycles: 0 },
      updatedAt: newerStamp,
    })
    first.unmount()
  })

  it('a delayed GET reconciles a newer local record through the server', async () => {
    const firstStamp = testNowMs()
    const newerStamp = firstStamp + 5_000
    saveGoalDraft(
      SLOT,
      { message: 'tab A initial draft', idleSecs: 60, maxCycles: 0 },
      firstStamp,
    )

    let resolveGet!: (value: { ok: boolean; json: () => Promise<unknown> }) => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body))
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
        })
      }
      return new Promise(resolve => { resolveGet = resolve })
    }) as unknown as typeof fetch)

    renderPopover(null)
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent(
      'Checking saved goal…',
    )

    // A separate tab appends a newer immutable record while tab A's GET waits.
    // The delayed response must re-read and preserve that record.
    saveGoalDraft(
      SLOT,
      { message: 'tab B newer draft', idleSecs: 60, maxCycles: 0 },
      newerStamp,
    )

    await act(async () => {
      resolveGet({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'older remote canonical', idle_secs: 60, max_cycles: 0 },
          updated_at: firstStamp + 1_000,
        }),
      })
      await Promise.resolve()
    })

    await waitFor(() => expect(goalBox().value).toBe('tab B newer draft'))
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: { message: 'tab B newer draft', idleSecs: 60, maxCycles: 0 },
      updatedAt: newerStamp,
    })
  })

  it('uses the canonical server copy when equal timestamps have different content', async () => {
    const stamp = testNowMs()
    saveGoalDraft(SLOT, { message: 'offline local draft', idleSecs: 60, maxCycles: 0 }, stamp)
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'canonical server draft', idle_secs: 90, max_cycles: 4 },
          updated_at: stamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)

    await waitFor(() => expect(goalBox().value).toBe('canonical server draft'))
    expect(puts).toHaveLength(0)
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: { message: 'canonical server draft', idleSecs: 90, maxCycles: 4 },
      updatedAt: stamp,
    })
  })

  it.each([
    {
      name: 'draft to canonical tombstone',
      local: { message: 'draft rejected by canonical clear', idleSecs: 60, maxCycles: 0 },
      canonical: null,
      expected: null,
    },
    {
      name: 'tombstone to canonical draft',
      local: null,
      canonical: { message: 'canonical draft beats clear', idleSecs: 90, maxCycles: 4 },
      expected: 'canonical draft beats clear',
    },
  ])('applies a newer server canonical of the opposite mode on open: $name', async ({ local, canonical, expected }) => {
    const updatedAt = testNowMs() + 1_000
    saveGoalDraft(SLOT, local, updatedAt)
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)) as Record<string, unknown>)
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ draft: null, updated_at: 0 }) })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: canonical && {
            message: canonical.message,
            idle_secs: canonical.idleSecs,
            max_cycles: canonical.maxCycles,
          },
          updated_at: updatedAt + 1,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => {
      if (expected) expect(goalBox().value).toBe(expected)
      else expect(goalBox().value).toContain('north star')
    })
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Updated from another device.')
    expect(puts).toHaveLength(0)
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: canonical,
      updatedAt: updatedAt + 1,
    })
  })

  it('never lets a late server response overwrite text typed after open', async () => {
    const now = testNowMs()
    let resolveDraft!: (value: { ok: boolean; json: () => Promise<unknown> }) => void
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      return new Promise(resolve => { resolveDraft = resolve })
    }) as unknown as typeof fetch)

    renderPopover(null)
    fireEvent.change(goalBox(), { target: { value: 'typing on mobile now' } })
    await act(async () => {
      resolveDraft({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'desktop response arrived late', idle_secs: 60, max_cycles: 0 },
          updated_at: now + 1_000,
        }),
      })
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(goalBox().value).toBe('typing on mobile now')
  })
  it('opening with a live loop never writes the loop config into the draft store', () => {
    vi.useFakeTimers()
    // No stored draft. Open with a live loop, let any timer fire, then close.
    const view = renderPopover(makeLoop())
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    view.unmount()
    // The live loop's config must NOT have been mirrored into the user-draft store.
    expect(loadGoalDraft(SLOT)).toBeNull()
  })

  it('editing while a loop is running does not persist to the draft store (loop is authoritative)', () => {
    vi.useFakeTimers()
    const view = renderPopover(makeLoop())
    fireEvent.change(goalBox(), { target: { value: 'tweaked while running' } })
    act(() => { vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS) })
    view.unmount()
    expect(loadGoalDraft(SLOT)).toBeNull()
  })

  it('pausing a loop leaves the remembered inactive draft unchanged', async () => {
    saveGoalDraft(SLOT, { message: 'remembered before start', idleSecs: 120, maxCycles: 5 })
    renderPopover(makeLoop({ message: 'live loop config', idle_secs: 90, max_cycles: 3 }))

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Pause loop' }))
    })

    expect(loadGoalDraft(SLOT)).toEqual({
      message: 'remembered before start', idleSecs: 120, maxCycles: 5,
    })
  })

  it('falsy loop fields fall back to default template / 60 / 0, not bare "" / 0 (|| not ??)', () => {
    // A loop with an empty message and idle_secs/max_cycles of 0 must show the
    // default template + 60 — falsy loop fields fall back (|| not ??).
    renderPopover(makeLoop({ message: '', idle_secs: 0, max_cycles: 0 }))
    expect(goalBox().value).toContain('north star')
    expect((screen.getByDisplayValue('60') as HTMLInputElement).value).toBe('60')
  })
})
