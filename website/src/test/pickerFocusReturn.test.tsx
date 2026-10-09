/**
 * #18313: a PICK in a composer picker hands focus to the composer; a CANCEL
 * keeps the ARIA menu-button return to the trigger.
 *
 * Before this, both pickers left focus on their own trigger after a pick, so
 * the Enter the user meant as "send" re-opened the menu. The host decides where
 * focus goes (`onPicked` returns true when it took focus); these tests pin the
 * split between pick and cancel on both pickers, with the real Radix menu for
 * the approval picker (the hand-back rides Radix's `onCloseAutoFocus`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalModePicker from '../components/ApprovalModePicker'
import BusySendButton from '../components/BusySendButton'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'

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

/** A stand-in for the composer textarea the host hands focus to. */
function Host({ onPicked, picker }: { onPicked: () => boolean; picker: 'approval' | 'busy' }) {
  return (
    <div>
      <textarea data-testid="composer" aria-label="composer" />
      {picker === 'approval'
        ? <ApprovalModePicker mode="normal" slotKey="dashboard:1" onPicked={onPicked} />
        : <BusySendButton mode="steer" onModeChange={vi.fn()} onFire={vi.fn()} onPicked={onPicked} />}
    </div>
  )
}

function focusComposerStub(): boolean {
  screen.getByTestId('composer').focus()
  return true
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.chatMode).mockResolvedValue({} as never)
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ApprovalModePicker focus after close (#18313)', () => {
  function mount(onPicked: () => boolean) {
    const store = createTestStore({
      dashboard: { status: { disabled_approval_modes: [] } } as never,
    })
    return renderWithProviders(<Host picker="approval" onPicked={onPicked} />, { store })
  }

  it('a pick hands focus to the host when onPicked takes it', async () => {
    const user = userEvent.setup()
    const onPicked = vi.fn(focusComposerStub)
    mount(onPicked)

    const trigger = screen.getByLabelText('Approval mode: Normal')
    await user.click(trigger)
    await user.click(screen.getByRole('menuitem', { name: /Trust/i }))

    await waitFor(() => expect(onPicked).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('composer')).toHaveFocus())
    expect(trigger).not.toHaveFocus()
  })

  it('a pick falls back to the trigger when onPicked declines (touch)', async () => {
    const user = userEvent.setup()
    const onPicked = vi.fn(() => false)
    mount(onPicked)

    const trigger = screen.getByLabelText('Approval mode: Normal')
    await user.click(trigger)
    await user.click(screen.getByRole('menuitem', { name: /Trust/i }))

    await waitFor(() => expect(onPicked).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('Escape is a cancel: focus returns to the trigger and onPicked is not consulted', async () => {
    const user = userEvent.setup()
    const onPicked = vi.fn(focusComposerStub)
    mount(onPicked)

    const trigger = screen.getByLabelText('Approval mode: Normal')
    await user.click(trigger)
    await screen.findByRole('menuitem', { name: /Trust/i })
    await user.keyboard('{Escape}')

    await waitFor(() => expect(trigger).toHaveFocus())
    expect(onPicked).not.toHaveBeenCalled()
  })
})

describe('BusySendButton focus after close (#18313)', () => {
  async function openMenu(onPicked: () => boolean) {
    render(<Host picker="busy" onPicked={onPicked} />)
    const caret = screen.getByTestId('busy-send-caret')
    fireEvent.click(caret)
    await screen.findByRole('menu')
    return caret
  }

  it('a pick hands focus to the host when onPicked takes it', async () => {
    const onPicked = vi.fn(focusComposerStub)
    const caret = await openMenu(onPicked)

    fireEvent.click(screen.getByTestId('busy-send-mode-queue'))

    expect(onPicked).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('composer')).toHaveFocus()
    expect(caret).not.toHaveFocus()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('a pick falls back to the caret when onPicked declines (touch)', async () => {
    const onPicked = vi.fn(() => false)
    const caret = await openMenu(onPicked)

    fireEvent.click(screen.getByTestId('busy-send-mode-queue'))

    expect(onPicked).toHaveBeenCalledTimes(1)
    expect(caret).toHaveFocus()
  })

  it('Escape is a cancel: focus returns to the caret and onPicked is not consulted', async () => {
    const onPicked = vi.fn(focusComposerStub)
    const caret = await openMenu(onPicked)

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(caret).toHaveFocus()
    expect(onPicked).not.toHaveBeenCalled()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
