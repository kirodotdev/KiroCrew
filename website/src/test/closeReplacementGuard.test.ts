import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { closeHitAReplacement, isCloseOutcomeUnknown } from '../utils/closeOutcome'

const read = (rel: string): string => readFileSync(resolve(__dirname, rel), 'utf-8')

describe('a close names the incarnation at every site, so a replacement survives', () => {
  it('leaves no close that deletes by key alone', () => {
    const sources = [read('../pages/ArtifactDetailPage.tsx'), read('../store/chatSlice.ts')]

    for (const src of sources) {
      expect(src).not.toMatch(/deleteChatSlot\((?:slot\.key|key)\)/)
    }
  })

  it('sends the artifact close the incarnation of the row it archives', () => {
    const page = read('../pages/ArtifactDetailPage.tsx')

    expect(page).toMatch(/api\.deleteChatSlot\(slot\.key, slot\.incarnation\)/)
  })
})

describe('a replacement mismatch is never presented as safe to retry', () => {
  it('classifies it as unknown, whose copy tells the reader not to close again', () => {
    const refusedShape = { status: 409, code: 'target_replaced', definitive: true }
    const noStatus = { code: 'target_replaced' }

    expect(closeHitAReplacement(refusedShape)).toBe(true)
    // `definitive: true` would otherwise route to 'refused', whose copy says to try again
    expect(isCloseOutcomeUnknown(refusedShape)).toBe(true)
    expect(isCloseOutcomeUnknown(noStatus)).toBe(true)
  })

  it('reads the code out of a raw response body too', () => {
    const raw = { status: 409, body: JSON.stringify({ code: 'target_replaced' }) }

    expect(isCloseOutcomeUnknown(raw)).toBe(true)
  })

  it('still treats a genuinely unknown outcome as unknown', () => {
    expect(isCloseOutcomeUnknown({ status: 500 })).toBe(true)
    expect(isCloseOutcomeUnknown({ status: 408 })).toBe(true)
    expect(isCloseOutcomeUnknown({})).toBe(true)
  })
})

describe('the replacement code reaches the notice through the rejection', () => {
  it('recognises the shape the server actually sends, with the code only in the body', () => {
    // ApiError has no `.code` field at all: status, body and authRequired only. A
    // structural read yields undefined, which is how the notice lost the replacement.
    const asApiErrorWouldArrive = {
      status: 409,
      body: JSON.stringify({
        error: 'the target session was replaced during the close',
        code: 'target_replaced',
        definitive: true,
      }),
    }

    expect(closeHitAReplacement(asApiErrorWouldArrive)).toBe(true)
    expect(isCloseOutcomeUnknown(asApiErrorWouldArrive)).toBe(true)
  })

  it('builds the rejection from that body-aware read, not a structural field', () => {
    const slice = read('../store/chatSlice.ts')

    expect(slice).toMatch(/const replaced = closeHitAReplacement\(e\)/)
    expect(slice).toMatch(/replaced \? \{ code: 'target_replaced' as const \} : \{\}/)
    expect(slice).not.toMatch(/const code = \(e as \{ code\?: unknown \} \| null\)\?\.code/)
  })
})

describe('the refused branch never aims a second close at the key', () => {
  beforeEach(() => vi.resetModules())

  it('settles and reconciles instead of deleting again', async () => {
    const { withSlotClose } = await import('../store/chatSlice')
    const deleteAgain = vi.fn()
    const dispatched: unknown[] = []
    const dispatch = ((a: unknown) => {
      dispatched.push(a)
      return { unwrap: async () => undefined }
    }) as never
    const getState = (() => ({
      dashboard: { slots: [], closingSlots: {}, closeSeq: 0, lastSlotsGeneration: 0 },
      chat: {},
    })) as never

    await expect(
      withSlotClose(dispatch, getState, 'chat-1', async () => {
        deleteAgain()
        throw { status: 409, code: 'target_replaced', definitive: true }
      }),
    ).rejects.toBeTruthy()

    expect(deleteAgain).toHaveBeenCalledTimes(1)
  })
})
