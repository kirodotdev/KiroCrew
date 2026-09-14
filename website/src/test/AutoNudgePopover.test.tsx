import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import {
  __resetForTests,
  GOAL_DRAFT_RECORD_PREFIX,
  GOAL_DRAFT_MAX_MESSAGE_CHARS,
  GOAL_DRAFT_TTL_MS,
  LEGACY_GOAL_DRAFTS_KEY,
  loadGoalDraft,
  loadGoalDraftSnapshot,
  saveGoalDraft,
} from '../utils/goalDrafts'
import { DRAFT_SAVE_DEBOUNCE_MS } from '../utils/draftConstants'

const SLOT = 'chat-1-100'

const goalRecordKeys = () => Array.from({ length: localStorage.length }, (_, index) => (
  localStorage.key(index)
)).filter((key): key is string => key?.startsWith(GOAL_DRAFT_RECORD_PREFIX) === true)

const goalStorageSnapshot = () => goalRecordKeys()
  .sort()
  .map(key => [key, localStorage.getItem(key)])

const isGoalRecordKey = (key: string) => key.startsWith(GOAL_DRAFT_RECORD_PREFIX)

function renderPopover(loop: AutoNudgeLoop | null, slotKey = SLOT) {
  // A FRESH client per render: the popover reads the shared `cron-jobs` key, and
  // a client reused across tests would serve one test's stubbed rows to the next.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover
        slotKey={slotKey}
        loop={loop}
        open={true}
        onOpenChange={() => {}}
        onChange={() => {}}
      />
    </QueryClientProvider>,
  )
}

function renderStatefulPopover(loop: AutoNudgeLoop, slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  function Harness() {
    const [currentLoop, setCurrentLoop] = useState<AutoNudgeLoop | null>(loop)
    const [open, setOpen] = useState(true)
    return (
      <>
        <output data-testid="popover-open">{String(open)}</output>
        <AutoNudgePopover
          slotKey={slotKey}
          loop={currentLoop}
          open={open}
          onOpenChange={setOpen}
          onChange={setCurrentLoop}
        />
      </>
    )
  }
  return render(
    <QueryClientProvider client={qc}>
      <Harness />
    </QueryClientProvider>,
  )
}

const makeLoop = (over: Partial<AutoNudgeLoop> = {}): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'active loop goal',
  idle_secs: 90, max_cycles: 3, cycle_count: 1, active: true, last_fire_ts: 0,
  next_due_ts: 0, ...over,
})

describe('AutoNudgePopover goal persistence', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    // The popover fetches on OPEN (reads /api/crons to list this slot's
    // watches) and on Save/Stop. Stub so nothing escapes the test.
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const goalBox = () => screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i) as HTMLTextAreaElement
  const publishCrossTabSnapshot = (
    slot: string,
    draft: { message: string; idleSecs: number; maxCycles: number } | null,
    updatedAt: number,
    duplicates = 1,
  ) => {
    const priorKeys = new Set(goalRecordKeys())
    saveGoalDraft(slot, draft, updatedAt)
    const recordKey = goalRecordKeys().find(key => !priorKeys.has(key))
    if (!recordKey) throw new Error('cross-tab fixture did not append a record')
    for (let i = 0; i < duplicates; i++) {
      window.dispatchEvent(new StorageEvent('storage', {
        key: recordKey,
        newValue: localStorage.getItem(recordKey),
        storageArea: localStorage,
      }))
    }
  }
  const publishLegacyCrossTabSnapshot = (
    slot: string,
    draft: { message: string; idleSecs: number; maxCycles: number } | null,
    updatedAt: number,
    duplicates = 1,
  ) => {
    localStorage.setItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`, JSON.stringify({ [slot]: updatedAt }))
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [slot]: draft ?? { deleted: true } }),
    )
    for (let i = 0; i < duplicates; i++) {
      for (const key of [`${LEGACY_GOAL_DRAFTS_KEY}-ts`, LEGACY_GOAL_DRAFTS_KEY]) {
        window.dispatchEvent(new StorageEvent('storage', {
          key,
          newValue: localStorage.getItem(key),
          storageArea: localStorage,
        }))
      }
    }
  }

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

  it.each([7_999, 8_000, 8_001])(
    'keeps the goal editor remotely representable at %i ASCII characters',
    length => {
      renderPopover(null)
      fireEvent.change(goalBox(), { target: { value: 'x'.repeat(length) } })

      const expectedLength = Math.min(length, GOAL_DRAFT_MAX_MESSAGE_CHARS)
      expect(goalBox().value).toBe('x'.repeat(expectedLength))
      expect(screen.getByTestId('goal-message-character-count')).toHaveTextContent(
        `${expectedLength} / ${GOAL_DRAFT_MAX_MESSAGE_CHARS}`,
      )
      if (expectedLength === GOAL_DRAFT_MAX_MESSAGE_CHARS) {
        expect(screen.getByText(/Maximum 8000 characters/i)).toBeInTheDocument()
      }
    },
  )

  it('counts multibyte input by Unicode code point instead of UTF-16 units', () => {
    renderPopover(null)
    fireEvent.change(goalBox(), {
      target: { value: '😀'.repeat(GOAL_DRAFT_MAX_MESSAGE_CHARS + 1) },
    })

    expect(goalBox().value).toBe('😀'.repeat(GOAL_DRAFT_MAX_MESSAGE_CHARS))
    expect(goalBox().value.length).toBe(GOAL_DRAFT_MAX_MESSAGE_CHARS * 2)
    expect(screen.getByTestId('goal-message-character-count')).toHaveTextContent(
      `${GOAL_DRAFT_MAX_MESSAGE_CHARS} / ${GOAL_DRAFT_MAX_MESSAGE_CHARS}`,
    )
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
    const now = Date.now()
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
    const canonicalAt = Date.now()
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
    const now = Date.now()
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
    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(
      "Couldn't sync this draft",
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(goalBox().value).toBe('recovered desktop goal'))
    expect(reads).toBe(2)
  })

  it('migrates a newer local draft without letting the stale server copy win', async () => {
    const now = Date.now()
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
    const now = Date.now()
    saveGoalDraft(SLOT, { message: 'stale mobile goal', idleSecs: 60, maxCycles: 0 }, now - 5_000)
    let resolveDraft!: (value: { ok: boolean; json: () => Promise<unknown> }) => void
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      return new Promise(resolve => { resolveDraft = resolve })
    }) as unknown as typeof fetch)

    renderPopover(null)
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Checking other devices…')
    expect(screen.getByRole('button', { name: /Start loop/i })).toBeDisabled()

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
  })

  it('serializes a first-sync migration before a live edit from the same slot', async () => {
    const now = Date.now()
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
    const now = Date.now()
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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    fireEvent.change(goalBox(), { target: { value: 'first mount edit' } })
    first.unmount()
    await waitFor(() => expect(puts).toHaveLength(1))

    saveGoalDraft(SLOT, { message: 'second mount local copy', idleSecs: 60, maxCycles: 0 }, now + 2)
    const second = renderPopover(null)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(puts).toHaveLength(1)

    resolveFirstPut()
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[0].migration).toBe(false)
    expect(puts[1].migration).toBe(true)
    second.unmount()
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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())

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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
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

  it('retries from the in-memory edit when local and remote persistence both fail', async () => {
    const now = Date.now()
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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())

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
    const oldStamp = Date.now()
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
    const oldStamp = Date.now()
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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
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

  it('keeps a local clear-write failure visible instead of closing as if both stores cleared', async () => {
    const slot = 'chat-clear-local-failure'
    const oldStamp = Date.now() - 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))

    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
      expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
      expect(goalBox().value).toContain('north star')
    } finally {
      Storage.prototype.setItem = orig
    }

    expect(loadGoalDraftSnapshot(slot)).toEqual({
      draft: { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 },
      updatedAt: oldStamp,
    })
  })

  it('keeps a remote clear-write failure visible with a durable local tombstone', async () => {
    const slot = 'chat-clear-remote-failure'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      return Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ error: 'temporarily unavailable' }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))

    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toContain('north star')
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
    expect(loadGoalDraftSnapshot(slot).updatedAt).toBeGreaterThan(0)
  })

  it('closes only after the clear tombstone is durable locally and remotely', async () => {
    const slot = 'chat-clear-success'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    let clearPut: Record<string, unknown> | null = null
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      clearPut = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: clearPut.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))

    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(clearPut).toMatchObject({ draft: null, migration: false })
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
    expect(loadGoalDraftSnapshot(slot).updatedAt).toBeGreaterThan(0)
  })

  it('keeps and queues an edit typed while loop deletion is in flight', async () => {
    const slot = 'chat-clear-delete-edit'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    const puts: Record<string, unknown>[] = []
    let resolveDelete!: () => void
    let deleteStarted = false
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        deleteStarted = true
        return new Promise(resolve => {
          resolveDelete = () => resolve({ ok: true, json: () => Promise.resolve({}) })
        })
      }
      if (!init?.method) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: null, updated_at: 0 }),
        })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(deleteStarted).toBe(true))

    fireEvent.change(goalBox(), { target: { value: 'edit while deleting loop' } })
    resolveDelete()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toBe('edit while deleting loop')
    expect(puts[0]).toMatchObject({ draft: null, migration: false })
    expect((puts[1].draft as Record<string, unknown>).message).toBe('edit while deleting loop')
    expect(loadGoalDraft(slot)?.message).toBe('edit while deleting loop')
  })

  it.each([
    {
      name: 'idle seconds',
      slot: 'chat-clear-delete-idle-edit',
      label: 'Seconds between nudges',
      value: '321',
      property: 'idle_secs',
      expected: 321,
    },
    {
      name: 'max cycles',
      slot: 'chat-clear-delete-cycle-edit',
      label: 'Max cycles (0 = infinite)',
      value: '7',
      property: 'max_cycles',
      expected: 7,
    },
  ])('keeps a $name edit typed while loop deletion is in flight', async ({
    slot, label, value, property, expected,
  }) => {
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 90, maxCycles: 3 })
    const puts: Record<string, unknown>[] = []
    let resolveDelete!: () => void
    let deleteStarted = false
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        deleteStarted = true
        return new Promise(resolve => {
          resolveDelete = () => resolve({ ok: true, json: () => Promise.resolve({}) })
        })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      puts.push(sent)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({
      slot_key: slot,
      active: false,
      message: 'stale durable goal',
      idle_secs: 90,
      max_cycles: 3,
    }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(deleteStarted).toBe(true))

    fireEvent.change(screen.getByLabelText(label), { target: { value } })
    resolveDelete()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(screen.getByLabelText(label)).toHaveValue(Number(value))
    expect((puts[1].draft as Record<string, unknown>)[property]).toBe(expected)
  })

  it('keeps a newer edit visible through remote clear success and remount', async () => {
    const slot = 'chat-clear-newer-edit'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    const puts: Record<string, unknown>[] = []
    let canonicalDraft: Record<string, unknown> | null = null
    let canonicalAt = 0
    let resolveClear!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!init?.method) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: canonicalDraft, updated_at: canonicalAt }),
        })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      if (puts.length === 1) {
        return new Promise(resolve => {
          resolveClear = () => resolve({
            ok: true,
            json: () => Promise.resolve({ draft: null, updated_at: sent.updated_at }),
          })
        })
      }
      canonicalDraft = sent.draft as Record<string, unknown>
      canonicalAt = Number(sent.updated_at)
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: canonicalDraft, updated_at: canonicalAt }),
      })
    }) as unknown as typeof fetch)

    const first = renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'newer edit during clear' } })
    resolveClear()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toBe('newer edit during clear')
    expect(puts[0]).toMatchObject({ draft: null, migration: false })
    expect((puts[1].draft as Record<string, unknown>).message).toBe('newer edit during clear')
    expect(loadGoalDraft(slot)?.message).toBe('newer edit during clear')

    first.unmount()
    renderPopover(null, slot)
    expect(goalBox().value).toBe('newer edit during clear')
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    expect(goalBox().value).toBe('newer edit during clear')
  })

  it('ignores a failed clear completion after a newer edit is already queued', async () => {
    const slot = 'chat-clear-failure-newer-edit'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    const puts: Record<string, unknown>[] = []
    let resolveClear!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      puts.push(sent)
      if (puts.length === 1) {
        return new Promise(resolve => {
          resolveClear = () => resolve({
            ok: false,
            status: 503,
            json: () => Promise.resolve({ error: 'temporarily unavailable' }),
          })
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'edit after local clear' } })
    resolveClear()

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toBe('edit after local clear')
    expect((puts[1].draft as Record<string, unknown>).message).toBe('edit after local clear')
    expect(loadGoalDraft(slot)?.message).toBe('edit after local clear')
  })

  it('retries the newer edit instead of the failed clear tombstone', async () => {
    const slot = 'chat-clear-failure-retry-edit'
    const oldStamp = Date.now() - 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    const puts: Record<string, unknown>[] = []
    let remoteHealthy = false
    let resolveClear!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!init?.method) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            draft: { message: 'stale durable goal', idle_secs: 60, max_cycles: 0 },
            updated_at: oldStamp,
          }),
        })
      }
      const sent = JSON.parse(String(init.body)) as Record<string, unknown>
      puts.push(sent)
      if (puts.length === 1) {
        return new Promise(resolve => {
          resolveClear = () => resolve({
            ok: false,
            status: 503,
            json: () => Promise.resolve({ error: 'temporarily unavailable' }),
          })
        })
      }
      if (!remoteHealthy) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(puts).toHaveLength(1))

    fireEvent.change(goalBox(), { target: { value: 'retry this newer edit' } })
    resolveClear()

    await waitFor(() => expect(puts).toHaveLength(2))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(goalBox().value).toBe('retry this newer edit')
    expect(loadGoalDraft(slot)?.message).toBe('retry this newer edit')

    remoteHealthy = true
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))

    await waitFor(() => expect(puts).toHaveLength(3))
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect((puts[2].draft as Record<string, unknown>).message).toBe('retry this newer edit')
    expect(goalBox().value).toBe('retry this newer edit')
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
  })

  it('preserves a failed clear tombstone and visible failure across remount', async () => {
    const slot = 'chat-clear-remount'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      return Promise.resolve({
        ok: false,
        status: 503,
        json: () => Promise.resolve({ error: 'temporarily unavailable' }),
      })
    }) as unknown as typeof fetch)

    const first = renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    first.unmount()

    renderPopover(null, slot)
    expect(goalBox().value).toContain('north star')
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
  })

  it('keeps Retry failed when a local clear failure is followed by a remote failure', async () => {
    const slot = 'chat-clear-local-then-remote'
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 })
    let retrying = false
    let retryReads = 0
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (retrying && !init?.method) {
        retryReads += 1
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      const sent = JSON.parse(String(init?.body)) as Record<string, unknown>
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: sent.updated_at }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    } finally {
      Storage.prototype.setItem = orig
    }

    retrying = true
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    await waitFor(() => expect(retryReads).toBe(1))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(goalBox().value).toContain('north star')
  })

  it('keeps Retry failed when a remote clear failure is followed by a local failure', async () => {
    const slot = 'chat-clear-remote-then-local'
    const oldStamp = Date.now() - 5_000
    saveGoalDraft(slot, { message: 'stale durable goal', idleSecs: 60, maxCycles: 0 }, oldStamp)
    let retrying = false
    let retryPut: Record<string, unknown> | null = null
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'DELETE') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) })
      }
      if (!retrying && init?.method === 'PUT') {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: () => Promise.resolve({ error: 'temporarily unavailable' }),
        })
      }
      if (retrying && init?.method === 'PUT') {
        retryPut = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: null, updated_at: retryPut.updated_at }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'stale durable goal', idle_secs: 60, max_cycles: 0 },
          updated_at: oldStamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' }))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())

    retrying = true
    const orig = Storage.prototype.setItem
    Storage.prototype.setItem = function(k: string, v: string) {
      if (isGoalRecordKey(k)) throw new Error('QuotaExceeded')
      return orig.call(this, k, v)
    }
    try {
      fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
      await waitFor(() => expect(retryPut).not.toBeNull())
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
      expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
      expect(goalBox().value).toContain('north star')
    } finally {
      Storage.prototype.setItem = orig
    }

    expect(loadGoalDraftSnapshot(slot).draft).toBeNull()
  })

  it('keeps the only local draft when a full server store immediately evicts its migration', async () => {
    const stamp = Date.now() - 1_000
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
    expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull()
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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    expect(goalRecordKeys()).toEqual([])
  })

  it('caches a lower canonical timestamp when it answers the submitted edit', async () => {
    const canonicalAt = Date.now() - 1_000
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
    const staleFutureStamp = Date.now() + 5 * 60_000
    const canonicalStamp = Date.now() + 1_000
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
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())

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
    const olderStamp = Date.now()
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

  it('a delayed GET cannot overwrite a newer cross-tab local draft', async () => {
    const firstStamp = Date.now()
    const newerStamp = firstStamp + 5_000
    saveGoalDraft(
      SLOT,
      { message: 'tab A initial draft', idleSecs: 60, maxCycles: 0 },
      firstStamp,
    )

    let resolveGet!: (value: { ok: boolean; json: () => Promise<unknown> }) => void
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      return new Promise(resolve => { resolveGet = resolve })
    }) as unknown as typeof fetch)

    renderPopover(null)
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent(
      'Checking other devices…',
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

  it.each([
    {
      name: 'distinct draft bodies',
      local: { message: 'offline local draft', idleSecs: 60, maxCycles: 0 },
      remote: { message: 'equal-stamp remote draft', idleSecs: 90, maxCycles: 4 },
    },
    {
      name: 'local draft versus remote tombstone',
      local: { message: 'offline local draft', idleSecs: 60, maxCycles: 0 },
      remote: null,
    },
    {
      name: 'local tombstone versus remote draft',
      local: null,
      remote: { message: 'equal-stamp remote draft', idleSecs: 90, maxCycles: 4 },
    },
  ])('reissues equal-timestamp offline content: $name', async ({ local, remote }) => {
    const stamp = Date.now()
    saveGoalDraft(SLOT, local, stamp)
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
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
        json: () => Promise.resolve({
          draft: remote ? {
            message: remote.message,
            idle_secs: remote.idleSecs,
            max_cycles: remote.maxCycles,
          } : null,
          updated_at: stamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)

    await waitFor(() => expect(puts).toHaveLength(1))
    expect(Number(puts[0].updated_at)).toBe(stamp + 1)
    expect(puts[0].draft).toEqual(local ? {
      message: local.message,
      idle_secs: local.idleSecs,
      max_cycles: local.maxCycles,
    } : null)
    await waitFor(() => expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: local,
      updatedAt: stamp + 1,
    }))
    if (local) expect(goalBox().value).toBe(local.message)
    else expect(goalBox().value).toContain('north star')
  })

  it('reissues again when another client takes the first bumped timestamp', async () => {
    const stamp = Date.now()
    const local = { message: 'offline local draft', idleSecs: 60, maxCycles: 0 }
    saveGoalDraft(SLOT, local, stamp)
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            draft: { message: 'rival equal-stamp draft', idle_secs: 90, max_cycles: 4 },
            updated_at: sent.updated_at,
          }) })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
        })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        draft: { message: 'initial remote draft', idle_secs: 90, max_cycles: 4 },
        updated_at: stamp,
      }) })
    }) as unknown as typeof fetch)

    renderPopover(null)

    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts.map(put => put.updated_at)).toEqual([stamp + 1, stamp + 2])
    expect(puts.every(put => (put.draft as Record<string, unknown>).message === local.message)).toBe(true)
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({ draft: local, updatedAt: stamp + 2 })
    expect(goalBox().value).toBe(local.message)
  })

  it('queues an accepted cross-tab draft for remote sync and adopts its canonical', async () => {
    const updatedAt = Date.now() + 1_000
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: updatedAt + 1 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    await act(async () => {
      publishCrossTabSnapshot(
        SLOT,
        { message: 'newer cross-tab goal', idleSecs: 90, maxCycles: 4 },
        updatedAt,
      )
    })

    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toMatchObject({ migration: true, updated_at: updatedAt })
    expect(goalBox().value).toBe('newer cross-tab goal')
    expect(loadGoalDraftSnapshot(SLOT).updatedAt).toBe(updatedAt + 1)
  })

  it('queues an accepted cross-tab tombstone and keeps the cleared editor live', async () => {
    const firstStamp = Date.now()
    const tombstoneStamp = firstStamp + 1_000
    saveGoalDraft(
      SLOT,
      { message: 'goal cleared elsewhere', idleSecs: 60, maxCycles: 0 },
      firstStamp,
    )
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: null, updated_at: tombstoneStamp + 1 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          draft: { message: 'goal cleared elsewhere', idle_secs: 60, max_cycles: 0 },
          updated_at: firstStamp,
        }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    await act(async () => { publishCrossTabSnapshot(SLOT, null, tombstoneStamp) })

    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toMatchObject({ draft: null, migration: true, updated_at: tombstoneStamp })
    expect(goalBox().value).toContain('north star')
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({ draft: null, updatedAt: tombstoneStamp + 1 })
  })

  it('queues a cross-tab update behind the slot write already in flight', async () => {
    const firstStamp = Date.now()
    const crossTabStamp = firstStamp + 5_000
    saveGoalDraft(
      SLOT,
      { message: 'first migration', idleSecs: 60, maxCycles: 0 },
      firstStamp,
    )
    const puts: Record<string, unknown>[] = []
    let resolveFirst!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        if (puts.length === 1) {
          return new Promise(resolve => {
            resolveFirst = () => resolve({
              ok: true,
              json: () => Promise.resolve({ draft: sent.draft, updated_at: firstStamp + 1 }),
            })
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: crossTabStamp + 1 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(puts).toHaveLength(1))
    await act(async () => {
      publishCrossTabSnapshot(
        SLOT,
        { message: 'cross-tab waits behind migration', idleSecs: 60, maxCycles: 0 },
        crossTabStamp,
      )
      await Promise.resolve()
    })
    expect(puts).toHaveLength(1)

    await act(async () => { resolveFirst(); await Promise.resolve() })
    await waitFor(() => expect(puts).toHaveLength(2))
    expect((puts[1].draft as Record<string, unknown>).message)
      .toBe('cross-tab waits behind migration')
    expect(goalBox().value).toBe('cross-tab waits behind migration')
  })

  it('keeps a failed cross-tab sync visible and retries the same snapshot', async () => {
    const updatedAt = Date.now() + 1_000
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
            status: 503,
            json: () => Promise.resolve({ error: 'temporarily unavailable' }),
          })
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ draft: sent.draft, updated_at: updatedAt + 1 }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    await act(async () => {
      publishCrossTabSnapshot(
        SLOT,
        { message: 'cross-tab retry goal', idleSecs: 60, maxCycles: 0 },
        updatedAt,
      )
    })
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toBeInTheDocument())
    expect(goalBox().value).toBe('cross-tab retry goal')

    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    await waitFor(() => expect(puts).toHaveLength(2))
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull())
    expect((puts[1].draft as Record<string, unknown>).message).toBe('cross-tab retry goal')
    expect(goalBox().value).toBe('cross-tab retry goal')
  })

  it.each(['raw', 'atomic'] as const)(
    'does not remount an expired timestamp-less %s migration after server eviction',
    async format => {
      const nowValue = Date.now()
      const now = vi.spyOn(Date, 'now').mockReturnValue(nowValue)
      const legacyDraft = { message: `${format} legacy migration`, idleSecs: 60, maxCycles: 0 }
      const payload = format === 'atomic'
        ? { __slotDraftStore: 1, drafts: { [SLOT]: legacyDraft }, timestamps: {} }
        : { [SLOT]: legacyDraft }
      localStorage.setItem(LEGACY_GOAL_DRAFTS_KEY, JSON.stringify(payload))
      const legacyBefore = localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)
      const puts: Record<string, unknown>[] = []
      vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
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
      }) as unknown as typeof fetch)

      const first = renderPopover(null)
      await waitFor(() => expect(puts).toHaveLength(1))
      expect(puts[0].updated_at).toBe(nowValue)
      expect((puts[0].draft as Record<string, unknown>).message).toBe(legacyDraft.message)
      first.unmount()

      now.mockReturnValue(nowValue + GOAL_DRAFT_TTL_MS + 1)
      saveGoalDraft(`${SLOT}-compaction-trigger`, {
        message: 'unrelated fresh slot',
        idleSecs: 60,
        maxCycles: 0,
      })
      __resetForTests()
      puts.length = 0
      renderPopover(null)
      await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
      expect(puts).toHaveLength(0)
      expect(goalBox().value).toContain('north star')
      expect(localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY)).toBe(legacyBefore)
      now.mockRestore()
    },
  )

  it('ignores an unpaired legacy sidecar event until the body commit arrives', async () => {
    const oldStamp = Date.now()
    const newStamp = oldStamp + 1_000
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
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
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [SLOT]: { message: 'stale legacy body', idleSecs: 60, maxCycles: 0 } }),
    )
    localStorage.setItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`, JSON.stringify({ [SLOT]: oldStamp }))

    localStorage.setItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`, JSON.stringify({ [SLOT]: newStamp }))
    window.dispatchEvent(new StorageEvent('storage', {
      key: `${LEGACY_GOAL_DRAFTS_KEY}-ts`,
      newValue: localStorage.getItem(`${LEGACY_GOAL_DRAFTS_KEY}-ts`),
      storageArea: localStorage,
    }))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(puts).toHaveLength(0)
    expect(goalBox().value).toContain('north star')

    localStorage.setItem(
      LEGACY_GOAL_DRAFTS_KEY,
      JSON.stringify({ [SLOT]: { message: 'paired legacy body', idleSecs: 90, maxCycles: 4 } }),
    )
    window.dispatchEvent(new StorageEvent('storage', {
      key: LEGACY_GOAL_DRAFTS_KEY,
      newValue: localStorage.getItem(LEGACY_GOAL_DRAFTS_KEY),
      storageArea: localStorage,
    }))

    await waitFor(() => expect(puts).toHaveLength(1))
    expect((puts[0].draft as Record<string, unknown>).message).toBe('paired legacy body')
    expect(puts[0].updated_at).toBe(newStamp)
  })

  it('folds duplicate storage events into one remote migration', async () => {
    const updatedAt = Date.now() + 1_000
    const puts: Record<string, unknown>[] = []
    let resolvePut!: () => void
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        return new Promise(resolve => {
          resolvePut = () => resolve({
            ok: true,
            json: () => Promise.resolve({ draft: sent.draft, updated_at: updatedAt + 1 }),
          })
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    await act(async () => {
      publishLegacyCrossTabSnapshot(
        SLOT,
        { message: 'one migration only', idleSecs: 60, maxCycles: 0 },
        updatedAt,
        2,
      )
      await Promise.resolve()
    })
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts).toHaveLength(1)
    await act(async () => { resolvePut(); await Promise.resolve() })
  })

  it('requeues a formerly canonical cross-tab draft when a fresh read shows server eviction', async () => {
    const updatedAt = Date.now() + 1_000
    const puts: Record<string, unknown>[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
        const sent = JSON.parse(String(init.body)) as Record<string, unknown>
        puts.push(sent)
        return Promise.resolve({
          ok: true,
          // Migration preserves the browser edit stamp. This makes the accepted
          // snapshot byte-identical to the prior canonical, so only the fresh
          // GET proving eviction can authorize a second queue entry.
          json: () => Promise.resolve({ draft: sent.draft, updated_at: sent.updated_at }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    const first = renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    await act(async () => {
      publishCrossTabSnapshot(
        SLOT,
        { message: 'canonical later evicted', idleSecs: 60, maxCycles: 0 },
        updatedAt,
      )
    })
    await waitFor(() => expect(puts).toHaveLength(1))
    first.unmount()

    renderPopover(null)
    await waitFor(() => expect(puts).toHaveLength(2))
    expect((puts[1].draft as Record<string, unknown>).message).toBe('canonical later evicted')
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
  ])('applies the server canonical when it has the opposite mode: $name', async ({ local, canonical, expected }) => {
    const updatedAt = Date.now() + 1_000
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/crons')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ jobs: [] }) })
      }
      if (init?.method === 'PUT') {
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
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ draft: null, updated_at: 0 }),
      })
    }) as unknown as typeof fetch)

    renderPopover(null)
    await waitFor(() => expect(screen.queryByTestId('goal-draft-sync-status')).toBeNull())
    await act(async () => { publishCrossTabSnapshot(SLOT, local, updatedAt) })

    await waitFor(() => {
      if (expected) expect(goalBox().value).toBe(expected)
      else expect(goalBox().value).toContain('north star')
    })
    expect(loadGoalDraftSnapshot(SLOT)).toEqual({
      draft: canonical,
      updatedAt: updatedAt + 1,
    })
  })

  it('never lets a late server response overwrite text typed after open', async () => {
    const now = Date.now()
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

  it('stopping a loop leaves the remembered inactive draft unchanged', async () => {
    saveGoalDraft(SLOT, { message: 'remembered before start', idleSecs: 120, maxCycles: 5 })
    renderPopover(makeLoop({ message: 'live loop config', idle_secs: 90, max_cycles: 3 }))

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Stop loop/i }))
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

describe('AutoNudgePopover number-field editing (idle / max cycles)', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  // Idle is the first number input, max-cycles the second (DOM order in the JSX).
  const fields = () => screen.getAllByRole('spinbutton') as HTMLInputElement[]
  const idleField = () => fields()[0]
  const cyclesField = () => fields()[1]

  it('allows clearing the idle field to empty while typing, then defaults to 60 on blur (the reported bug)', () => {
    renderPopover(null)
    expect(idleField().value).toBe('60')
    // The empty edit is allowed as-typed rather than snapping straight back to
    // 60 with the leading digit stuck...
    fireEvent.change(idleField(), { target: { value: '' } })
    expect(idleField().value).toBe('')
    // ...and only commits to the default when the field loses focus.
    fireEvent.blur(idleField())
    expect(idleField().value).toBe('60')
  })

  it('retypes idle 60 -> 30 without the leading digit sticking', () => {
    renderPopover(null)
    fireEvent.change(idleField(), { target: { value: '' } })
    fireEvent.change(idleField(), { target: { value: '30' } })
    expect(idleField().value).toBe('30')
    fireEvent.blur(idleField())
    expect(idleField().value).toBe('30')
  })

  it('empty max-cycles commits to 0 (infinity) on blur', () => {
    renderPopover(null)
    expect(cyclesField().value).toBe('0')
    fireEvent.change(cyclesField(), { target: { value: '' } })
    expect(cyclesField().value).toBe('')
    fireEvent.blur(cyclesField())
    expect(cyclesField().value).toBe('0')
  })

  it('clamps locally accepted values before saving or syncing them', async () => {
    renderPopover(null)
    fireEvent.change(idleField(), { target: { value: '172800' } })
    fireEvent.change(cyclesField(), { target: { value: '2147483648' } })
    fireEvent.blur(idleField())
    fireEvent.blur(cyclesField())

    expect(idleField().value).toBe('86400')
    expect(cyclesField().value).toBe('2147483647')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Start loop/i }))
    })
    const calls = (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
    const save = calls.find(call => call[1]?.method === 'POST' && call[1]?.body)
    const body = JSON.parse(String(save?.[1]?.body))
    expect(body.idle_secs).toBe(86400)
    expect(body.max_cycles).toBe(2147483647)
  })

  it('Save sends the typed idle value even without an intervening blur', async () => {
    renderPopover(null)
    fireEvent.change(idleField(), { target: { value: '45' } })
    // Click Start loop WITHOUT blurring the field first — save() must read the
    // raw string, not a stale committed number.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Start loop/i })) })
    // Select the call by URL, not by index: opening the popover also READS
    // /api/crons to list this slot's watches, so the save POST is no longer
    // call 0 and an index would pin an unrelated ordering.
    // The init arg is optional and its `body` is too: the /api/crons read is a
    // bare `fetch(url)` and a delete carries only `{ method }`, so `c[1]?.body`
    // below is load-bearing rather than defensive.
    const calls = (fetch as unknown as { mock: { calls: [string, { body?: string }?][] } }).mock.calls
    const save = calls.find(c => String(c[0]).startsWith('/api/autonudge') && c[1]?.body)
    expect(save, 'no /api/autonudge write was issued').toBeTruthy()
    const body = JSON.parse(save![1]!.body!)
    expect(body.idle_secs).toBe(45)
  })
})

describe('AutoNudgePopover trigger chip — interrupted state', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  const renderChip = (loop: AutoNudgeLoop | null, interrupted: boolean) => render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <AutoNudgePopover
        slotKey={SLOT}
        loop={loop}
        open={false}
        onOpenChange={() => {}}
        onChange={() => {}}
        interrupted={interrupted}
      />
    </QueryClientProvider>,
  )

  it('pulses while the loop is active and the session is healthy', () => {
    renderChip(makeLoop({ cycle_count: 47 }), false)
    const chip = screen.getByTitle('Goal active (cycle 47/3)')
    expect(chip.className).toContain('animate-pulse')
    expect(chip.textContent).toContain('47')
  })

  it('stops pulsing and explains itself when the last turn was interrupted (the reported bug)', () => {
    // The composer is showing Resume: nothing runs until the user acts or the
    // next idle-timer cycle fires, so a pulsing chip would claim active work
    // for that whole gap.
    renderChip(makeLoop({ cycle_count: 47 }), true)
    const chip = screen.getByTitle(/last turn was interrupted/)
    expect(chip.className).not.toContain('animate-pulse')
    // The cycle count survives — it is state, not a liveness claim.
    expect(chip.textContent).toContain('47')
  })

  it('ignores interrupted when no loop is active (plain set-a-goal chip)', () => {
    renderChip(null, true)
    const chip = screen.getByTitle('Set a goal')
    expect(chip.className).not.toContain('animate-pulse')
  })
})


describe('AutoNudgePopover — zero-token watches armed on this slot', () => {
  const cron = (over: Record<string, unknown> = {}) => ({
    id: 'j1',
    name: 'pr watch #6234',
    schedule: 'every 60s',
    next_run_ts: 1787816571,
    session_key: `dashboard:${SLOT}`,
    script: '~/.kiro/crew/crons/pr_watch.py:watch',
    enabled: true,
    ...over,
  })

  function stubCrons(rows: unknown[]) {
    // `{ jobs: [...] }` is the endpoint's real envelope. An earlier version of
    // these tests stubbed a bare array, which matched a wrong reader and hid a
    // section that never rendered against the live gateway -- the fixture has to
    // be the shape the server sends, or the test only proves the reader agrees
    // with itself.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(String(url).startsWith('/api/crons') ? { jobs: rows } : { loop: null }),
        }),
      ) as unknown as typeof fetch,
    )
  }

  beforeEach(() => { localStorage.clear(); __resetForTests() })
  afterEach(() => { vi.unstubAllGlobals() })

  /**
   * Render, then wait for the crons read to have been ANSWERED, not just issued.
   *
   * The section is populated by `fetch` -> `json()` -> `setState`, three promise
   * hops that `act` does not wait for, so a bare `await act(render)` samples the
   * popover before the answer lands. That made the positive test below flake
   * (1 in 5 full runs on a loaded host) and every "not listed" assertion in this
   * block vacuous: the section is absent BEFORE the fetch resolves whether or not
   * the filter works. Waiting on the mocked fetch having been called, then
   * draining the chain, makes both kinds of assertion about the rendered answer.
   */
  async function renderPopoverSettled() {
    await act(async () => { renderPopover(null) })
    const fetchMock = vi.mocked(fetch)
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(c => String(c[0]).startsWith('/api/crons'))).toBe(true),
    )
    for (let i = 0; i < 4; i++) {
      await act(async () => { await Promise.resolve() })
    }
  }

  it('lists a script cron this slot owns, so an armed watch is visible in chat', async () => {
    // The reported gap: a watch is deliberately NOT an autonudge loop, so the
    // popover showed "Set a goal" and nothing else while a watch was polling --
    // the one surface a user opens to confirm something is running.
    stubCrons([cron()])
    await renderPopoverSettled()
    expect(await screen.findByText(/Zero-token watches/i)).toBeTruthy()
    expect(screen.getByText('pr watch #6234')).toBeTruthy()
  })

  it('never lists a watch owned by a different slot', async () => {
    // Ownership goes through the shared `runBelongsToSlot`, which normalizes the
    // `dashboard:` namespace rather than demanding byte equality -- but the SLOT
    // must still match, and that is the property worth pinning: another
    // conversation's watch appearing here is worse than showing none.
    stubCrons([cron({ session_key: 'dashboard:chat-9-999', name: 'someone elses watch' })])
    await renderPopoverSettled()
    expect(screen.queryByText('someone elses watch')).toBeNull()
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
  })

  it('never lists a message-only cron under a zero-token heading', async () => {
    // A cron with no script wakes the agent every fire. Listing it here would
    // make the heading lie about what it costs.
    stubCrons([cron({ script: '', name: 'daily reminder' })])
    await renderPopoverSettled()
    expect(screen.queryByText('daily reminder')).toBeNull()
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
  })

  it('never lists a disabled watch as if it were armed', async () => {
    stubCrons([cron({ enabled: false, name: 'paused watch' })])
    await renderPopoverSettled()
    expect(screen.queryByText('paused watch')).toBeNull()
  })

  it('reads the jobs envelope the endpoint actually returns, not a bare array', async () => {
    // The live endpoint answers `{ jobs: [...] }` (handlers/cron.py). Reading a
    // bare array fails SILENTLY -- no error, the filter just never matches -- so
    // this pins the envelope rather than trusting the reader. Found by a pod
    // capture after the unit tests were green against the wrong fixture.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve(String(url).startsWith('/api/crons') ? [cron()] : { loop: null }),
        }),
      ) as unknown as typeof fetch,
    )
    await renderPopoverSettled()
    // A bare array is NOT the contract, so nothing should be read out of it.
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
  })

  it('stays silent when the read fails rather than banner-ing over the goal form', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        String(url).startsWith('/api/crons')
          ? Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) })
          : Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) }),
      ) as unknown as typeof fetch,
    )
    await renderPopoverSettled()
    expect(screen.queryByText(/Zero-token watches/i)).toBeNull()
    // The popover's actual job is still fully usable.
    expect(screen.getByPlaceholderText(/Describe what you want the agent to accomplish/i)).toBeTruthy()
  })
})

/** #6482: hovering the goal button / opening the popover shows a live countdown
 *  to the next trigger, computed from the loop's already-serialized next_due_ts. */
describe('AutoNudgePopover next-trigger countdown', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
    vi.useFakeTimers()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const nowSecs = () => Date.now() / 1000

  it('shows the countdown in the popover and the trigger tooltip, and it ticks', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() + 125 }))
    // 125s -> "2m 5s" (en narrow units via fmtDuration).
    expect(screen.getAllByText(/Next cycle in .*2.*m.*5.*s/i).length).toBeGreaterThan(0)
    const trigger = screen.getByRole('button', { name: /Goal active \(cycle 1\/3\)/i })
    expect(trigger.getAttribute('title')).toMatch(/Next cycle in/i)

    // One tick: the rendered remaining time decreases.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(screen.getAllByText(/Next cycle in .*2.*m.*4.*s/i).length).toBeGreaterThan(0)
  })

  it('drops the seconds digit above an hour', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() + 3_720 }))
    const line = screen.getAllByText(/Next cycle in/i)[0].textContent || ''
    expect(line).toMatch(/1.*h/i)
    expect(line).not.toMatch(/\ds\b/)
  })

  it('reads "due" instead of a negative countdown when the deadline elapsed mid-turn', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() - 5 }))
    expect(screen.getAllByText(/Next cycle due, fires after the current turn/i).length).toBeGreaterThan(0)
  })

  it('shows the unscheduled placeholder when next_due_ts is 0', () => {
    renderPopover(makeLoop({ next_due_ts: 0 }))
    expect(screen.getAllByText(/Next cycle not yet scheduled/i).length).toBeGreaterThan(0)
  })

  it('shows no countdown for an inactive loop', () => {
    renderPopover(makeLoop({ active: false, next_due_ts: nowSecs() + 300 }))
    expect(screen.queryByText(/Next cycle/i)).toBeNull()
  })

  /** Review finding: the countdown must stay OUT of aria-label — a per-second
   *  label change re-announces the button to screen readers. Title only. */
  it('keeps aria-label stable (countdown lives in title only)', () => {
    renderPopover(makeLoop({ next_due_ts: nowSecs() + 125 }))
    const trigger = screen.getByRole('button', { name: /Goal active \(cycle 1\/3\)/i })
    expect(trigger.getAttribute('aria-label')).not.toMatch(/Next cycle/i)
    expect(trigger.getAttribute('title')).toMatch(/Next cycle in/i)
  })

  /** Review finding: the 1s ticker is popover-open-only — a closed-but-armed
   *  loop must not re-render the toolbar button every second. Hover/focus
   *  refresh the snapshot instead, which is all a native tooltip can show. */
  it('does not tick while closed; hovering the trigger refreshes the tooltip', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const deadline = nowSecs() + 125
    render(
      <QueryClientProvider client={qc}>
        <AutoNudgePopover slotKey={SLOT} loop={makeLoop({ next_due_ts: deadline })} open={false} onOpenChange={() => {}} onChange={() => {}} />
      </QueryClientProvider>,
    )
    const trigger = screen.getByRole('button', { name: /Goal active \(cycle 1\/3\)/i })
    expect(trigger.getAttribute('title')).toMatch(/2.*m.*5.*s/i)

    // A minute passes with the popover closed: no interval is armed, so the
    // title still carries the mount-time snapshot...
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(trigger.getAttribute('title')).toMatch(/2.*m.*5.*s/i)

    // ...until a hover refreshes it to the current remaining time.
    fireEvent.mouseEnter(trigger)
    expect(trigger.getAttribute('title')).toMatch(/1.*m.*5.*s/i)
  })

  it('stops updating after the loop goes inactive (ticker torn down)', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    const deadline = nowSecs() + 125
    const props = { slotKey: SLOT, open: true, onOpenChange: () => {}, onChange: () => {} }
    const view = render(
      <QueryClientProvider client={qc}>
        <AutoNudgePopover {...props} loop={makeLoop({ next_due_ts: deadline })} />
      </QueryClientProvider>,
    )
    expect(screen.getAllByText(/Next cycle in/i).length).toBeGreaterThan(0)

    view.rerender(
      <QueryClientProvider client={qc}>
        <AutoNudgePopover {...props} loop={makeLoop({ active: false, next_due_ts: deadline })} />
      </QueryClientProvider>,
    )
    expect(screen.queryByText(/Next cycle/i)).toBeNull()
    // Advancing the clock after teardown must not resurrect it or throw.
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(screen.queryByText(/Next cycle/i)).toBeNull()
  })
})

/** #7410 residual 1: the cycle readout carries its cap, so a loop coasting
 *  toward its max_cycles backstop is visible before it silently stops. */
describe('AutoNudgePopover cycle cap readout', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const renderChip = (loop: AutoNudgeLoop | null, interrupted = false) => render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}>
      <AutoNudgePopover
        slotKey={SLOT}
        loop={loop}
        open={false}
        onOpenChange={() => {}}
        onChange={() => {}}
        interrupted={interrupted}
      />
    </QueryClientProvider>,
  )

  it('renders the cap beside the cycle number, so a loop nearing its backstop is visible before it stops', () => {
    // The reported gap: max_cycles reached the frontend but was never displayed,
    // so cycle 23 of 24 looked exactly like cycle 23 of an uncapped loop.
    renderChip(makeLoop({ cycle_count: 23, max_cycles: 24 }))
    const chip = screen.getByTitle('Goal active (cycle 23/24)')
    expect(chip.textContent).toContain('23/24')
    // Screen-reader users learn the cap too — it is state, not a live countdown.
    expect(chip.getAttribute('aria-label')).toBe('Goal active (cycle 23/24)')
  })

  it('renders a bare cycle count with no slash when max_cycles is 0, because an uncapped loop has no denominator to count toward', () => {
    renderChip(makeLoop({ cycle_count: 23, max_cycles: 0 }))
    const chip = screen.getByTitle('Goal active (cycle 23)')
    expect(chip.textContent).toContain('23')
    expect(chip.textContent).not.toContain('/')
    expect(chip.getAttribute('aria-label')).toBe('Goal active (cycle 23)')
  })

  it('carries the cap into the interrupted tooltip too, since an interrupted loop is still armed against that cap', () => {
    renderChip(makeLoop({ cycle_count: 12, max_cycles: 24 }), true)
    const chip = screen.getByTitle(/last turn was interrupted/)
    expect(chip.getAttribute('title')).toContain('cycle 12/24')
  })

  it('shows the capped readout in the popover header, not only on the chip', () => {
    renderPopover(makeLoop({ cycle_count: 3, max_cycles: 24 }))
    expect(screen.getByText('· cycle 3/24')).toBeTruthy()
  })

  it('keeps the capped aria-label static while the countdown ticks (a cap must not re-announce the button every second)', () => {
    // Pins the same contract as "keeps aria-label stable": the cap is derived
    // from cycle_count/max_cycles only, so an armed ticker changes the title and
    // leaves the label alone.
    vi.useFakeTimers()
    renderPopover(makeLoop({ cycle_count: 3, max_cycles: 24, next_due_ts: Date.now() / 1000 + 125 }))
    const trigger = screen.getByRole('button', { name: 'Goal active (cycle 3/24)' })
    expect(trigger.getAttribute('title')).toMatch(/Next cycle in/i)
    act(() => { vi.advanceTimersByTime(3_000) })
    expect(trigger.getAttribute('aria-label')).toBe('Goal active (cycle 3/24)')
    expect(trigger.getAttribute('aria-label')).not.toMatch(/Next cycle/i)
  })
})

describe('AutoNudgePopover Trigger nudge (#8212)', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  /** Local render helper: the shared one hardcodes no-op callbacks, and these
   *  tests are about what the press DOES to them.
   *
   *  `open` is CONTROLLED here, mirroring the real parent (ChatInput owns the
   *  flag and feeds it back). A fixed `open={true}` would make the harness pin
   *  the popover's presence, so "the edit survives" could not fail even if the
   *  code closed it -- the assertion would be about the fixture rather than the
   *  component. */
  const renderWith = (loop: AutoNudgeLoop | null, onChange = vi.fn()) => {
    const onOpenChange = vi.fn()
    const Harness = () => {
      const [open, setOpen] = useState(true)
      return (
        <AutoNudgePopover
          slotKey={SLOT}
          loop={loop}
          open={open}
          onOpenChange={v => { onOpenChange(v); setOpen(v) }}
          onChange={onChange}
        />
      )
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
    render(
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>,
    )
    return { onChange, onOpenChange }
  }

  const triggerButton = () => screen.queryByRole('button', { name: 'Trigger nudge' })

  it('offers the button while a loop is active', () => {
    renderWith(makeLoop())
    expect(triggerButton()).toBeTruthy()
  })

  it('offers it NOWHERE when no loop is running, so the affordance never appears without a subject', () => {
    renderWith(null)
    // Complement assertion rather than a bare negative on one node: a stale
    // render could leave the button somewhere else in the tree, and "the
    // button I looked for is absent" would still pass.
    expect(triggerButton()).toBeNull()
    expect(screen.queryAllByRole('button', { name: /Trigger/i })).toHaveLength(0)
  })

  it('offers it NOWHERE for a paused loop, because the server refuses to fire one', () => {
    // Gated on `active`, not on `loop`: every terminal bound leaves the loop
    // inactive, so a button here could only ever produce a 409.
    renderWith(makeLoop({ active: false }))
    expect(triggerButton()).toBeNull()
    expect(screen.queryAllByRole('button', { name: /Trigger/i })).toHaveLength(0)
  })

  it('disables itself once a cycle is due, so a press visibly acknowledges itself', () => {
    // The press used to leave the button re-enabled and unchanged, so a reader
    // could not tell whether pressing again would double the nudge. It would not:
    // the cycle is already armed. Both directions asserted -- a loop that is NOT
    // due must stay pressable, or this would disable the feature it guards.
    renderWith(makeLoop({ next_due_ts: 1_700_000_000 }))
    expect(triggerButton()).toBeTruthy()
    expect((triggerButton() as HTMLButtonElement).disabled).toBe(true)
    cleanup()
    renderWith(makeLoop({ next_due_ts: Math.floor(Date.now() / 1000) + 300 }))
    expect((triggerButton() as HTMLButtonElement).disabled).toBe(false)
  })

  it('names the way OUT of a paused loop instead of leaving Save to do it silently', () => {
    // The primary button PATCHes `active: true`, so on a paused loop it is the
    // resume control -- and it used to read "Save", which said nothing. A blind
    // reader found no resume path at all and called "Stop loop" risky as a
    // result. Both directions asserted: an active loop must still read Save, or
    // this would just move the confusion.
    renderWith(makeLoop({ active: false }))
    expect(screen.getByRole('button', { name: 'Start loop' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Start loop' })).toBeNull()
  })

  it('says the loop is stopped where the button would be, so the absence has a reason', () => {
    // Absence alone is ambiguous: an inactive loop looked identical to an active
    // one whose button failed to render, and a usability reader could not tell
    // the stopped screenshot was even the same loop. The state is the reason for
    // the absence, so it occupies the space the absence leaves.
    renderWith(makeLoop({ active: false }))
    expect(screen.getByTestId('auto-nudge-loop-paused')).toBeTruthy()
    // And it is genuinely conditional, not always-on decoration.
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.queryByTestId('auto-nudge-loop-paused')).toBeNull()
    expect(triggerButton()).toBeTruthy()
  })

  it('posts to the loop-scoped fire route with NO body, so the ARMED message is what fires', async () => {
    const fired = makeLoop({ next_due_ts: 1_700_000_000 })
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).endsWith('/fire') ? { ok: true, loop: fired } : { loop: null }),
      }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())

    await act(async () => { fireEvent.click(triggerButton()!) })

    // Selected by URL, not by index: opening the popover also reads /api/crons.
    const calls = (fetch as unknown as { mock: { calls: [string, { method?: string, body?: string }?][] } }).mock.calls
    const fire = calls.find(c => String(c[0]) === '/api/autonudge/l1/fire')
    expect(fire, 'no POST to the fire route was issued').toBeTruthy()
    expect(fire![1]?.method).toBe('POST')
    // Load-bearing: a body would let a stale popover field become the prompt.
    // The nudge fired must be whatever the loop currently holds, read server-side.
    expect(fire![1]?.body).toBeUndefined()
    // The server no longer moves the deadline, so the component supplies the
    // armed one. Asserted field-wise rather than by identity: the loop's own
    // data must be passed through untouched, and only `next_due_ts` replaced.
    const passed = onChange.mock.calls.at(-1)?.[0]
    expect(passed).toMatchObject({ ...fired, next_due_ts: expect.any(Number) })
    expect(passed.next_due_ts).toBeGreaterThan(Date.now() / 1000 - 5)
    // And it must NOT close: closing would drop an unsaved edit in the textarea
    // 40px above, with no dirty guard, so a press after an edit would cost the
    // user their text on top of spending a turn on the old prompt.
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('keeps a typed-but-unsaved goal edit after a successful press', async () => {
    // The complement of the assertion above, stated as the user-visible fact
    // rather than as a callback that was not invoked: a press must never be a
    // silent way to lose work.
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(String(url).endsWith('/fire') ? { ok: true, loop: makeLoop() } : { loop: null }),
      }),
    ) as unknown as typeof fetch)
    renderWith(makeLoop())
    const box = screen.getByLabelText('Goal description') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'edited but not saved' } })

    await act(async () => { fireEvent.click(triggerButton()!) })

    expect((screen.getByLabelText('Goal description') as HTMLTextAreaElement).value)
      .toBe('edited but not saved')
  })

  it('surfaces a refusal inline and keeps the popover open, because it holds unsaved fields', async () => {
    // The refusal names the outcome and the next step, not just the condition:
    // a reader must be able to tell a refusal from a delay, and the press was
    // refused rather than queued.
    const REFUSAL = 'nudge not sent: the agent is still working, so try again when it finishes'
    vi.stubGlobal('fetch', vi.fn((url: string) =>
      String(url).endsWith('/fire')
        ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: REFUSAL, code: 'session_busy' }) })
        : Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) }),
    ) as unknown as typeof fetch)
    const { onChange, onOpenChange } = renderWith(makeLoop())

    await act(async () => { fireEvent.click(triggerButton()!) })

    expect(screen.getByText(REFUSAL)).toBeTruthy()
    // A refusal must not report success by tearing the popover down.
    expect(onChange).not.toHaveBeenCalled()
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('names the object it clears once the loop is already stopped, and says the erase is final', async () => {
    // One button, two actions. On a live loop the press stops the loop and keeps
    // the record. On a stopped one there is nothing left to stop: the press
    // removes it, which is the only way the slot can watch something else -- a
    // stopped structured monitor blocks a re-arm until its row is gone.
    // "Clear record" failed a blind read (the popover shows nothing called a
    // "record"), so the label names the GOAL, the status reads Stopped rather
    // than the resumable-sounding Paused, and a help line names both exits
    // because the erase has no undo. Both directions asserted so this cannot
    // just move the confusion.
    renderWith(makeLoop({ active: false }))
    const clear = screen.getByRole('button', { name: 'Clear stopped goal' })
    expect(clear).toBeTruthy()
    // Danger-coloured unconditionally, not on :hover -- a touch viewport never
    // produces hover, so a hover-only colour renders an irreversible erase
    // identically to the buttons beside it.
    expect(clear.className).toContain('text-danger')
    expect(screen.queryByRole('button', { name: 'Stop loop' })).toBeNull()
    expect(screen.getByTestId('auto-nudge-loop-paused').textContent).toBe('Stopped')
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Start loop resumes this goal. Clear stopped goal removes it for good.')
    cleanup()
    renderWith(makeLoop({ active: true }))
    expect(screen.getByRole('button', { name: 'Stop loop' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
    expect(screen.queryByTestId('auto-nudge-stopped-help')).toBeNull()
  })

  it('asks before erasing a stopped goal, and each label restates the action', async () => {
    // Same two-step the monitor surface uses for its identical erase. The
    // confirm row renders no question, so a bare "Yes" would name nothing:
    // both labels have to restate what happens.
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') calls.push(String(url))
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
    }) as unknown as typeof fetch)

    renderWith(makeLoop({ active: false }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    expect(calls).toEqual([])
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    // Two controls, not three: the confirmation replaces the primary CTA rather
    // than sitting beside it (website/AUTOSDE.yaml:230 caps a row at two). Its
    // back-out reads "Cancel", the same word the monitor surface's confirm uses
    // for the same act.
    const row = screen.getByRole('button', { name: 'Cancel' }).parentElement!
    expect(Array.from(row.querySelectorAll('button')).map(b => b.textContent))
      .toEqual(['Cancel', 'Clear goal for good'])
    // And the help line becomes the question, instead of naming two buttons that
    // just left the row.
    expect(screen.getByTestId('auto-nudge-stopped-help').textContent)
      .toBe('Remove this goal for good?')
    // Cancelling erases nothing and restores the original control.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })) })
    expect(calls).toEqual([])
    expect(screen.getByRole('button', { name: 'Clear stopped goal' })).toBeTruthy()
    // Second press through the confirm performs it.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })
    expect(calls).toEqual(['/api/autonudge/l1?intent=clear'])
  })

  it('drops a primed confirmation when the record changes under the popover', async () => {
    // The popover re-renders from websocket state without closing, so another
    // tab can swap the record while a confirmation is primed: edit and restart
    // the same loop id, then a cycle cap stops it again. The press would then
    // erase a goal the confirmation never described, and the server sees no
    // mismatch because the record is inactive both times. Each of the three
    // changes that can arrive this way is asserted.
    // A harness that can swap the loop WITHOUT closing the popover, which is
    // what a websocket-driven re-render does.
    const Swappable = ({ next }: { next: Partial<AutoNudgeLoop> }) => {
      const [loop, setLoop] = useState<AutoNudgeLoop>(makeLoop({ active: false }))
      return (
        <>
          <button onClick={() => setLoop(current => ({ ...current, ...next }))}>swap</button>
          <AutoNudgePopover
            slotKey={SLOT}
            loop={loop}
            open={true}
            onOpenChange={() => {}}
            onChange={() => {}}
          />
        </>
      )
    }

    for (const next of [
      { id: 'l2' },
      { active: true },
      { message: 'a different goal entirely' },
    ]) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
      render(
        <QueryClientProvider client={qc}>
          <Swappable next={next} />
        </QueryClientProvider>,
      )
      fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
      expect(screen.getByRole('button', { name: 'Clear goal for good' })).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'swap' }))
      expect(screen.queryByRole('button', { name: 'Clear goal for good' })).toBeNull()
      cleanup()
    }
  })

  it('sends the pressed INTENT so a stale label cannot erase a record it did not mean to', async () => {
    // The server otherwise reads the operation off the record's state at arrival
    // time, so a "Stop loop" press against a record that went terminal in the
    // meantime would clear it. The intent travels with the request; the server
    // 409s on a mismatch.
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') calls.push(String(url))
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })
    }) as unknown as typeof fetch)

    renderWith(makeLoop({ active: true }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop loop' })) })
    cleanup()
    renderWith(makeLoop({ active: false }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })

    expect(calls).toEqual([
      '/api/autonudge/l1?intent=stop',
      '/api/autonudge/l1?intent=clear',
    ])
  })

  it('sits on the schedule line, not in the Stop/Save action row (max-two-buttons-per-row)', async () => {
    // `website/AUTOSDE.yaml:230` holds a row to two controls and names this
    // escape itself: the third action "leaves the row". Asserted structurally
    // rather than by counting the whole popover, because the rule is about
    // SIBLINGS IN ONE horizontal group.
    renderWith(makeLoop())
    const save = screen.getByRole('button', { name: 'Save' })
    const row = save.parentElement!
    const rowButtons = Array.from(row.querySelectorAll('button'))
    expect(rowButtons).toHaveLength(2)
    expect(rowButtons.map(b => b.textContent)).toEqual(['Stop loop', 'Save'])
    // And the trigger is a sibling of the schedule text instead.
    const trigger = triggerButton()!
    expect(trigger.parentElement).not.toBe(row)
    expect(trigger.parentElement!.textContent).toMatch(/Last fire:/)
  })
})
