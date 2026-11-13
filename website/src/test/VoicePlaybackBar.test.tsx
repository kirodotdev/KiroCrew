import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../api/client'
import VoicePlaybackBar from '../components/VoicePlaybackBar'
import { setVoicePaused, setVoicePlaying, setVoicePreparing } from '../store/chatSlice'
import { createTestStore, renderWithProviders } from './helpers'

/** The bar's reading-state line; the speed row's note is a second, later status. */
const readingStatus = () => screen.getAllByRole('status')[0]

function mount() {
  const store = createTestStore()
  renderWithProviders(<VoicePlaybackBar />, { store })
  return store
}

describe('VoicePlaybackBar', () => {
  beforeEach(() => {
    vi.spyOn(api, 'voiceConfig').mockResolvedValue({ rate: '100%' })
    vi.spyOn(api, 'updateVoiceConfig').mockResolvedValue({ ok: true })
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('renders nothing while no reply is being read', () => {
    mount()
    expect(screen.queryByTestId('voice-playback-bar')).toBeNull()
  })

  it('says the audio is preparing until playback starts, then that it is reading', () => {
    const store = mount()
    act(() => { store.dispatch(setVoicePreparing(true)) })
    expect(readingStatus().textContent).toBe('Preparing audio…')

    act(() => { store.dispatch(setVoicePlaying(true)) })
    expect(readingStatus().textContent).toBe('Reading aloud')

    // Playing wins while the next sentence is still being prepared.
    act(() => { store.dispatch(setVoicePreparing(false)) })
    expect(readingStatus().textContent).toBe('Reading aloud')

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
      expect(readingStatus().textContent).toBe('Paused')
      expect(screen.queryByRole('button', { name: 'Pause reading' })).toBeNull()
      expect(countEvents('voice-resume', () => {
        fireEvent.click(screen.getByRole('button', { name: 'Resume reading' }))
      })).toBe(1)

      act(() => { store.dispatch(setVoicePaused(false)) })
      expect(readingStatus().textContent).toBe('Reading aloud')
      expect(screen.getByRole('button', { name: 'Pause reading' })).toBeTruthy()
    })

    it('says Paused instead of Preparing audio when paused between sentences', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePreparing(true)) })
      act(() => { store.dispatch(setVoicePaused(true)) })
      expect(readingStatus().textContent).toBe('Paused')
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
      expect(readingStatus().textContent).toBe('Preparing audio…')
      expect(screen.getByRole('button', { name: 'Read again from the start' })).toBe(restart)
      expect(document.activeElement).toBe(restart)
    })
  })

  describe('button layout', () => {
    /** The bar's rows: the direct `div` children of the glass pane (its optics layers are `span`s). */
    const barRows = () => Array.from(screen.getByTestId('voice-playback-bar').firstElementChild!.querySelectorAll(':scope > div'))

    /** A row's action buttons; the Speed group's options are radios, one control, not actions. */
    const actionButtons = (row: Element) => Array.from(row.querySelectorAll('button:not([role="radio"])')).map(button => button.getAttribute('data-testid'))

    it.each([
      ['playing', false],
      ['paused', true],
    ] as const)('puts Pause or Resume and Stop in the first row, and the Speed group with Read again in the second, while %s', (_state, paused) => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      act(() => { store.dispatch(setVoicePaused(paused)) })
      const rows = barRows()
      expect(rows.map(actionButtons)).toEqual([
        ['voice-playback-pause', 'voice-playback-stop'],
        ['voice-playback-restart'],
      ])
      expect(rows[0].querySelector('[role="radiogroup"]')).toBeNull()
      expect(rows[1].querySelector('[role="radiogroup"]')).toBe(screen.getByRole('radiogroup', { name: 'Speed' }))
    })

    it('keeps the status text in the first row and separates the second row with a hairline', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const [firstRow, secondRow] = barRows()
      expect(firstRow.contains(readingStatus())).toBe(true)
      expect(secondRow.classList.contains('border-t')).toBe(true)
      expect(screen.getByTestId('voice-playback-speed-row').classList.contains('items-end')).toBe(true)
    })

    it('puts Read again after the Speed group on the same line', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const groupLine = screen.getByRole('radiogroup', { name: 'Speed' }).parentElement!
      expect(groupLine.lastElementChild).toBe(screen.getByTestId('voice-playback-restart'))
      expect(groupLine.classList.contains('flex-wrap')).toBe(true)
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

    it('keeps the Read again label at every width; the line wraps instead', () => {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      const restart = screen.getByTestId('voice-playback-restart')
      expect(restart.textContent).toBe('Read again')
      expect(restart.querySelector('[class*="sr-only"]')).toBeNull()
    })
  })

  describe('focus when the bar goes away', () => {
    /** Drive the rAF `focusComposer` schedules. */
    const flushFrame = async () => {
      await new Promise<void>(r => requestAnimationFrame(() => r()))
    }
    /** Let the speed control's saved-rate request settle before the bar goes away. */
    const settleSpeedConfig = () => screen.findByRole('radio', { name: '100%', checked: true })
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
      await settleSpeedConfig()
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
      await settleSpeedConfig()
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
        await settleSpeedConfig()
        other.focus()
        act(() => { store.dispatch(setVoicePlaying(false)) })
        await flushFrame()
        expect(document.activeElement).toBe(other)
      } finally {
        other.remove()
      }
    })
  })

  describe('speed control', () => {
    function mountPlaying() {
      const store = mount()
      act(() => { store.dispatch(setVoicePlaying(true)) })
      return store
    }
    const speedGroup = () => screen.getByRole('radiogroup', { name: 'Speed' })
    const speedRadio = (rate: string) => screen.findByRole('radio', { name: rate }) as Promise<HTMLButtonElement>
    const loadedRadio = async (rate: string) => {
      const radio = await speedRadio(rate)
      await waitFor(() => expect(radio.disabled).toBe(false))
      return radio
    }

    it('offers the four presets as one radio group labelled Speed', async () => {
      mountPlaying()
      await loadedRadio('100%')
      expect(within(speedGroup()).getAllByRole('radio').map(radio => radio.textContent)).toEqual(['80%', '100%', '120%', '150%'])
    })

    it('checks the radio for the saved speaking rate and no other', async () => {
      vi.mocked(api.voiceConfig).mockResolvedValue({ rate: '120%' })
      mountPlaying()
      await waitFor(async () => expect((await speedRadio('120%')).getAttribute('aria-checked')).toBe('true'))
      const checked = within(speedGroup()).getAllByRole('radio').filter(radio => radio.getAttribute('aria-checked') === 'true')
      expect(checked.map(radio => radio.textContent)).toEqual(['120%'])
    })

    it('checks no radio for a saved rate that is not a preset', async () => {
      vi.mocked(api.voiceConfig).mockResolvedValue({ rate: '90%' })
      mountPlaying()
      await waitFor(() => expect(api.voiceConfig).toHaveBeenCalled())
      const radios = within(speedGroup()).getAllByRole('radio') as HTMLButtonElement[]
      await waitFor(() => expect(radios.every(radio => !radio.disabled)).toBe(true))
      expect(radios.map(radio => radio.getAttribute('aria-checked'))).toEqual(['false', 'false', 'false', 'false'])
    })

    it('keeps the Speed group out of the status row, in the row below with Read again as its only action', async () => {
      mountPlaying()
      await loadedRadio('100%')
      const statusRow = readingStatus().parentElement!
      expect(within(statusRow).getAllByRole('button').map(button => button.getAttribute('data-testid'))).toEqual([
        'voice-playback-pause',
        'voice-playback-stop',
      ])
      const speedRow = screen.getByTestId('voice-playback-speed-row')
      expect(statusRow.contains(speedRow)).toBe(false)
      expect(statusRow.compareDocumentPosition(speedRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(within(speedRow).getAllByRole('button').map(button => button.getAttribute('data-testid'))).toEqual(['voice-playback-restart'])
      expect(speedRow.contains(speedGroup())).toBe(true)
    })

    it('disables every radio until the saved rate has loaded', () => {
      vi.mocked(api.voiceConfig).mockReturnValue(new Promise(() => {}))
      mountPlaying()
      const radios = within(speedGroup()).getAllByRole('radio') as HTMLButtonElement[]
      expect(radios.every(radio => radio.disabled)).toBe(true)
      expect(radios.some(radio => radio.getAttribute('aria-checked') === 'true')).toBe(false)
    })

    it('shows a load failure through ErrorNotice under the radio group and retries the config request', async () => {
      vi.mocked(api.voiceConfig).mockRejectedValueOnce(new Error('load failed')).mockResolvedValue({ rate: '100%' })
      mountPlaying()

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent("Couldn't load your reading speed.")
      expect(speedGroup().compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
      await waitFor(() => expect(api.voiceConfig).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(screen.queryByTestId('voice-speed-load-error')).toBeNull())
    })

    it('saves the chosen preset as the gateway rate and announces it to the playback owner', async () => {
      vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '150%' })
      mountPlaying()
      const onChanged = vi.fn()
      window.addEventListener('voice-config-changed', onChanged)
      try {
        await loadedRadio('100%')
        fireEvent.click(await speedRadio('150%'))
        await waitFor(() => expect(api.updateVoiceConfig).toHaveBeenCalledWith({ rate: '150%' }))
        await waitFor(async () => expect((await speedRadio('150%')).getAttribute('aria-checked')).toBe('true'))
        expect((onChanged.mock.calls[0][0] as CustomEvent).detail).toEqual({ rate: '150%' })
      } finally {
        window.removeEventListener('voice-config-changed', onChanged)
      }
    })

    it('does not save when the checked preset is chosen again', async () => {
      mountPlaying()
      fireEvent.click(await loadedRadio('100%'))
      expect(api.updateVoiceConfig).not.toHaveBeenCalled()
    })

    describe('keyboard', () => {
      it('puts only the checked radio in the tab order', async () => {
        vi.mocked(api.voiceConfig).mockResolvedValue({ rate: '120%' })
        mountPlaying()
        await loadedRadio('120%')
        const tabIndexes = (within(speedGroup()).getAllByRole('radio') as HTMLButtonElement[]).map(radio => radio.tabIndex)
        expect(tabIndexes).toEqual([-1, -1, 0, -1])
      })

      it('moves focus with the arrow keys, wrapping at the ends, without saving', async () => {
        mountPlaying()
        const hundred = await loadedRadio('100%')
        hundred.focus()

        fireEvent.keyDown(hundred, { key: 'ArrowRight' })
        expect(document.activeElement).toBe(await speedRadio('120%'))
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' })
        expect(document.activeElement).toBe(await speedRadio('150%'))
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' })
        expect(document.activeElement).toBe(await speedRadio('80%'))
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' })
        expect(document.activeElement).toBe(await speedRadio('150%'))
        fireEvent.keyDown(document.activeElement!, { key: 'Home' })
        expect(document.activeElement).toBe(await speedRadio('80%'))
        fireEvent.keyDown(document.activeElement!, { key: 'End' })
        expect(document.activeElement).toBe(await speedRadio('150%'))

        expect(api.updateVoiceConfig).not.toHaveBeenCalled()
      })

      it('saves the focused preset when it is activated', async () => {
        mountPlaying()
        const hundred = await loadedRadio('100%')
        hundred.focus()
        fireEvent.keyDown(hundred, { key: 'ArrowRight' })
        fireEvent.click(document.activeElement!)
        await waitFor(() => expect(api.updateVoiceConfig).toHaveBeenCalledWith({ rate: '120%' }))
      })
    })

    it('shows a save failure, rolls back, and announces the stored rate after reconciliation', async () => {
      vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '100%' })
      vi.mocked(api.updateVoiceConfig).mockRejectedValue(new Error('write failed'))
      const onChanged = vi.fn()
      window.addEventListener('voice-config-changed', onChanged)
      try {
        mountPlaying()
        await loadedRadio('100%')
        fireEvent.click(await speedRadio('120%'))

        const notice = await screen.findByTestId('voice-speed-save-error')
        expect(notice).toHaveTextContent("Couldn't save the speed. Your previous speed is kept.")
        expect(speedGroup().compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
        await waitFor(async () => expect((await speedRadio('100%')).getAttribute('aria-checked')).toBe('true'))
        await waitFor(() => expect(api.voiceConfig).toHaveBeenCalledTimes(2))
        const announced = onChanged.mock.calls.map(([event]) => (event as CustomEvent<{ rate: string }>).detail.rate)
        expect(announced).toEqual(['120%', '100%', '100%'])
      } finally {
        window.removeEventListener('voice-config-changed', onChanged)
      }
    })

    describe('while a save is pending', () => {
      function pendingSave() {
        let finishSave: ((value: { ok: boolean }) => void) | undefined
        vi.mocked(api.updateVoiceConfig).mockReturnValue(new Promise(resolve => { finishSave = resolve }))
        vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '120%' })
        return { finish: () => act(async () => { finishSave?.({ ok: true }) }) }
      }

      it('keeps focus on the chosen radio, marked busy rather than natively disabled', async () => {
        const save = pendingSave()
        mountPlaying()
        const radio = await loadedRadio('120%')
        radio.focus()

        fireEvent.click(radio)
        await waitFor(() => expect(radio.getAttribute('aria-disabled')).toBe('true'))
        expect(radio.getAttribute('aria-busy')).toBe('true')
        expect(radio.disabled).toBe(false)
        expect(document.activeElement).toBe(radio)

        await save.finish()
        await waitFor(() => expect(radio.getAttribute('aria-disabled')).toBeNull())
        expect(document.activeElement).toBe(radio)
      })

      it('ignores a second choice, by pointer or keyboard', async () => {
        const save = pendingSave()
        mountPlaying()
        const radio = await loadedRadio('120%')
        radio.focus()
        fireEvent.click(radio)
        await waitFor(() => expect(radio.getAttribute('aria-disabled')).toBe('true'))

        fireEvent.click(await speedRadio('150%'))
        fireEvent.keyDown(radio, { key: 'ArrowRight' })
        fireEvent.click(document.activeElement!)
        expect(api.updateVoiceConfig).toHaveBeenCalledTimes(1)
        expect(document.activeElement).toBe(radio)

        await save.finish()
        await waitFor(() => expect(radio.getAttribute('aria-disabled')).toBeNull())
      })
    })

    describe('note that the speed applies to what is read next', () => {
      const note = () => screen.getByTestId('voice-speed-applies-next')
      const NOTE = 'Applies to what is read next. Press Read again to hear the whole reply at this speed.'

      it('stays empty until a speed is chosen, then says the speed applies next', async () => {
        vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '120%' })
        mountPlaying()
        await loadedRadio('100%')
        expect(note().getAttribute('role')).toBe('status')
        expect(note().textContent).toBe('')

        fireEvent.click(await speedRadio('120%'))
        await waitFor(() => expect(note().textContent).toBe(NOTE))
      })

      it('does not appear when the checked preset is chosen again', async () => {
        mountPlaying()
        fireEvent.click(await loadedRadio('100%'))
        expect(note().textContent).toBe('')
      })

      it('does not appear when the save fails', async () => {
        vi.mocked(api.updateVoiceConfig).mockRejectedValue(new Error('write failed'))
        mountPlaying()
        await loadedRadio('100%')
        fireEvent.click(await speedRadio('120%'))
        await screen.findByTestId('voice-speed-save-error')
        expect(note().textContent).toBe('')
      })

      it('clears while the next choice saves and returns once it is saved', async () => {
        vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '120%' })
        mountPlaying()
        await loadedRadio('100%')
        fireEvent.click(await speedRadio('120%'))
        await waitFor(() => expect(note().textContent).toBe(NOTE))
        await loadedRadio('150%')

        let finishSave: ((value: { ok: boolean }) => void) | undefined
        vi.mocked(api.updateVoiceConfig).mockReturnValue(new Promise(resolve => { finishSave = resolve }))
        fireEvent.click(await speedRadio('150%'))
        await waitFor(() => expect(note().textContent).toBe(''))
        await act(async () => { finishSave?.({ ok: true }) })
        await waitFor(() => expect(note().textContent).toBe(NOTE))
      })

      it('goes away with the bar when the reading ends', async () => {
        vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '120%' })
        const store = mountPlaying()
        await loadedRadio('100%')
        fireEvent.click(await speedRadio('120%'))
        await waitFor(() => expect(note().textContent).toBe(NOTE))

        act(() => { store.dispatch(setVoicePlaying(false)) })
        expect(screen.queryByTestId('voice-speed-applies-next')).toBeNull()
        act(() => { store.dispatch(setVoicePlaying(true)) })
        await loadedRadio('120%')
        expect(note().textContent).toBe('')
      })

      it('sits below the radio group, so the group does not move when it appears', async () => {
        vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '120%' })
        mountPlaying()
        await loadedRadio('100%')
        const row = screen.getByTestId('voice-playback-speed-row')
        const groupRow = speedGroup().parentElement!
        expect(row.firstElementChild).toBe(groupRow)
        expect(groupRow.nextElementSibling).toBe(note())

        fireEvent.click(await speedRadio('120%'))
        await waitFor(() => expect(note().textContent).toBe(NOTE))
        expect(row.firstElementChild).toBe(groupRow)
      })
    })

    it('refetches on settle and announces the rate stored by the gateway', async () => {
      vi.mocked(api.voiceConfig).mockResolvedValueOnce({ rate: '100%' }).mockResolvedValue({ rate: '120%' })
      const onChanged = vi.fn()
      window.addEventListener('voice-config-changed', onChanged)
      try {
        mountPlaying()
        await loadedRadio('100%')
        fireEvent.click(await speedRadio('150%'))

        await waitFor(() => expect(api.voiceConfig).toHaveBeenCalledTimes(2))
        await waitFor(async () => expect((await speedRadio('120%')).getAttribute('aria-checked')).toBe('true'))
        const events = onChanged.mock.calls.map(([event]) => (event as CustomEvent<{ rate: string }>).detail.rate)
        expect(events.at(-1)).toBe('120%')
      } finally {
        window.removeEventListener('voice-config-changed', onChanged)
      }
    })

    it('leaves the reading going and keeps Stop working', async () => {
      const store = mountPlaying()
      fireEvent.click(await loadedRadio('100%'))
      fireEvent.click(await speedRadio('120%'))
      expect(store.getState().chat.voicePlaying).toBe(true)
      const onStop = vi.fn()
      window.addEventListener('voice-stop', onStop)
      try {
        fireEvent.click(screen.getByRole('button', { name: 'Stop reading' }))
      } finally {
        window.removeEventListener('voice-stop', onStop)
      }
      expect(onStop).toHaveBeenCalledOnce()
      await waitFor(() => expect(screen.getByTestId('voice-speed-applies-next').textContent).not.toBe(''))
    })
  })
})
