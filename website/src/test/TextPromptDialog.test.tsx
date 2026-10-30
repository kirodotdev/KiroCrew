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

describe('useTextPrompt', () => {
  it('opens with focus in the input, so the user can type at once', async () => {
    const user = userEvent.setup()
    render(<Probe onAnswer={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(within(dialog).getByRole('textbox')).toHaveFocus())
    await user.keyboard('notes.md')
    expect(within(dialog).getByRole('textbox')).toHaveValue('notes.md')
  })

  it('returns submitted text', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox'), 'methods.tex')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))

    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('methods.tex'))
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
