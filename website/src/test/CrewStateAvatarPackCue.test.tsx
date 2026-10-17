/**
 * Where a crew's cue comes FROM, decided by what the crew wears.
 *
 * `CrewStateAvatar` is the component that observes the transition, so it is also
 * where the two sources are chosen between: a ghost's preset, or the worn pack's
 * own audio. The rules worth pinning are the two that are invisible in the diff —
 * a crew that wears no pack must issue no pack request at all, and the built-in
 * pack is the seeded ghost, whose cue route answers `builtin_no_content`, so it
 * must not be asked either.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

const observed: unknown[] = []
vi.mock('../hooks/useCrewAvatarState', () => ({
  useCrewAvatarState: (options: unknown) => {
    observed.push(options)
    return 'idle'
  },
  AVATAR_FLASH_MS: 4000,
}))

// The renderer is not what this file is about, and mounting it would issue the
// pack read a second time from below.
vi.mock('../components/appearancePacks/PackAvatar', () => ({
  default: () => React.createElement('span', { 'data-testid': 'pack-avatar-stub' }),
}))

import CrewStateAvatar from '../components/CrewStateAvatar'
import dashboardReducer from '../store/dashboardSlice'
import { BUILTIN_PACK_ID } from '../lib/appearancePacks/library'

let qc: QueryClient

function mount(avatar: unknown) {
  const store = configureStore({ reducer: { dashboard: dashboardReducer } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <CrewStateAvatar seed="radar" avatar={avatar} />
      </Provider>
    </QueryClientProvider>,
  )
}

/** The options the LAST render handed the state hook. */
const lastOptions = () => observed.at(-1) as Record<string, unknown>

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  detail.mockReset()
  observed.length = 0
})

describe('CrewStateAvatar — which cue the crew gets', () => {
  it('hands a ghost its presets and asks for no pack', async () => {
    mount({ kind: 'ghost', sounds: { done: 'chime' } })
    expect(lastOptions().sounds).toEqual({ done: 'chime' })
    expect(lastOptions().packCue).toBeNull()
    // The query is disabled for a crew that wears no pack, so the ghost tier
    // costs no request — a roster of ghosts must not hit the pack route at all.
    expect(detail).not.toHaveBeenCalled()
  })

  it('hands a pack-wearing crew the states its manifest declares', async () => {
    detail.mockResolvedValue({
      animations: { idle: { content: '<svg/>', format: 'svg' } },
      sounds: { done: true, error: true },
    })
    const { rerender } = mount({ kind: 'pack', id: 'aurora' })
    await vi.waitFor(() => expect(detail).toHaveBeenCalledWith('aurora'))
    await vi.waitFor(() => expect(lastOptions().packCue).not.toBeNull())
    expect(lastOptions().packCue).toEqual({ id: 'aurora', states: { done: true, error: true } })
    void rerender
  })

  it('leaves the cue null while the read is still in flight', () => {
    detail.mockReturnValue(new Promise(() => {}))
    mount({ kind: 'pack', id: 'aurora' })
    // Not a preset in the meantime: a pack's crew is silent until its own
    // manifest says otherwise, and guessing would report the wrong sound.
    expect(lastOptions().packCue).toBeNull()
  })

  it('reports the read as pending only while a pack is worn', async () => {
    // A ghost crew must NOT read as pending: the query is disabled for it and a
    // disabled query reports pending, so deriving the flag from query status
    // would hold every ghost's cue edge forever.
    mount({ kind: 'ghost', sounds: { done: 'chime' } })
    expect(lastOptions().packPending).toBe(false)

    detail.mockReturnValue(new Promise(() => {}))
    mount({ kind: 'pack', id: 'aurora' })
    expect(lastOptions().packPending).toBe(true)
  })

  it('stops reporting pending once the manifest arrives', async () => {
    detail.mockResolvedValue({
      animations: { idle: { content: '<svg/>', format: 'svg' } },
      sounds: { done: true },
    })
    mount({ kind: 'pack', id: 'aurora' })
    await vi.waitFor(() => expect(lastOptions().packCue).not.toBeNull())
    expect(lastOptions().packPending).toBe(false)
  })

  it('stops reporting pending when the manifest cannot be read at all', async () => {
    // "In flight" and "cannot be known" are different answers, and only the first
    // is worth holding a transition for. React Query has already retried and will
    // re-read on the next mount, focus or reconnect, so a hold that outlived the
    // failure would suppress this crew's cues for the rest of the session — and a
    // suppressed cue is indistinguishable from a pack that declares none.
    detail.mockRejectedValue(new Error('gateway down'))
    mount({ kind: 'pack', id: 'aurora' })
    await vi.waitFor(() => expect(lastOptions().packPending).toBe(false))
    expect(lastOptions().packCue).toBeNull()
  })

  it('leaves the cue null for a pack that declares none', async () => {
    detail.mockResolvedValue({ animations: { idle: { content: '<svg/>', format: 'svg' } } })
    mount({ kind: 'pack', id: 'aurora' })
    await vi.waitFor(() => expect(detail).toHaveBeenCalled())
    expect(lastOptions().packCue).toBeNull()
  })

  it('never asks the built-in pack for a cue', () => {
    // `kiro-ghost` IS the seeded ghost and ships in this bundle; both its slot
    // and its sound route answer `builtin_no_content` on purpose.
    mount({ kind: 'pack', id: BUILTIN_PACK_ID })
    expect(detail).not.toHaveBeenCalled()
    expect(lastOptions().packCue).toBeNull()
  })

  it('gives a picture neither a preset nor a pack cue', () => {
    // A picture is static and silent: `soundsFrom` is ghost-only, so even a
    // record carrying a legacy cue resolves to nothing here.
    mount({ kind: 'image', v: 2, sounds: { done: 'chime' } })
    expect(lastOptions().sounds).toBeNull()
    expect(lastOptions().packCue).toBeNull()
    expect(detail).not.toHaveBeenCalled()
  })
})
