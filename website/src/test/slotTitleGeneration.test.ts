/** The per-slot title generation every menu shares: one model call per slot at
 *  a time across surfaces, and the generating flag clears however it ends. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { sseSlotTitle } from '../store/dashboardSlice'

const { generateTitleMock } = vi.hoisted(() => ({ generateTitleMock: vi.fn() }))
vi.mock('../api/client', () => ({ api: { generateTitle: (...a: unknown[]) => generateTitleMock(...a) } }))
import { generateSlotTitle, useSlotTitleGenerating } from '../hooks/slotTitleGeneration'

beforeEach(() => generateTitleMock.mockReset())

describe('generateSlotTitle', () => {
  it('refuses a second start for the same slot until the first settles, from any caller', async () => {
    let resolve!: (v: { title: string }) => void
    generateTitleMock.mockReturnValueOnce(new Promise(r => { resolve = r })).mockResolvedValue({ title: 'B' })
    const rowDispatch = vi.fn(), phoneDispatch = vi.fn()
    const first = generateSlotTitle('slot-a', rowDispatch)
    expect(first).not.toBeNull()
    expect(generateSlotTitle('slot-a', phoneDispatch)).toBeNull()
    // A different slot is independent.
    await generateSlotTitle('slot-b', phoneDispatch)
    expect(phoneDispatch).toHaveBeenCalledWith(sseSlotTitle({ key: 'slot-b', title: 'B' }))
    resolve({ title: 'A' })
    await first
    expect(rowDispatch).toHaveBeenCalledWith(sseSlotTitle({ key: 'slot-a', title: 'A' }))
    expect(generateSlotTitle('slot-a', rowDispatch)).not.toBeNull()
  })

  it('reports the slot as generating while it runs and clears it on failure', async () => {
    let reject!: (e: Error) => void
    generateTitleMock.mockReturnValueOnce(new Promise((_r, j) => { reject = j }))
    const { result } = renderHook(() => useSlotTitleGenerating('slot-c'))
    expect(result.current).toBe(false)
    const settled = generateSlotTitle('slot-c', vi.fn())!.catch(e => e)
    await waitFor(() => expect(result.current).toBe(true))
    reject(new Error('offline'))
    expect(String(await settled)).toBe('Error: offline')
    await waitFor(() => expect(result.current).toBe(false))
  })
})
