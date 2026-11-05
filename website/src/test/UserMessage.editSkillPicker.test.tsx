import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../utils/shareUrl', () => ({ copySessionLink: vi.fn().mockResolvedValue(undefined) }))

import UserMessage from '../pages/chat/UserMessage'

const SKILLS = [
  { key: 'WorkforceEmploymentKnowledgeBase/oncall-handover', name: 'oncall-handover', description: 'Handover report', source: 'kirocrew' },
  { key: 'ticket-pull', name: 'ticket-pull', description: 'Pull tickets', source: 'kirocrew' },
]

const renderContent = (content: string) => <span data-testid="content">{content}</span>

function renderEditing(props: Partial<ComponentProps<typeof UserMessage>> = {}) {
  const onEditResend = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  render(
    <QueryClientProvider client={client}>
      <UserMessage
        content="original"
        renderContent={renderContent}
        canEdit
        messageIndex={0}
        messageTs="t1"
        onEditResend={onEditResend}
        {...props}
      />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByTitle('Edit & Resend'))
  const field = screen.getByRole('textbox', { name: 'Edit message' }) as HTMLTextAreaElement
  return { field, onEditResend }
}

function typeInto(field: HTMLTextAreaElement, value: string) {
  field.focus()
  field.setSelectionRange(field.value.length, field.value.length)
  fireEvent.change(field, { target: { value, selectionStart: value.length, selectionEnd: value.length } })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.skills.mockResolvedValue(SKILLS)
})

describe('the Edit & Resend field offers the $ skill menu (#29472)', () => {
  it('opens the skill menu on a typed $ and lists the matching skill', async () => {
    const { field } = renderEditing({ slotKey: 'chat-7', editSkillPicker: { slotId: 'chat-7', agentName: 'default', project: '' } })
    typeInto(field, 'hello $onc')
    expect(await screen.findByText('$oncall-handover')).toBeInTheDocument()
    // Scoped the way the composer scopes it: this chat's session key and agent.
    expect(mockApi.skills).toHaveBeenCalledWith('dashboard:chat-7', 'default', expect.anything())
  })

  it('inserts the picked token on Enter instead of resending the edit', async () => {
    const { field, onEditResend } = renderEditing({ editSkillPicker: { slotId: 'chat-7', agentName: 'default' } })
    typeInto(field, 'hello $onc')
    await screen.findByText('$oncall-handover')
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(field.value).toBe('hello $oncall-handover '))
    expect(onEditResend).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('closes the menu on Escape and keeps the editor open', async () => {
    const { field } = renderEditing({ editSkillPicker: { slotId: 'chat-7' } })
    typeInto(field, '$tick')
    await screen.findByText('$ticket-pull')
    fireEvent.keyDown(field, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(screen.getByRole('textbox', { name: 'Edit message' })).toBeInTheDocument()
  })

  it('closes the menu when focus moves to another field, so its Enter is not taken', async () => {
    const other = document.createElement('textarea')
    document.body.appendChild(other)
    const { field } = renderEditing({ editSkillPicker: { slotId: 'chat-7' } })
    typeInto(field, 'hello $onc')
    await screen.findByText('$oncall-handover')
    act(() => { other.focus() })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    fireEvent.keyDown(other, { key: 'Enter' })
    expect(field.value).toBe('hello $onc')
    other.remove()
  })

  it('closes the menu when a click on empty space takes focus from the field', async () => {
    const { field } = renderEditing({ editSkillPicker: { slotId: 'chat-7' } })
    typeInto(field, 'hello $onc')
    await screen.findByText('$oncall-handover')
    act(() => { field.blur() })
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
  })

  it('keeps the menu open when focus moves to its own Retry button', async () => {
    mockApi.skills.mockRejectedValue(new Error('offline'))
    const { field } = renderEditing({ editSkillPicker: { slotId: 'chat-7' } })
    typeInto(field, 'hello $onc')
    const retry = await screen.findByRole('button', { name: 'Retry' })
    act(() => { retry.focus() })
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    // Leaving Retry for an unrelated field closes the menu as well.
    const other = document.createElement('textarea')
    document.body.appendChild(other)
    act(() => { other.focus() })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument())
    other.remove()
  })

  it('opens no menu when the host names no chat for it', async () => {
    const { field } = renderEditing()
    typeInto(field, 'hello $onc')
    expect(field.value).toBe('hello $onc')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(mockApi.skills).not.toHaveBeenCalled()
  })
})
