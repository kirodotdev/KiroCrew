import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import VoicePlaybackNotice from '../components/VoicePlaybackNotice'
import { recentErrors } from '../utils/errorReport'
import { reportVoiceFailure } from '../lib/voiceFailure'

vi.mock('../components/AskAgentButton', () => ({ default: () => null }))

const renderNotice = (slot: string | null | undefined) => render(
  <MemoryRouter><VoicePlaybackNotice slot={slot} /></MemoryRouter>,
)

function emit(type: string, slot = 'chat-a', code = 'voice_synthesis_failed') {
  act(() => { window.dispatchEvent(new CustomEvent(type, { detail: { slot, code } })) })
}

describe('voice playback error notice', () => {
  it.each([undefined, null])('ignores playback events before a conversation is bound (%s)', (slot) => {
    const recordedErrors = [...recentErrors()]
    renderNotice(slot)
    act(() => {
      window.dispatchEvent(new Event('voice-error'))
      window.dispatchEvent(new CustomEvent('voice-error', { detail: null }))
      window.dispatchEvent(new CustomEvent('voice-error', { detail: { slot, code: 'voice_synthesis_failed' } }))
    })
    emit('voice-error', 'chat-b')
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
    expect(recentErrors()).toEqual(recordedErrors)
  })

  it('reports an asynchronous failure and lets the user dismiss it', () => {
    renderNotice('chat-a')
    const before = recentErrors().map(report => report.id)
    act(() => { reportVoiceFailure({ slot: 'chat-a', code: 'voice_synthesis_failed' }) })
    expect(screen.getByTestId('voice-playback-error')).toHaveTextContent('Read aloud failed')
    const settings = screen.getByRole('link', { name: 'Read aloud settings (Text-to-speech)' })
    expect(settings).toHaveAttribute(
      'href', '/settings/voice?highlight=voice.provider-2',
    )
    expect(recentErrors()[0]).toMatchObject({ code: 'voice_synthesis_failed', endpoint: '/api/voice/synthesize' })
    expect(recentErrors().filter(report => !before.includes(report.id))).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
  })

  it('explains browser playback blocking separately', () => {
    renderNotice('chat-a')
    emit('voice-error', 'chat-a', 'voice_playback_blocked')
    const notice = screen.getByTestId('voice-playback-error')
    expect(notice).toHaveTextContent('Your browser blocked automatic playback.')
    expect(notice).toHaveTextContent('More actions')
    expect(notice).toHaveTextContent('then select Read aloud.')
    expect(notice).not.toHaveTextContent(/speaker icon/i)
    expect(notice).not.toHaveTextContent(/again/i)
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('keeps errors and retry cleanup scoped to the visible conversation', () => {
    const { rerender } = renderNotice('chat-a')
    emit('voice-error', 'chat-b')
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
    emit('voice-error')
    emit('voice-synthesis-start', 'chat-b')
    expect(screen.getByTestId('voice-playback-error')).toBeInTheDocument()
    rerender(<MemoryRouter><VoicePlaybackNotice slot="chat-b" /></MemoryRouter>)
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
  })

  it.each([undefined, null])('discards a previous failure while the conversation is unbound (%s)', (slot) => {
    const { rerender } = renderNotice('chat-a')
    emit('voice-error')
    expect(screen.getByTestId('voice-playback-error')).toBeInTheDocument()
    const recordedErrors = [...recentErrors()]

    rerender(<MemoryRouter><VoicePlaybackNotice slot={slot} /></MemoryRouter>)
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
    emit('voice-error')
    emit('voice-error', 'chat-b')
    expect(recentErrors()).toEqual(recordedErrors)

    rerender(<MemoryRouter><VoicePlaybackNotice slot="chat-a" /></MemoryRouter>)
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
    emit('voice-error', 'chat-a', 'voice_playback_blocked')
    expect(screen.getByTestId('voice-playback-error')).toHaveTextContent('browser blocked')
  })

  it.each(['voice-stop', 'voice-synthesis-start'])('clears an error on %s', (type) => {
    renderNotice('chat-a')
    emit('voice-error')
    emit(type)
    expect(screen.queryByTestId('voice-playback-error')).not.toBeInTheDocument()
  })

  it('still journals a failure after the chat notice unmounts', () => {
    const { unmount } = renderNotice('chat-a')
    unmount()
    const before = recentErrors().map(report => report.id)
    reportVoiceFailure({ slot: 'chat-a', code: 'voice_playback_failed' })
    const added = recentErrors().filter(report => !before.includes(report.id))
    expect(added).toHaveLength(1)
    expect(added[0]).toMatchObject({
      source: 'system', code: 'voice_playback_failed', endpoint: '/api/voice/synthesize',
    })
  })

  it.each(['dismiss', 'voice-stop', 'voice-synthesis-start', 'slot change', 'unmount'])(
    'ends the footer recovery reveal when the blocked notice ends through %s', (end) => {
      const onBlockedSlotChange = vi.fn()
      const { rerender, unmount } = render(
        <MemoryRouter><VoicePlaybackNotice slot="chat-a" onBlockedSlotChange={onBlockedSlotChange} /></MemoryRouter>,
      )
      emit('voice-error', 'chat-b', 'voice_playback_blocked')
      expect(onBlockedSlotChange).toHaveBeenLastCalledWith(null)
      emit('voice-error', 'chat-a', 'voice_playback_blocked')
      expect(onBlockedSlotChange).toHaveBeenLastCalledWith('chat-a')
      if (end === 'dismiss') fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
      else if (end === 'slot change') rerender(
        <MemoryRouter><VoicePlaybackNotice slot="chat-b" onBlockedSlotChange={onBlockedSlotChange} /></MemoryRouter>,
      )
      else if (end === 'unmount') unmount()
      else emit(end)
      expect(onBlockedSlotChange).toHaveBeenLastCalledWith(null)
    },
  )
})
