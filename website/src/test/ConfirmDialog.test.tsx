/**
 * Unit tests for the shared async confirm surface (useConfirm).
 *
 * The contract every converted call site relies on: the promise resolves
 * `true` only from the confirm button; Cancel, Escape, and the X button all
 * resolve `false`; and the native `window.confirm` is never touched — the
 * native dialog is synchronous, freezes the renderer's event loop, and lets a
 * queued Quit event tear the app down before a follow-up request is sent.
 */
import { StrictMode, useState, type ReactNode } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
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

function ConditionalProbe({ renderDialog, onAnswer }: { renderDialog: boolean; onAnswer: (ok: boolean) => void }) {
  const { confirm, confirmDialog } = useConfirm()
  return (
    <>
      <button onClick={async () => onAnswer(await confirm({ title: 'Proceed?', confirmLabel: 'Proceed' }))}>ask conditionally</button>
      {renderDialog && confirmDialog}
    </>
  )
}

function SplitRenderer({ dialog }: { dialog: ReactNode }) {
  const [renderDialog, setRenderDialog] = useState(true)
  return (
    <>
      <button onClick={() => setRenderDialog(false)}>remove renderer</button>
      {renderDialog && dialog}
    </>
  )
}

function SplitRendererProbe({ onAnswer }: { onAnswer: (ok: boolean) => void }) {
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  return (
    <>
      <button onClick={async () => onAnswer(await confirm({ title: 'Proceed?', confirmLabel: 'Proceed' }))}>ask separately</button>
      <output data-testid="confirm-open">{String(confirmOpen)}</output>
      <SplitRenderer dialog={confirmDialog} />
    </>
  )
}

describe('useConfirm', () => {
  it('answers false when confirm is called without rendering its dialog', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<ConditionalProbe renderDialog={false} onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask conditionally' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('answers false when a pending dialog is removed by an owner branch', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const view = render(<ConditionalProbe renderDialog onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask conditionally' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })

    view.rerender(<ConditionalProbe renderDialog={false} onAnswer={onAnswer} />)

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('answers false when a child removes the separately rendered dialog', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<SplitRendererProbe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask separately' }))
    await screen.findByRole('dialog', { name: 'Proceed?' })

    await user.click(screen.getByRole('button', { name: 'remove renderer' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
  })

  it('clears confirmOpen when a child removes the renderer during exit', async () => {
    const user = userEvent.setup()
    render(<SplitRendererProbe onAnswer={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'ask separately' }))
    const dialog = await screen.findByRole('dialog', { name: 'Proceed?' })
    expect(screen.getByTestId('confirm-open')).toHaveTextContent('true')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(dialog).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'remove renderer' }))

    await waitFor(() => expect(screen.getByTestId('confirm-open')).toHaveTextContent('false'))
  })

  it('keeps a rendered question pending under StrictMode until the user answers', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<StrictMode><ConditionalProbe renderDialog onAnswer={onAnswer} /></StrictMode>)

    await user.click(screen.getByRole('button', { name: 'ask conditionally' }))
    const dialog = await screen.findByRole('dialog', { name: 'Proceed?' })
    expect(onAnswer).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Proceed' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(true))
  })

  it("marks its confirm button, and only that one, as the guide's confirm signal", async () => {
    const user = userEvent.setup()
    render(<Probe onAnswer={vi.fn()} />)
    await user.click(screen.getByText('ask'))
    const dialog = await screen.findByRole('dialog')
    const marked = dialog.querySelectorAll('[data-guide-confirm]')
    expect(marked).toHaveLength(1)
    expect(marked[0]).toBe(screen.getByRole('button', { name: 'Discard draft' }))
  })

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

  it('paints on the layer above a full-screen overlay when asked to', async () => {
    // The artifact and file previews' full-screen shells are opaque body
    // portals at z-[9999]; a discard prompt they raise on the ordinary modal
    // layer (z-[101]) would be covered and the button that raised it look dead.
    const onAnswer = vi.fn()
    const { unmount } = render(<Probe onAnswer={onAnswer} opts={{ layer: 'top' }} />)
    fireEvent.click(screen.getByText('ask'))
    const dialog = await screen.findByRole('dialog')
    const layer = dialog.closest('.fixed.inset-0.z-\\[10001\\]')
    expect(layer).not.toBeNull()
    expect(document.querySelector('.z-\\[10000\\]')).not.toBeNull()
    unmount()

    render(<Probe onAnswer={onAnswer} />)
    fireEvent.click(screen.getByText('ask'))
    const plain = await screen.findByRole('dialog')
    expect(plain.closest('.fixed.inset-0.z-\\[101\\]')).not.toBeNull()
    expect(document.querySelector('.z-\\[10001\\]')).toBeNull()
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

  it('cancel() answers "no" so a caller can withdraw an ask whose subject is gone', async () => {
    function CancelProbe({ onAnswer }: { onAnswer: (ok: boolean) => void }) {
      const { confirm, cancel, confirmDialog } = useConfirm()
      return (
        <>
          <button onClick={async () => onAnswer(await confirm({ title: 'Clear?', confirmLabel: 'Clear' }))}>ask</button>
          <button onClick={cancel}>withdraw</button>
          {confirmDialog}
        </>
      )
    }
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<CancelProbe onAnswer={onAnswer} />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    await user.click(screen.getByText('withdraw'))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(false))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps cancel() the same function across renders, so it is safe in an effect dependency list', async () => {
    const seen: Array<() => void> = []
    function IdentityProbe() {
      const { confirm, cancel, confirmDialog } = useConfirm()
      seen.push(cancel)
      return (
        <>
          <button onClick={() => { void confirm({ title: 'Clear?', confirmLabel: 'Clear' }) }}>ask</button>
          {confirmDialog}
        </>
      )
    }
    const user = userEvent.setup()
    render(<IdentityProbe />)
    await user.click(screen.getByText('ask'))
    await screen.findByRole('dialog')
    expect(seen.length).toBeGreaterThan(1)
    expect(new Set(seen).size).toBe(1)
  })
})
