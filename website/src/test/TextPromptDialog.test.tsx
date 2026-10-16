import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { useTextPrompt, type TextPromptOptions } from '../components/TextPromptDialog'

function Probe({ onAnswer, opts }: { onAnswer: (value: string | null) => void; opts?: Partial<TextPromptOptions> }) {
  const { promptText, textPromptDialog } = useTextPrompt()
  return (
    <>
      <button onClick={async () => onAnswer(await promptText({
        title: 'Name the file',
        confirmLabel: 'Create file',
        ...opts,
      }))}>
        ask
      </button>
      {textPromptDialog}
    </>
  )
}

describe('useTextPrompt', () => {
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

  it('keeps required input open and displays inline validation', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} opts={{ emptyError: 'Enter a file name' }} />)

    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter a file name')
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
