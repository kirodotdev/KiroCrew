/**
 * Unit tests for the shared async confirm surface (useConfirm).
 *
 * The contract every converted call site relies on: the promise resolves
 * `true` only from the confirm button; Cancel, Escape, and the X button all
 * resolve `false`; and the native `window.confirm` is never touched — the
 * native dialog is synchronous, freezes the renderer's event loop, and lets a
 * queued Quit event tear the app down before a follow-up request is sent.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useConfirm, type ConfirmOptions } from '../components/ConfirmDialog'

function Probe({ onAnswer, opts }: { onAnswer: (ok: boolean) => void; opts?: Partial<ConfirmOptions> }) {
  const { confirm, confirmDialog } = useConfirm()
  return (
    <>
      <button
        onClick={async () =>
          onAnswer(
            await confirm({
              title: 'Discard draft?',
              body: 'The draft is gone for good.',
              confirmLabel: 'Discard draft',
              ...opts,
            }),
          )
        }
      >
        ask
      </button>
      {confirmDialog}
    </>
  )
}

describe('useConfirm', () => {
  it('renders a themed dialog and never calls window.confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toBeInTheDocument()
    expect(screen.getByText('Discard draft?')).toBeInTheDocument()
    expect(screen.getByText('The draft is gone for good.')).toBeInTheDocument()
    // The confirm button restates the action — never "OK".
    expect(screen.getByRole('button', { name: 'Discard draft' })).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()
    // Opening the dialog alone answers nothing.
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('resolves true only from the confirm button', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await user.click(await screen.findByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(true))
    expect(onAnswer).toHaveBeenCalledTimes(1)
  })

  it('resolves false from Cancel', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('resolves false from Escape', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('resolves false from the X button', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    await user.click(screen.getByLabelText('Close'))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('a second ask answers the first "no" instead of leaking its promise', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    await user.click(screen.getByText('ask'))
    // First promise settled false; the second is still pending.
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
    expect(onAnswer).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Discard draft' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2))
    expect(onAnswer).toHaveBeenLastCalledWith(true)
  })

  it('an unmount while open answers "no" so the awaiting caller is released', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const { unmount } = render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    unmount()
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })
})
