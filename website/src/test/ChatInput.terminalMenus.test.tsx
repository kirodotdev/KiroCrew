import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { $getRoot, $selectAll, CONTROLLED_TEXT_INSERTION_COMMAND, getNearestEditorFromDOMNode } from 'lexical'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

beforeEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
  vi.spyOn(api, 'pathComplete').mockResolvedValue({ results: [], root: '/work/project' })
  vi.spyOn(api, 'fileSearch').mockResolvedValue({ results: [] })
  vi.spyOn(api, 'skills').mockResolvedValue([])
})

function Host({
  lexicalComposer = false,
  terminalCommands = 'local',
  onCancel = vi.fn(),
  onSend = vi.fn(),
}: {
  lexicalComposer?: boolean
  terminalCommands?: 'local' | 'remote' | 'pending' | null
  onCancel?: () => void
  onSend?: () => void
}) {
  const [value, setValue] = useState('')
  const [recording, setRecording] = useState(false)
  return (
    <>
      <button onClick={() => setValue('! pwd')}>Restore command draft</button>
      <button onClick={() => setValue('ordinary draft')}>Restore chat draft</button>
      <button onClick={() => setRecording(true)}>Start fixture recording</button>
      <ComposerVoiceSliceOverride inputProps={{ voiceRecording: recording, onVoiceCancel: onCancel }}>
        <ChatInput
          value={value}
          onChange={setValue}
          onSend={onSend}
          onSteer={onSend}
          sentMessages={['previous question']}
          project="/work/project"
          terminalCommands={terminalCommands ?? undefined}
          lexicalComposer={lexicalComposer}
          onFileSelect={vi.fn()}
          onUploadFiles={vi.fn()}
        />
      </ComposerVoiceSliceOverride>
      <output data-testid="draft">{value}</output>
    </>
  )
}

async function typeInto(value: string) {
  const input = await screen.findByRole('textbox', { name: 'Message input' })
  if (input instanceof HTMLTextAreaElement) {
    fireEvent.change(input, { target: { value } })
  } else {
    const editor = getNearestEditorFromDOMNode(input)!
    act(() => {
      editor.update(() => { $selectAll() }, { discrete: true })
      editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, value)
    })
  }
  await waitFor(() => expect(screen.getByTestId('draft').textContent).toBe(value))
  return input
}

function moveCaret(input: HTMLElement, position: 'start' | 'end') {
  if (input instanceof HTMLTextAreaElement) {
    const offset = position === 'start' ? 0 : input.value.length
    input.setSelectionRange(offset, offset)
  } else {
    act(() => {
      getNearestEditorFromDOMNode(input)!.update(() => {
        if (position === 'start') $getRoot().selectStart()
        else $getRoot().selectEnd()
      }, { discrete: true })
    })
  }
}

describe.each([false, true])('terminal completion ownership (Lexical: %s)', lexicalComposer => {
  it.each(['! ./build.sh', '! cat @literal', '! echo $review', '! echo $HOME'])('recalls history and restores literal %s', async draft => {
    const onSend = vi.fn()
    renderWithProviders(<Host lexicalComposer={lexicalComposer} onSend={onSend} />)
    const input = await typeInto(draft)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    moveCaret(input, 'start')
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    await waitFor(() => expect(screen.getByTestId('draft')).toHaveTextContent('previous question'))
    moveCaret(input, 'end')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    await waitFor(() => expect(screen.getByTestId('draft').textContent).toBe(draft))
    expect(onSend).not.toHaveBeenCalled()
  })

  it.each(['! ./build.sh', '! cat @literal', '! echo $review', '! echo $HOME'])('lets Escape cancel dictation beside literal %s', async draft => {
    const onCancel = vi.fn()
    const onSend = vi.fn()
    renderWithProviders(<Host lexicalComposer={lexicalComposer} onCancel={onCancel} onSend={onSend} />)
    await typeInto(draft)
    fireEvent.click(screen.getByText('Start fixture recording'))
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalledOnce()
    expect(screen.getByTestId('draft').textContent).toBe(draft)
    expect(onSend).not.toHaveBeenCalled()
  })

  it.each(['read ./', 'read @file', '$review', '/'])('clears picker %s on restored command entry without reviving it on exit', async initial => {
    renderWithProviders(<Host lexicalComposer={lexicalComposer} />)
    await typeInto(initial)
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Restore command draft'))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Restore chat draft'))
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    await typeInto('read ./')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
  })

  it('offers no chat insertion shortcut for a terminal draft', async () => {
    renderWithProviders(<Host lexicalComposer={lexicalComposer} />)
    await typeInto('! pwd')
    fireEvent.click(screen.getByLabelText('Add files & options'))
    expect(await screen.findByText('Upload file')).toBeVisible()
    expect(screen.queryByTitle('Reference a file')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Slash commands')).not.toBeInTheDocument()
    expect(screen.queryByTitle('Use a skill')).not.toBeInTheDocument()
    expect(screen.getByTestId('draft').textContent).toBe('! pwd')
  })

  it('preserves bang text completion on hosts without terminal command support', async () => {
    renderWithProviders(<Host lexicalComposer={lexicalComposer} terminalCommands={null} />)
    await typeInto('! ./')
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
  })
})
