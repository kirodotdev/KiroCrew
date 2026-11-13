/**
 * useActionsProvider — the React wiring around the pure provider factory
 * (which actionsProvider.test.ts covers): a failed "New session" create is
 * reported on the shell notice, since the palette has closed by then.
 */
import { describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'

vi.mock('../../../store/chatSlice', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../store/chatSlice')>()),
  createSlot: () => () => {
    const p = Promise.reject(new Error('zzq create failed'))
    return Object.assign(p.catch(() => undefined), { unwrap: () => p, requestId: 'zzq-create' })
  },
}))

import { renderHookWithProviders } from '../../../test/helpers'
import { useActionsProvider } from './actionsProvider'

describe('useActionsProvider — New session', () => {
  it('reports a failed create on the shell notice', async () => {
    const { result, store } = renderHookWithProviders(() => useActionsProvider({ openShortcuts: vi.fn() }))
    const rows = await Promise.resolve(result.current.search(''))
    const newSession = rows.find((r) => r.id === 'actions:new-session')
    expect(newSession?.onActivate).toBeDefined()
    newSession!.onActivate!()

    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toEqual({ title: "Couldn't create a new chat.", message: 'zzq create failed', error: true }))
  })
})
