// Settings ▸ Chat ▸ Model — optimistic picker display consistency (#6875).
//
// The seven model/effort selects render `pending ?? server`: a pick must show
// in the trigger IMMEDIATELY (while the config PATCH is still in flight), roll
// back beside the existing failed-to-save banner when the PATCH rejects, and
// reconcile to whatever the refetched config reports once the mutation
// settles — the server stays authoritative, even when it answers with a
// different value than the one picked.
//
// The pending display is per config PATH with monotonic ownership: concurrent
// picks on two pickers never revert each other, an older settle never clears
// a newer pick on the same picker (A→B→A), a persisted-but-unadvertised id
// stays selectable mid-flight, and the shared failure banner is only
// auto-cleared by a retry on the SAME path.
//
// Every display test here pins the pre-settle state with the PATCH promise
// held open — the window where the query cache still holds the pre-pick
// config, so only the pending overlay can produce the asserted trigger text.
//
// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks —
// use the same lightweight mock the sibling ChatPanel suites use so options
// are real role="option" nodes.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, modelsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } })
  ),
  modelsMock: vi.fn(() =>
    Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'claude-opus-4.8', description: 'Opus' },
      { model_name: 'claude-haiku-4.5', description: 'Haiku' },
    ])
  ),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => Promise.resolve({ restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' }),
    kirocrewConfig: kirocrewConfigMock,
    models: modelsMock,
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

/** The agent block the config mock serves. Reassigned mid-test to stand in for
 *  the server's post-PATCH state: the refetch that follows a successful PATCH
 *  reads whatever this holds at that moment. */
let serverAgent: Record<string, unknown> = {}
const seed = (agent: Record<string, unknown>) => {
  serverAgent = agent
  kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent: serverAgent }) as never)
}

/** A PATCH whose settle the test controls. While `resolve`/`reject` has not
 *  been called the request is in flight — the window the optimistic display
 *  must already be visible in. */
function deferPatch() {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  patchConfigMock.mockImplementation(() => promise as never)
  return { resolve, reject }
}

/** N independently controlled PATCHes, handed out call-by-call in order. */
function deferPatches(n: number) {
  const ds = Array.from({ length: n }, () => {
    let resolve!: (v: unknown) => void
    let reject!: (e: unknown) => void
    const promise = new Promise((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  })
  let i = 0
  patchConfigMock.mockImplementation(() => ds[Math.min(i++, n - 1)].promise as never)
  return ds
}

/** Open a SettingsSelect by label and return its option nodes.
 *  Waits for the control to leave its loading-disabled state first — the
 *  trigger exists (inert) while the config query is still in flight. */
async function openSelect(label: string) {
  const trigger = await screen.findByRole('combobox', { name: label })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  return trigger
}

async function pick(label: string, optionName: string) {
  const trigger = await openSelect(label)
  fireEvent.click(screen.getByRole('option', { name: optionName }))
  return trigger
}

beforeEach(() => {
  patchConfigMock.mockReset()
  patchConfigMock.mockImplementation(() => Promise.resolve({}) as never)
  kirocrewConfigMock.mockClear()
  modelsMock.mockClear()
  seed({ model: 'auto', reasoning_effort: '' })
})

describe('ChatPanel — optimistic default model', () => {
  it('shows the picked model before the PATCH resolves', async () => {
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')

    // Pre-settle: the trigger already shows the pick while the PATCH promise
    // is still open, and the config has NOT been refetched — the only way to
    // display it this early is the optimistic pending value.
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
    expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'claude-opus-4.8')
    expect(kirocrewConfigMock).toHaveBeenCalledTimes(1)

    // Settle: the refetch reports the same value; the display keeps it.
    serverAgent = { model: 'claude-opus-4.8', reasoning_effort: '' }
    resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    expect(trigger).toHaveTextContent('claude-opus-4.8')
  })

  it('reconciles to the server value when the refetch reports a different one', async () => {
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))

    // The backend normalises the pick to something else; once the mutation
    // settles the picker must show the SERVER's answer, not the local one.
    serverAgent = { model: 'claude-haiku-4.5', reasoning_effort: '' }
    resolve({})
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))
  })

  it('rolls back to the persisted model when the PATCH rejects', async () => {
    const { reject } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))

    reject(new Error('boom'))
    expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveTextContent('Default (auto)'))
  })

  it('enables the effort row against the optimistic model, not the stale one', async () => {
    // Effort gating is derived from the SHOWN model, so picking a
    // reasoning-capable model unlocks the effort row while the PATCH is
    // still in flight — the panel stays self-consistent with its triggers.
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const effortTrigger = await screen.findByRole('combobox', { name: 'Default Reasoning Effort' })
    await waitFor(() => expect(effortTrigger).toHaveAttribute('data-disabled'))

    await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(effortTrigger).not.toHaveAttribute('data-disabled'))
    resolve({})
  })
})

describe('ChatPanel — optimistic role effort (the empty-string value)', () => {
  it('shows "Model default" ("") before the PATCH resolves and keeps it after settle', async () => {
    // '' is a MEANINGFUL value (inherit the model's own default): a truthiness
    // test would drop the pending pick and keep showing 'High'.
    seed({ role_models: { background: 'claude-opus-4.8' }, role_efforts: { background: 'high' } })
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Background Effort', 'Model default')

    await waitFor(() => expect(trigger).toHaveTextContent('Model default'))
    expect(patchConfigMock).toHaveBeenCalledWith('agent.role_efforts.background', '')
    expect(kirocrewConfigMock).toHaveBeenCalledTimes(1)

    serverAgent = { role_models: { background: 'claude-opus-4.8' }, role_efforts: { background: '' } }
    resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    expect(trigger).toHaveTextContent('Model default')
  })

  it('rolls back to the persisted effort when the PATCH rejects', async () => {
    seed({ role_models: { background: 'claude-opus-4.8' }, role_efforts: { background: '' } })
    const { reject } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Background Effort', 'Max')
    await waitFor(() => expect(trigger).toHaveTextContent('Max'))

    reject(new Error('boom'))
    expect(await screen.findByText(/Failed to save role effort/)).toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveTextContent('Model default'))
  })
})

describe('ChatPanel — optimistic fallback model', () => {
  it('shows the picked model before the PATCH resolves and keeps the refetched value', async () => {
    seed({ fallback_model: 'auto' })
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Fallback model', 'claude-opus-4.8')

    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
    expect(patchConfigMock).toHaveBeenCalledWith('agent.fallback_model', 'claude-opus-4.8')
    expect(kirocrewConfigMock).toHaveBeenCalledTimes(1)

    serverAgent = { fallback_model: 'claude-opus-4.8' }
    resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    expect(trigger).toHaveTextContent('claude-opus-4.8')
  })

  it('shows "Disabled" ("") optimistically and rolls it back when the PATCH rejects', async () => {
    // The fallback picker's other meaningful '' value: Disabled. It must
    // render optimistically (?? semantics again) and snap back to the
    // persisted choice beside the existing error copy on failure.
    seed({ fallback_model: 'auto' })
    const { reject } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Fallback model', 'Disabled')
    await waitFor(() => expect(trigger).toHaveTextContent('Disabled'))
    expect(patchConfigMock).toHaveBeenCalledWith('agent.fallback_model', '')

    reject(new Error('boom'))
    expect(await screen.findByText(/Failed to save fallback model/)).toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveTextContent('Auto (recommended)'))
  })
})

describe('ChatPanel — pending ownership and reconciliation edges', () => {
  it('concurrent picks on two pickers never revert each other (error + refetch paths)', async () => {
    // The headline race: with a whole-config snapshot, Y's failure restores a
    // state captured before X's pick (erasing X's display), and X's settle
    // refetch returns server state that lacks Y's un-applied PATCH (reverting
    // Y's display). Per-path pending entries make both directions impossible.
    seed({ model: 'auto', reasoning_effort: '', fallback_model: 'auto' })
    const ds = deferPatches(2)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const fallbackTrigger = await pick('Fallback model', 'claude-haiku-4.5')
    const modelTrigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(fallbackTrigger).toHaveTextContent('claude-haiku-4.5'))
    await waitFor(() => expect(modelTrigger).toHaveTextContent('claude-opus-4.8'))

    // Y (fallback) FAILS while X (default model) is still in flight: only the
    // fallback display may roll back — the model pick must survive.
    ds[0].reject(new Error('boom'))
    expect(await screen.findByText(/Failed to save fallback model/)).toBeInTheDocument()
    await waitFor(() => expect(fallbackTrigger).toHaveTextContent('Auto (recommended)'))
    expect(modelTrigger).toHaveTextContent('claude-opus-4.8')

    // X SUCCEEDS: its settle-time refetch reports the model as persisted. The
    // display keeps it, and nothing else on the panel moved.
    serverAgent = { model: 'claude-opus-4.8', reasoning_effort: '', fallback_model: 'auto' }
    ds[1].resolve({})
    await waitFor(() => expect(kirocrewConfigMock.mock.calls.length).toBeGreaterThan(1))
    expect(modelTrigger).toHaveTextContent('claude-opus-4.8')
    expect(fallbackTrigger).toHaveTextContent('Auto (recommended)')
  })

  it('keeps the latest pick displayed when an older same-value PATCH settles first (A→B→A)', async () => {
    // Ownership must be a token, not the picked value: after pick A → pick B
    // → pick A, the FIRST A's settle must not clear the pending entry the
    // THIRD pick owns. The server still reports the stale value here, so a
    // wrongful clear is visible as the trigger falling back to it.
    const ds = deferPatches(3)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await pick('Default Model', 'claude-haiku-4.5')
    await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
    expect(patchConfigMock).toHaveBeenCalledTimes(3)

    // First A settles (serverAgent still 'auto'); B and the second A are in
    // flight, so the trigger must keep showing the latest pick.
    ds[0].resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    expect(trigger).toHaveTextContent('claude-opus-4.8')

    // The remaining mutations settle. The server reports a normalised value,
    // so the trigger moving to it proves BOTH later mutations settled, the
    // pending entry cleared, and the server stayed authoritative.
    serverAgent = { model: 'claude-haiku-4.5', reasoning_effort: '' }
    ds[1].resolve({})
    ds[2].resolve({})
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))
  })

  it('clears a stale failure banner when a new pick starts', async () => {
    // A failed save leaves the banner up; the next attempt must not show a
    // fresh optimistic value under last attempt's error in the same frame.
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()

    serverAgent = { model: 'claude-haiku-4.5', reasoning_effort: '' }
    await pick('Default Model', 'claude-haiku-4.5')
    await waitFor(() =>
      expect(screen.queryByText(/Failed to save default model/)).not.toBeInTheDocument()
    )
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))
  })

  it('does NOT clear a failure banner that came from a non-picker save', async () => {
    // The banner is one shared slot for ~12 failure sources on this panel. A
    // picker pick may only auto-clear a PICKER failure: dismissing, say, an
    // unresolved auto-compact error would leave the user believing that
    // setting persisted.
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    // Fail a non-picker save (Auto-Compact Threshold PATCHes via .then/.catch,
    // not a picker mutation) to raise its banner.
    await pick('Auto-Compact Threshold', '90%')
    expect(await screen.findByText(/Failed to save auto-compact threshold/)).toBeInTheDocument()

    // A model pick must leave that unresolved failure visible. The PATCH
    // resolves instantly here, so seed the refetched config with the pick —
    // asserting the pre-settle overlay against an already-settled mutation
    // would race the microtask cascade.
    serverAgent = { model: 'claude-opus-4.8', reasoning_effort: '' }
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
    expect(screen.getByText(/Failed to save auto-compact threshold/)).toBeInTheDocument()
  })

  it("does NOT clear ANOTHER picker's failure banner", async () => {
    // Banner ownership is per config path, not per picker-kind: a fallback
    // save failure must survive a subsequent Default Model pick — that pick
    // says nothing about whether the fallback persisted.
    seed({ model: 'auto', reasoning_effort: '', fallback_model: 'auto' })
    patchConfigMock.mockImplementationOnce(() => Promise.reject(new Error('boom')) as never)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    await pick('Fallback model', 'claude-haiku-4.5')
    expect(await screen.findByText(/Failed to save fallback model/)).toBeInTheDocument()

    // Instant-resolve PATCH: seed the refetched config with the pick so the
    // assertion holds past settle (same reasoning as the test above).
    serverAgent = { model: 'claude-opus-4.8', reasoning_effort: '', fallback_model: 'auto' }
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
    expect(screen.getByText(/Failed to save fallback model/)).toBeInTheDocument()

    // Re-picking the FALLBACK itself does clear its own stale failure.
    await pick('Fallback model', 'claude-opus-4.8')
    await waitFor(() =>
      expect(screen.queryByText(/Failed to save fallback model/)).not.toBeInTheDocument()
    )
  })

  it('suppresses the failure banner of a pick superseded by a newer one on the same path', async () => {
    // Pick A, then pick B before A settles; A then FAILS. B owns the display
    // now — A's late failure must not install a stale "failed to save" beside
    // the value B is about to persist.
    const ds = deferPatches(2)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await pick('Default Model', 'claude-haiku-4.5')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))

    ds[0].reject(new Error('boom'))
    serverAgent = { model: 'claude-haiku-4.5', reasoning_effort: '' }
    ds[1].resolve({})
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))
    expect(screen.queryByText(/Failed to save default model/)).not.toBeInTheDocument()
  })

  it('keeps the accepted value displayed when the settle-time refetch fails', async () => {
    // invalidateQueries swallows refetch rejections, so onSettled clears the
    // overlay either way. The success-path cache write of the ACCEPTED value
    // is what keeps the display truthful when the refetch transiently fails.
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))

    kirocrewConfigMock.mockImplementationOnce(() => Promise.reject(new Error('offline')) as never)
    resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    // Overlay cleared, refetch failed — the cache write keeps the pick shown.
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
  })

  it("a superseded pick's own settle never writes its value over a newer settled pick", async () => {
    // A→B on one picker, B settles FIRST: A's later success must not write
    // its stale value into the cache. A failed refetch after A's settle
    // makes a wrongful write visible — the display would fall back to A
    // while the server holds B.
    const ds = deferPatches(2)
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await pick('Default Model', 'claude-haiku-4.5')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))

    // B (the newer pick) settles first; the server reports B.
    serverAgent = { model: 'claude-haiku-4.5', reasoning_effort: '' }
    ds[1].resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))

    // A settles late and its refetch fails: only a wrongful cache write
    // could change the display now.
    kirocrewConfigMock.mockImplementationOnce(() => Promise.reject(new Error('offline')) as never)
    ds[0].resolve({})
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(trigger).toHaveTextContent('claude-haiku-4.5'))
  })

  it('refetches after a failed PATCH so a server-side apply is not rolled back blind', async () => {
    // A PATCH can fail after persisting (5xx after apply). The error path
    // must still refetch: only the server can say which value survived.
    const { reject } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))

    // The server actually applied the write before answering 5xx.
    serverAgent = { model: 'claude-opus-4.8', reasoning_effort: '' }
    reject(new Error('boom'))
    expect(await screen.findByText(/Failed to save default model/)).toBeInTheDocument()
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))
  })

  it('keeps the server\'s unadvertised model listed while a pick is in flight', async () => {
    // The stored id may have dropped off the advertised list; picking a new
    // model must not remove it from the options during the in-flight window,
    // or the user could not change back to it.
    seed({ model: 'claude-opus-4.7-retired', reasoning_effort: '' })
    const { resolve } = deferPatch()
    wrap(<ChatPanel />)
    await waitFor(() => expect(modelsMock).toHaveBeenCalled())
    const trigger = await pick('Default Model', 'claude-opus-4.8')
    await waitFor(() => expect(trigger).toHaveTextContent('claude-opus-4.8'))

    fireEvent.click(trigger)
    const labels = screen.getAllByRole('option').map(o => o.textContent)
    expect(labels).toContain('claude-opus-4.7-retired')
    expect(labels).toContain('claude-opus-4.8')
    resolve({})
  })
})
