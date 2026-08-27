/**
 * The switchSlot thunk boundary carries the numeric HTTP status (#6199).
 *
 * The classifier tests in `../test/agentSessionResumeMissingSlot.test.ts` feed
 * `isMissingSlotError` its payload directly, and the flow tests there fake
 * `unwrap()` — so neither would notice if `switchSlot` itself stopped producing
 * the payload. This file pins the WIRING: the real thunk, dispatched against a
 * real store, must reject with `{ status, message }` (via `rejectWithValue`,
 * which `unwrap()` throws verbatim) whenever the slot-detail fetch failed with
 * a status attached, and must keep the ordinary serialized-error shape when no
 * status exists, so the prose fallback still has something to read.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({ api: { chatSlotDetail: vi.fn() } }))

import chatReducer, { switchSlot } from './chatSlice'
import { api } from '../api/client'
import { isMissingSlotError } from '../utils/thunkError'

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    // serializableCheck stays ON deliberately: "the payload can safely enter
    // the store" is part of the contract this file pins, and the check is what
    // would flag a future payload smuggling a Response or Error instance.
    middleware: (getDefault) => getDefault({ immutableCheck: false }),
  })
}

const detail = vi.mocked(api.chatSlotDetail)

/** What the api client throws, shaped structurally (`status` + `message`) the
 *  way `ApiError` carries them. The thunk's catch is deliberately structural
 *  rather than `instanceof ApiError` — mocking `../api/client` wholesale, as
 *  this file and its siblings do, is exactly why (see the comment in
 *  `switchSlot`) — so the real class is not needed to exercise it. */
const apiError = (status: number, message: string) => Object.assign(new Error(message), { status })

/** The value `unwrap()` throws for *key*, or null if the switch succeeded. */
async function rejection(key: string): Promise<unknown> {
  try {
    await makeStore().dispatch(switchSlot(key)).unwrap()
    return null
  } catch (e) {
    return e
  }
}

describe('switchSlot — the rejection carries the numeric status (#6199)', () => {
  beforeEach(() => vi.clearAllMocks())

  it('rejects with { status, message }, and 404 classifies as slot-gone', async () => {
    // The message deliberately carries NO prose hint ("404"/"not found"): the
    // pre-fix classifier answered false here, so this case pins the
    // false-NEGATIVE half of the bug, not just the wiring.
    detail.mockRejectedValue(apiError(404, 'slot unavailable'))
    const e = await rejection('gone')
    expect(e).toEqual({ status: 404, message: 'slot unavailable' })
    expect(isMissingSlotError(e)).toBe(true)
  })

  it('a 500 quoting "not found" is NOT a missing slot, end to end', async () => {
    // The shipped regression: before the status survived the boundary, this
    // rejection matched /not found/i and a live session was replaced.
    detail.mockRejectedValue(apiError(500, 'agent "foo" not found'))
    const e = await rejection('alive')
    expect(e).toEqual({ status: 500, message: 'agent "foo" not found' })
    expect(isMissingSlotError(e)).toBe(false)
  })

  it('a status-less failure keeps the serialized-error shape for the prose fallback', async () => {
    detail.mockRejectedValue(new TypeError('Failed to fetch'))
    const e = await rejection('k')
    // miniSerializeError: a plain object, not an Error, message preserved.
    expect(e).toMatchObject({ message: 'Failed to fetch' })
    expect(e instanceof Error).toBe(false)
    expect(isMissingSlotError(e)).toBe(false)
  })
})
