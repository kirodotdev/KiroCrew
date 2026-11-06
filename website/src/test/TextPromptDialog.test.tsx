import { StrictMode, useState, type ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Modal from '../components/Modal'
import { useTextPrompt, type TextPromptOptions } from '../components/TextPromptDialog'

vi.mock('../components/Modal', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/Modal')>()
  return { ...actual, default: vi.fn(actual.default) }
})

const required: TextPromptOptions = {
  title: 'New file', confirmLabel: 'Create file', requiredMessage: 'Enter a file name.',
}

function Probe({ onAnswer, renderDialog = true, opts = required, onRender }: {
  onAnswer: (value: string | null) => void
  renderDialog?: boolean
  opts?: TextPromptOptions
  onRender?: () => void
}) {
  const { textPrompt, textPromptDialog } = useTextPrompt()
  onRender?.()
  return (
    <>
      <button onClick={async () => onAnswer(await textPrompt(opts))}>ask</button>
      {renderDialog && textPromptDialog}
    </>
  )
}

function Renderer({ dialog }: { dialog: ReactNode }) {
  const [visible, setVisible] = useState(true)
  return <><button onClick={() => setVisible(false)}>remove renderer</button>{visible && dialog}</>
}

function SeparateProbe({ onAnswer }: { onAnswer: (value: string | null) => void }) {
  const { textPrompt, textPromptDialog } = useTextPrompt()
  return (
    <>
      <button onClick={async () => onAnswer(await textPrompt(required))}>ask</button>
      <Renderer dialog={textPromptDialog} />
    </>
  )
}

describe('useTextPrompt', () => {
  it('focuses the input and submits typed text with Enter', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const input = within(await screen.findByRole('dialog')).getByRole('textbox')
    await waitFor(() => expect(input).toHaveFocus())
    await user.keyboard('methods.tex{Enter}')
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('methods.tex'))
    expect(onAnswer).toHaveBeenCalledTimes(1)
  })

  it('rejects whitespace-only required text and clears the error on typing', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    const input = within(dialog).getByRole('textbox')
    await user.type(input, '   ')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter a file name.')
    expect(input).toHaveAccessibleDescription('Enter a file name.')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(onAnswer).not.toHaveBeenCalled()
    await user.type(input, 'notes.tex')
    expect(within(dialog).queryByRole('alert')).toBeNull()
    expect(input).not.toHaveAttribute('aria-invalid')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('   notes.tex'))
  })

  it.each(['', '   '])('accepts optional text %j', async value => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} opts={{
      title: 'Commit', confirmLabel: 'Commit', allowEmpty: true,
    }} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    const input = within(dialog).getByRole('textbox')
    expect(input).toHaveValue('')
    if (value) await user.type(input, value)
    await user.click(within(dialog).getByRole('button', { name: 'Commit' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(value))
  })

  it.each(['Cancel', 'Escape', 'Close', 'backdrop'])('answers null on %s', async dismissal => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    if (dismissal === 'Escape') fireEvent.keyDown(window, { key: 'Escape' })
    else if (dismissal === 'backdrop') fireEvent.click(document.querySelector('[class*="backdrop-blur-md"]')!)
    else await user.click(within(dialog).getByRole('button', { name: dismissal }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
    expect(onAnswer).toHaveBeenCalledTimes(1)
  })

  it('answers null to a replaced prompt and on unmount', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const view = render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'ask' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledOnce())
    expect(onAnswer).toHaveBeenCalledWith(null)
    view.unmount()
    await waitFor(() => expect(onAnswer).toHaveBeenCalledTimes(2))
    expect(onAnswer).toHaveBeenLastCalledWith(null)
  })

  it.each([['before', false], ['after', true]] as const)(
    'answers null when the owner omits its renderer %s it opens', async (_when, initiallyVisible) => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    const view = render(<Probe onAnswer={onAnswer} renderDialog={initiallyVisible} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    if (initiallyVisible) {
      await screen.findByRole('dialog')
      view.rerender(<Probe onAnswer={onAnswer} renderDialog={false} />)
    }
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
  })

  it('keeps typing and required-field errors inside the prompt without rerendering its owner', async () => {
    const onRender = vi.fn()
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<Probe onAnswer={onAnswer} onRender={onRender} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    onRender.mockClear()
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter a file name.')
    await user.type(within(dialog).getByRole('textbox'), 'notes.tex')
    expect(onRender).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('notes.tex'))
  })

  it('associates the visible instruction with the input and focuses it when clicked', async () => {
    const user = userEvent.setup()
    render(<Probe onAnswer={vi.fn()} opts={{ ...required, body: 'Choose a file name.' }} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    const input = within(dialog).getByRole('textbox', { name: 'Choose a file name.' })
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    await within(dialog).findByRole('alert')
    await user.click(within(dialog).getByText('Choose a file name.'))
    expect(input).toHaveFocus()
    expect(within(dialog).getByText('Choose a file name.')).toHaveAttribute('for', input.id)
  })

  it('starts a replacement prompt with empty text and no required-field error', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox'), '   ')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    await within(dialog).findByRole('alert')
    await user.click(screen.getByRole('button', { name: 'ask' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
    expect(within(dialog).getByRole('textbox')).toHaveValue('')
    expect(within(dialog).queryByRole('alert')).toBeNull()
  })

  it('keeps the Modal mounted with its content while it closes', async () => {
    const user = userEvent.setup()
    const onAnswer = vi.fn()
    render(<Probe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
    expect(vi.mocked(Modal).mock.calls.at(-1)?.[0]).toMatchObject({
      open: false, title: 'New file',
    })
  })

  it('answers null when a child removes the renderer without rerendering its owner', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<SeparateProbe onAnswer={onAnswer} />)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'remove renderer' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith(null))
  })

  it('keeps the question open across StrictMode effect replay', async () => {
    const onAnswer = vi.fn()
    const user = userEvent.setup()
    render(<StrictMode><Probe onAnswer={onAnswer} /></StrictMode>)
    await user.click(screen.getByRole('button', { name: 'ask' }))
    const dialog = await screen.findByRole('dialog')
    expect(onAnswer).not.toHaveBeenCalled()
    await user.type(within(dialog).getByRole('textbox'), 'notes.tex')
    await user.click(within(dialog).getByRole('button', { name: 'Create file' }))
    await waitFor(() => expect(onAnswer).toHaveBeenCalledWith('notes.tex'))
  })
})
