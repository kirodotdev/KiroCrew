import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalModePicker from '../components/ApprovalModePicker'
import { ApiError } from '../api/apiError'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'
import {
  consumeChatHandoff,
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      chatMode: vi.fn(),
    },
  }
})

function mount() {
  const store = createTestStore({
    dashboard: { status: { disabled_approval_modes: [] } } as never,
  })
  return renderWithProviders(
    <ApprovalModePicker mode="normal" slotKey="dashboard:1" />,
    { store },
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.chatMode).mockRejectedValue(
    new ApiError(403, 'denied', JSON.stringify({ code: 'mode_disabled_by_policy' })),
  )
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('ApprovalModePicker policy-refusal hand-off', () => {
  it.each([
    ['Enter', '{Enter}'],
    ['Space', ' '],
  ])('keeps the mode action and stages the refusal with %s', async (_label, key) => {
    const user = userEvent.setup()
    mount()

    await user.click(screen.getByLabelText('Approval mode: Normal'))
    const reads = screen.getByRole('menuitem', { name: /Reads/i })
    reads.focus()
    await user.keyboard('{Enter}')
    const alert = await screen.findByRole('alert')
    expect(api.chatMode).toHaveBeenCalledTimes(1)

    reads.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(api.chatMode).toHaveBeenCalledTimes(2))
    await screen.findByRole('alert')

    const yolo = screen.getByRole('menuitem', { name: /YOLO/i })
    yolo.focus()
    await user.keyboard('{ArrowDown}')
    const handoff = screen.getByRole('menuitem', { name: /^Ask the agent$/i })
    expect(handoff).toHaveFocus()
    expect(handoff).toHaveAttribute('aria-describedby', alert.id)
    await user.keyboard(key)

    expect(consumeChatHandoff()).toContain('Disabled by your organization')
    expect(api.chatMode).toHaveBeenCalledTimes(2)
  })
})
