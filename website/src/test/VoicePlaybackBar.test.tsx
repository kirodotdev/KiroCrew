import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'

import VoicePlaybackBar from '../components/VoicePlaybackBar'
import { setVoicePlaying, setVoicePreparing } from '../store/chatSlice'
import { createTestStore, renderWithProviders } from './helpers'

function mount() {
  const store = createTestStore()
  renderWithProviders(<VoicePlaybackBar />, { store })
  return store
}

describe('VoicePlaybackBar', () => {
  it('renders nothing while no reply is being read', () => {
    mount()
    expect(screen.queryByTestId('voice-playback-bar')).toBeNull()
  })

  it('says the audio is preparing until playback starts, then that it is reading', () => {
    const store = mount()
    act(() => { store.dispatch(setVoicePreparing(true)) })
    expect(screen.getByRole('status').textContent).toBe('Preparing audio…')

    act(() => { store.dispatch(setVoicePlaying(true)) })
    expect(screen.getByRole('status').textContent).toBe('Reading aloud')

    // Playing wins while the next sentence is still being prepared.
    act(() => { store.dispatch(setVoicePreparing(false)) })
    expect(screen.getByRole('status').textContent).toBe('Reading aloud')

    act(() => { store.dispatch(setVoicePlaying(false)) })
    expect(screen.queryByTestId('voice-playback-bar')).toBeNull()
  })

  it('wears the dock glass so the transcript behind it cannot show through the label', () => {
    const store = mount()
    act(() => { store.dispatch(setVoicePlaying(true)) })
    const pane = screen.getByRole('status').parentElement!
    // `glass-thick` is the blur-plus-tint step the other bars above the composer wear;
    // a bare translucent tint is what let the text underneath bleed into the label.
    expect(pane.classList.contains('glass-thick')).toBe(true)
    expect(pane.classList.contains('glass-accent')).toBe(true)
    expect(pane.className).not.toMatch(/\bbg-accent-subtle\b/)
  })

  it.each([
    ['preparing', setVoicePreparing],
    ['playing', setVoicePlaying],
  ] as const)('Stop requests a voice stop while %s', (_state, action) => {
    const store = mount()
    act(() => { store.dispatch(action(true)) })
    const onStop = vi.fn()
    window.addEventListener('voice-stop', onStop)
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Stop reading' }))
    } finally {
      window.removeEventListener('voice-stop', onStop)
    }
    expect(onStop).toHaveBeenCalledOnce()
  })

  describe('Read again', () => {
    it.each([
      ['preparing', setVoicePreparing],
      ['playing', setVoicePlaying],
    ] as const)('requests a voice restart while %s', (_state, action) => {
      const store = mount()
      act(() => { store.dispatch(action(true)) })
      const onRestart = vi.fn()
      const onStop = vi.fn()
      window.addEventListener('voice-restart', onRestart)
      window.addEventListener('voice-stop', onStop)
      try {
        fireEvent.click(screen.getByRole('button', { name: 'Read again from the start' }))
      } finally {
        window.removeEventListener('voice-restart', onRestart)
        window.removeEventListener('voice-stop', onStop)
      }
      expect(onRestart).toHaveBeenCalledOnce()
      expect(onStop).not.toHaveBeenCalled()
    })

    it('shows the short label next to the Stop button', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      expect(screen.getByTestId('voice-playback-restart').textContent).toBe('Read again')
      expect(screen.getByRole('button', { name: 'Stop reading' })).toBeTruthy()
    })

    it('keeps focus on the button while the reading restarts', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const restart = screen.getByRole('button', { name: 'Read again from the start' })
      restart.focus()
      const onRestart = () => {
        store.dispatch(setVoicePlaying(false))
        store.dispatch(setVoicePreparing(true))
      }
      window.addEventListener('voice-restart', onRestart)
      try {
        fireEvent.click(restart)
      } finally {
        window.removeEventListener('voice-restart', onRestart)
      }
      expect(screen.getByRole('status').textContent).toBe('Preparing audio…')
      expect(screen.getByRole('button', { name: 'Read again from the start' })).toBe(restart)
      expect(document.activeElement).toBe(restart)
    })
  })

  describe('focus when the bar goes away', () => {
    /** Drive the rAF `focusComposer` schedules. */
    const flushFrame = async () => {
      await new Promise<void>(r => requestAnimationFrame(() => r()))
    }
    let composer: HTMLTextAreaElement

    beforeEach(() => {
      composer = document.createElement('textarea')
      composer.setAttribute('data-composer-input', '')
      document.body.appendChild(composer)
    })
    afterEach(() => { composer.remove() })

    it('moves focus to the composer when Stop is pressed', async () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const stop = screen.getByRole('button', { name: 'Stop reading' })
      stop.focus()
      const onStop = () => store.dispatch(setVoicePlaying(false))
      window.addEventListener('voice-stop', onStop)
      try {
        act(() => { fireEvent.click(stop) })
      } finally {
        window.removeEventListener('voice-stop', onStop)
      }
      expect(screen.queryByTestId('voice-playback-bar')).toBeNull()
      await flushFrame()
      expect(document.activeElement).toBe(composer)
    })

    it('moves focus to the composer when the reading ends while Stop is focused', async () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      screen.getByRole('button', { name: 'Stop reading' }).focus()
      act(() => { store.dispatch(setVoicePlaying(false)) })
      await flushFrame()
      expect(document.activeElement).toBe(composer)
    })

    it('leaves focus alone when the reading ends while focus is elsewhere', async () => {
      const other = document.createElement('button')
      document.body.appendChild(other)
      try {
        const store = mount()
        act(() => { store.dispatch(setVoicePlaying(true)) })
        other.focus()
        act(() => { store.dispatch(setVoicePlaying(false)) })
        await flushFrame()
        expect(document.activeElement).toBe(other)
      } finally {
        other.remove()
      }
    })
  })
})
