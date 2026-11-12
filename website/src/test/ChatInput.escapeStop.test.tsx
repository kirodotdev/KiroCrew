import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'

// Escape-to-stop on the plain textarea composer (#22315). The gate's full
// precedence matrix lives in composerStopKey.test.ts; these pin the wiring on
// the mounted composer. The rich-text editor's twin is in
// ChatInput.lexicalKeys.test.tsx.

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [value, setValue] = useState('')
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} {...props} />
}

const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement

describe('ChatInput textarea: Escape stops the running turn (#22315)', () => {
  it('a bare Escape while a turn runs calls onStop once; a modified one does not', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape', shiftKey: true })
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('does nothing when no turn is running', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host onStop={onStop} />)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })

  it('an Escape that stopped voice read-back leaves the turn running', () => {
    // Stand-in for the capture-phase stop-speaking shortcut, which marks the
    // keydown it spent before the composer sees it.
    const markSpeech = (e: Event) => { if ((e as KeyboardEvent).key === 'Escape') noteEscapeStoppedSpeech(e) }
    document.addEventListener('keydown', markSpeech, true)
    try {
      const onStop = vi.fn()
      renderWithProviders(<Host isRunning onStop={onStop} />)
      fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
      expect(onStop).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', markSpeech, true)
    }
  })

  it('with a second Escape required, the first shows the hint and only the second stops (#23462)', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
    expect(screen.getByTestId('escape-stop-armed-hint')).toHaveTextContent('Esc again to stop')
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('escape-stop-armed-hint')).toBeNull()
  })
})

describe('the Stop button names the stop key (#23462)', () => {
  it('its tooltip names Esc; its accessible name is unchanged', () => {
    renderWithProviders(<Host isRunning onStop={vi.fn()} />)
    const stop = screen.getByTestId('stop-button-armed')
    expect(stop).toHaveAttribute('title', 'Stop generation (Esc)')
    expect(stop).toHaveAccessibleName('Stop generation')
  })

  it('with a second Escape required, its tooltip does not advertise a single Esc', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
    renderWithProviders(<Host isRunning onStop={vi.fn()} />)
    expect(screen.getByTestId('stop-button-armed')).toHaveAttribute('title', 'Stop generation')
  })

  it('its tooltip follows the setting while the composer stays mounted', () => {
    renderWithProviders(<Host isRunning onStop={vi.fn()} />)
    const stop = screen.getByTestId('stop-button-armed')
    expect(stop).toHaveAttribute('title', 'Stop generation (Esc)')
    localStorage.setItem('mc-chat-config', JSON.stringify({ doubleEscapeToStop: true }))
    act(() => { window.dispatchEvent(new Event('mc-config-changed')) })
    expect(stop).toHaveAttribute('title', 'Stop generation')
  })
})
