import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from '../../test/helpers'
import { selectSlotMessages } from '../../store/chatSlice'

const sendTurn = vi.fn()
vi.mock('../../chat-core/transport/sendTurn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../chat-core/transport/sendTurn')>()),
  sendTurn: (opts: unknown) => sendTurn(opts),
}))

const { sendChangeRequest } = await import('./sendChangeRequest')

const SLOT = 'member-atlas'

function userRows(store: ReturnType<typeof createTestStore>) {
  return selectSlotMessages(store.getState(), SLOT).filter(m => m.role === 'user')
}

afterEach(() => sendTurn.mockReset())

describe('sendChangeRequest', () => {
  it('draws the request in the open chat at once and confirms it on the receipt', async () => {
    const store = createTestStore()
    let answer: (v: unknown) => void = () => {}
    sendTurn.mockImplementation(() => new Promise(resolve => { answer = resolve }))
    const out = sendChangeRequest(store.dispatch, SLOT, 'show CI state')
    // Drawn before any answer: the person sees what they sent.
    const [row] = userRows(store)
    expect(row.content).toBe('show CI state')
    expect(row.meta?.optimistic).toBe(true)
    const sendId = row.meta?.sendId
    // The POST carries the same id, so the gateway's echo reconciles this row.
    expect(sendTurn.mock.calls[0][0]).toMatchObject({ slot: SLOT, message: 'show CI state', meta: { sendId } })
    answer({ status: 'dispatched', body: {} })
    expect(await out).toBe('sent')
    expect(userRows(store)[0].meta?.optimistic).toBeUndefined()
  })

  it('takes the row back when the gateway refused the request', async () => {
    const store = createTestStore()
    sendTurn.mockResolvedValue({ status: 'refused', body: {}, reason: 'no' })
    expect(await sendChangeRequest(store.dispatch, SLOT, 'show CI state')).toBe('refused')
    expect(userRows(store)).toHaveLength(0)
  })

  it('keeps the row, marked unconfirmed, when no receipt came back', async () => {
    const store = createTestStore()
    sendTurn.mockResolvedValue({ status: 'response-late', body: {} })
    expect(await sendChangeRequest(store.dispatch, SLOT, 'show CI state')).toBe('unconfirmed')
    const [row] = userRows(store)
    expect(row.meta?.deliveryUnconfirmed).toBe(true)
  })
})
