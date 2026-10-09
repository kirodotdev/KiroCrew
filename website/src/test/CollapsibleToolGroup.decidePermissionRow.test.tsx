/**
 * The collapsed tool group wired to the decide ChatPage gives it
 * (lib/decidePermissionRow), on a press the server refuses as terminal.
 *
 * The group marks the pressed decision before the request returns and only
 * clears that mark in its failure path. So the decide must reject on a
 * terminal refusal: if it returned normally, the group would keep the pressed
 * decision and show the "resolved" dot, and nothing would say the request is
 * no longer pending.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'
import { i18nT } from '../i18n/t'

const decideApproval = vi.fn((..._args: unknown[]) => Promise.resolve({}))
vi.mock('../api/client', async (orig) => ({
  ...(await orig<typeof import('../api/client')>()),
  api: { decideApproval: (...args: unknown[]) => decideApproval(...args) },
}))

import { ApiError } from '../api/client'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import { decidePermissionRow } from '../lib/decidePermissionRow'

const T = (k: string) => i18nT(`pages.chat.collapsibleToolGroup.${k}`)
const SLOT = 'chat-1'

describe('collapsed tool group with the permission-row decide', () => {
  it.each([
    [404, 'not found'],
    [400, 'no pending approval'],
  ])('a terminal %i refusal clears the pressed decision and the group says it is no longer pending', async (status, message) => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    decideApproval.mockImplementationOnce(() => Promise.reject(new ApiError(status, message)))
    const row: ChatMessage = {
      role: 'permission', content: '[subagent] shell', ts: '1',
      meta: { approval_id: 'same-id', registry: 'coordinator', approval_instance: 'inst-a', tool_input: 'ls' },
    }
    const store = createTestStore({
      chat: { activeSlot: SLOT, messages: [row], toolLog: [] } as unknown as RootState['chat'],
    })
    renderWithProviders(
      <CollapsibleToolGroup count={1} hasPermission permissionMeta={row.meta} pendingPermCount={1}
        onApprove={(action: string) => store.dispatch(decidePermissionRow(row.meta, SLOT, action))}>
        <div>zzq-child</div>
      </CollapsibleToolGroup>,
      { store },
    )

    fireEvent.click(screen.getByText(T('approve')))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      i18nT('components.approvalCard.approval_no_longer_pending'),
    )
    expect(screen.queryByLabelText(T('resolved'))).not.toBeInTheDocument()
    // Nothing is left for the agent to look into.
    expect(screen.queryByText(i18nT('components.askAgent.ask_the_agent') as string)).not.toBeInTheDocument()
  })
})
