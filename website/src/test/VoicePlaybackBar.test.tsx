import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'

import VoicePlaybackBar from '../components/VoicePlaybackBar'
import { setVoicePaused, setVoicePlaying, setVoicePreparing } from '../store/chatSlice'
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
    const pane = screen.getByTestId('voice-playback-bar').firstElementChild!
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

  describe('pause and resume', () => {
    /** Listen for `type` on the window for the duration of `run`; returns how often it fired. */
    function countEvents(type: string, run: () => void): number {
      const listener = vi.fn()
      window.addEventListener(type, listener)
      try { run() } finally { window.removeEventListener(type, listener) }
      return listener.mock.calls.length
    }

    it.each([
      ['preparing', setVoicePreparing],
      ['playing', setVoicePlaying],
    ] as const)('offers Pause reading while %s', (_state, action) => {
      const store = mount()
      act(() => { store.dispatch(action(true)) })
      expect(screen.getByRole('button', { name: 'Pause reading' })).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Resume reading' })).toBeNull()
    })

    it('requests a pause, then offers Resume reading once the store says paused', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      expect(countEvents('voice-pause', () => {
        fireEvent.click(screen.getByRole('button', { name: 'Pause reading' }))
      })).toBe(1)

      act(() => { store.dispatch(setVoicePaused(true)) })
      expect(screen.getByRole('status').textContent).toBe('Paused')
      expect(screen.queryByRole('button', { name: 'Pause reading' })).toBeNull()
      expect(countEvents('voice-resume', () => {
        fireEvent.click(screen.getByRole('button', { name: 'Resume reading' }))
      })).toBe(1)

      act(() => { store.dispatch(setVoicePaused(false)) })
      expect(screen.getByRole('status').textContent).toBe('Reading aloud')
      expect(screen.getByRole('button', { name: 'Pause reading' })).toBeTruthy()
    })

    it('says Paused instead of Preparing audio when paused between sentences', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePreparing(true)) })
      act(() => { store.dispatch(setVoicePaused(true)) })
      expect(screen.getByRole('status').textContent).toBe('Paused')
      expect(screen.getByRole('button', { name: 'Resume reading' })).toBeTruthy()
    })

    it('keeps Stop reading available while paused', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      act(() => { store.dispatch(setVoicePaused(true)) })
      expect(countEvents('voice-stop', () => {
        fireEvent.click(screen.getByRole('button', { name: 'Stop reading' }))
      })).toBe(1)
    })

    it('keeps focus on the same button while it switches between Pause and Resume', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const toggle = screen.getByTestId('voice-playback-pause')
      toggle.focus()
      act(() => { store.dispatch(setVoicePaused(true)) })
      expect(screen.getByTestId('voice-playback-pause')).toBe(toggle)
      expect(document.activeElement).toBe(toggle)
      expect(toggle.getAttribute('aria-label')).toBeNull()
      expect(toggle.textContent).toBe('Resume reading')
    })

    it('goes away, and the paused flag clears, when nothing is playing or preparing', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      act(() => { store.dispatch(setVoicePaused(true)) })
      act(() => { store.dispatch(setVoicePlaying(false)) })
      expect(screen.queryByTestId('voice-playback-bar')).toBeNull()
      expect(store.getState().chat.voicePaused).toBe(false)
    })
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

    it('shows the short label', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      expect(screen.getByTestId('voice-playback-restart').textContent).toBe('Read again')
    })

    it('stays available while paused', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      act(() => { store.dispatch(setVoicePaused(true)) })
      const onRestart = vi.fn()
      window.addEventListener('voice-restart', onRestart)
      try {
        fireEvent.click(screen.getByRole('button', { name: 'Read again from the start' }))
      } finally {
        window.removeEventListener('voice-restart', onRestart)
      }
      expect(onRestart).toHaveBeenCalledOnce()
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

  describe('button layout', () => {
    /** The bar's rows: the direct `div` children of the glass pane (its optics layers are `span`s). */
    const barRows = () => Array.from(screen.getByTestId('voice-playback-bar').firstElementChild!.querySelectorAll(':scope > div'))

    it.each([
      ['playing', false],
      ['paused', true],
    ] as const)('puts Pause or Resume and Stop in the first row and Read again alone in the second while %s', (_state, paused) => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      act(() => { store.dispatch(setVoicePaused(paused)) })
      const rows = barRows().map(row => Array.from(row.querySelectorAll('button')).map(button => button.getAttribute('data-testid')))
      expect(rows).toEqual([
        ['voice-playback-pause', 'voice-playback-stop'],
        ['voice-playback-restart'],
      ])
    })

    it('keeps the status text in the first row and separates the second row with a hairline', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const [firstRow, secondRow] = barRows()
      expect(firstRow.contains(screen.getByRole('status'))).toBe(true)
      expect(secondRow.classList.contains('border-t')).toBe(true)
      expect(secondRow.classList.contains('justify-end')).toBe(true)
    })
  })

  describe('narrow bars', () => {
    it('hides the Pause or Resume and Stop labels by the bar width, so the status keeps its room', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      expect(screen.getByTestId('voice-playback-bar').classList.contains('@container')).toBe(true)
      for (const id of ['voice-playback-pause', 'voice-playback-stop']) {
        const label = screen.getByTestId(id).querySelector('span')!
        expect(label.classList.contains('@max-[680px]:sr-only')).toBe(true)
      }
    })

    it('keeps the labels as the accessible names while they are hidden', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      expect(screen.getByTestId('voice-playback-pause').textContent).toBe('Pause reading')
      expect(screen.getByTestId('voice-playback-stop').textContent).toBe('Stop reading')
    })

    it('leaves Read again, alone on its row, to the viewport rule', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const label = screen.getByTestId('voice-playback-restart').querySelector('span')!
      expect(label.className).toBe('max-[360px]:sr-only')
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
