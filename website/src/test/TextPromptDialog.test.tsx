import { StrictMode, useState, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { useTextPrompt, type TextPromptOptions } from '../components/TextPromptDialog'

function Probe({ onAnswer, opts }: { onAnswer: (value: string | null) => void; opts?: Partial<TextPromptOptions> }) {
  const { textPrompt, textPromptDialog } = useTextPrompt()
  const base = { title: 'Name the file', confirmLabel: 'Create file' }
  const ask = (opts?.allowEmpty
    ? { ...base, ...opts }
    : { ...base, requiredMessage: 'Enter a file name.', ...opts }) as TextPromptOptions
  return (
    <>
      <button onClick={async () => onAnswer(await textPrompt(ask))}>
        ask
      </button>
      {textPromptDialog}
    </>
  )
}

function ConditionalProbe({ renderDialog, onAnswer }: { renderDialog: boolean; onAnswer: (value: string | null) => void }) {
  const { textPrompt, textPromptDialog } = useTextPrompt()
  const ask: TextPromptOptions = { title: 'Name the file', confirmLabel: 'Create file', requiredMessage: 'Enter a file name.' }
  return (
    <>
      <button onClick={async () => onAnswer(await textPrompt(ask))}>ask conditionally</button>
      {renderDialog && textPromptDialog}
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

function SplitRendererProbe({ onAnswer }: { onAnswer: (value: string | null) => void }) {
  const { textPrompt, textPromptDialog } = useTextPrompt()
  const ask: TextPromptOptions = { title: 'Name the file', confirmLabel: 'Create file', requiredMessage: 'Enter a file name.' }
  return (
    <>
      <button onClick={async () => onAnswer(await textPrompt(ask))}>ask separately</button>
      <SplitRenderer dialog={textPromptDialog} />
    </>
  )
}

describe('useTextPrompt', () => {
  it('answers null when textPrompt is called without rendering its dialog', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<ConditionalProbe renderDialog={false} onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask conditionally' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('answers null when a pending dialog is removed by an owner branch', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const view = render(<ConditionalProbe renderDialog onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask conditionally' }))
    await screen.findByRole('dialog', { name: 'Name the file' })

    view.rerender(<ConditionalProbe renderDialog={false} onAnswer={onAnswer} />)

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
  })

  it('answers null when a child removes the separately rendered dialog', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<SplitRendererProbe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask separately' }))
    await screen.findByRole('dialog', { name: 'Name the file' })

    await user.click(screen.getByRole('button', { name: 'remove renderer' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
  })

  it('keeps a rendered prompt pending under StrictMode until the user answers', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<StrictMode><ConditionalProbe renderDialog onAnswer={onAnswer} /></StrictMode>)

    await user.click(screen.getByRole('button', { name: 'ask conditionally' }))
    const dialog = await screen.findByRole('dialog', { name: 'Name the file' })
    expect(onAnswer).not.toHaveBeenCalled()
    await user.type(within(dialog).getByRole('textbox'), 'notes.md')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('notes.md'))
  })

  it('opens with focus in the input, so the user can type at once', async () => {
    const user = userEvent.setup()
    render(<Probe onAnswer={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('textbox')).toHaveFocus())
    await user.keyboard('notes.md')
    expect(within(dialog).getByRole('textbox')).toHaveValue('notes.md')
  })

  it('returns submitted text without using window.prompt', async () => {
    const promptSpy = vi.spyOn(window, 'prompt')
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox'), 'methods.tex')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('methods.tex'))
    expect(promptSpy).not.toHaveBeenCalled()
  })

  it("shows the caller's required-value message, not the prompt title, on a blank submit", async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter a file name.')
    expect(within(dialog).getByRole('alert')).not.toHaveTextContent('Name the file')
    // The field names its error, so returning to it reads the instruction.
    expect(within(dialog).getByRole('textbox')).toHaveAccessibleDescription('Enter a file name.')
    expect(onAnswer).not.toHaveBeenCalled()
  })

  it('allows an empty answer when configured', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} opts={{ allowEmpty: true }} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Create file' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(''))
  })

  it('shows the placeholder in the empty field and still answers a blank submit with an empty string', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} opts={{ allowEmpty: true, placeholder: 'Update from Papyrus' }} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('textbox')).toHaveAttribute('placeholder', 'Update from Papyrus')
    expect(within(dialog).getByRole('textbox')).toHaveValue('')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(''))
  })

  it.each([
    ['Cancel', async () => { await userEvent.setup().click(screen.getByRole('button', { name: 'Cancel' })) }],
    ['Escape', async () => { fireEvent.keyDown(window, { key: 'Escape' }) }],
  ])('returns null from %s', async (_name, dismiss) => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    await screen.findByRole('dialog')
    await dismiss()

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
  })

  it('answers a replaced prompt and an unmount with null', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const { unmount } = render(<Probe onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'ask' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))

    unmount()
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2))
    expect(onAnswer).toHaveBeenLastCalledWith(null)
  })
})
