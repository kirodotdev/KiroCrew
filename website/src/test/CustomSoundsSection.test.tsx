import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render as rtlRender, fireEvent, screen, within, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests, playPreset, loadSoundSettings, customSoundId } from '../hooks/useNotificationSound'

vi.mock('../hooks/useNotificationSound', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return { ...actual, playPreset: vi.fn() }
})

const STORAGE_KEY = 'mc-notification-sound'

function render(sub: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return rtlRender(
    <MemoryRouter initialEntries={[`/settings?tab=notifications&sub=${sub}`]}>
      <QueryClientProvider client={qc}><NotificationsPanel /></QueryClientProvider>
    </MemoryRouter>,
  )
}

type Tone = [freq: number, start: number, dur: number, gain: number]

/** Fill the tone rows, adding or removing rows to match, then press a button. */
const fillTones = (tones: Tone[]) => {
  while (screen.queryAllByRole('group', { name: /^Tone \d+$/ }).length < tones.length) {
    fireEvent.click(screen.getByRole('button', { name: 'Add tone' }))
  }
  while (screen.queryAllByRole('group', { name: /^Tone \d+$/ }).length > tones.length) {
    const n = screen.queryAllByRole('group', { name: /^Tone \d+$/ }).length
    fireEvent.click(screen.getByRole('button', { name: `Remove tone ${n}` }))
  }
  tones.forEach((tone, i) => {
    const row = screen.getByRole('group', { name: `Tone ${i + 1}` })
    ;(['Pitch (Hz)', 'Start (sec)', 'Length (sec)', 'Volume (0-1)'] as const).forEach((label, j) => {
      fireEvent.change(within(row).getByLabelText(label), { target: { value: String(tone[j]) } })
    })
  })
}

const addSound = (name: string, tones: Tone[]) => {
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: name } })
  fillTones(tones)
  fireEvent.click(screen.getByRole('button', { name: 'Add sound' }))
}

beforeEach(() => {
  localStorage.clear()
  __resetForTests()
  vi.mocked(playPreset).mockClear()
})

describe('Custom sounds settings', () => {
  it('adds a named sound, lists it, and offers it in the per-category picker', () => {
    const { unmount } = render('custom')
    addSound('myAlert', [[523, 0, 0.2, 1], [784, 0.2, 0.3, 0.9]])
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    expect(stored.customTones.myAlert).toHaveLength(2)
    const list = screen.getByRole('list', { name: 'Custom sounds' })
    expect(within(list).getByText('myAlert')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Play myAlert' }))
    expect(vi.mocked(playPreset).mock.calls[0][0]).toBe(customSoundId('myAlert'))
    unmount()

    // A category set to the custom sound shows it by its own name.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...stored, perCategory: { all: customSoundId('myAlert') } }))
    const { container } = render('percategory')
    // Labelled as the user's own, so it is not mistaken for a built-in.
    expect(container.textContent).toContain('myAlert (yours)')
  })

  it('shows the problem in plain words and saves nothing when a bound is broken', () => {
    render('custom')
    addSound('loud', [[523, 0, 0.2, 5]])
    expect(screen.getByText(/volume must be above 0/)).toBeTruthy()
    // A validation hint, not an application error.
    expect(screen.queryByRole('alert')).toBeNull()
    // The bad field itself is marked, and saving waits for an edit.
    const row = screen.getByRole('group', { name: 'Tone 1' })
    expect(within(row).getByLabelText('Volume (0-1)').getAttribute('aria-invalid')).toBe('true')
    expect(within(row).getByLabelText('Pitch (Hz)').getAttribute('aria-invalid')).toBeNull()
    expect((screen.getByRole('button', { name: 'Add sound' }) as HTMLButtonElement).disabled).toBe(true)
    // The hint sits under the row it is about, tied to the bad field.
    const volume = within(row).getByLabelText('Volume (0-1)')
    const hint = document.getElementById(volume.getAttribute('aria-describedby')!)!
    expect(hint.textContent).toMatch(/volume must be above 0/)
    expect(row.parentElement!.contains(hint)).toBe(true)
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    addSound('chime', [[523, 0, 0.2, 1]])
    expect(screen.getByText(/already used/)).toBeTruthy()
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('explains an empty name on click, and says what was saved', () => {
    render('custom')
    const addBtn = screen.getByRole('button', { name: 'Add sound' }) as HTMLButtonElement
    // Not left off with no reason: the click reports what is missing.
    expect(addBtn.disabled).toBe(false)
    fireEvent.click(addBtn)
    expect(screen.getByText(/Use 1 to 32 letters/)).toBeTruthy()
    // The hint is tied to the Name field and marks it, like a bad tone field.
    const nameInput = screen.getByLabelText('Name')
    expect(nameInput.getAttribute('aria-invalid')).toBe('true')
    expect(nameInput.className).toContain('border-danger')
    const nameHint = document.getElementById(nameInput.getAttribute('aria-describedby')!)!
    expect(nameHint.textContent).toMatch(/Use 1 to 32 letters/)
    expect(nameInput.parentElement!.contains(nameHint)).toBe(true)
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    addSound('myAlert', [[523, 0, 0.2, 1]])
    expect(screen.getByRole('status').textContent).toBe('Saved myAlert.')
  })

  it('starts with a tone the user can hear before saving', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: true, volume: 0.5, perCategory: { all: 'chime' } }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Test' }))
    const [id, , tones] = vi.mocked(playPreset).mock.calls[0]
    expect(id).toBe(customSoundId('preview'))
    expect(Object.values(tones!)[0]).toEqual([
      { freq: 523, start: 0, dur: 0.2, gain: 1 },
      { freq: 784, start: 0.2, dur: 0.3, gain: 0.9 },
    ])
    // Nothing is stored by listening.
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain('preview')
  })

  it('deleting a sound clears the categories that used it', () => {
    const tones = [{ freq: 523, start: 0, dur: 0.2, gain: 1 }]
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      customTones: { mine: tones },
      perCategory: { all: customSoundId('mine'), cron: customSoundId('mine'), hook: 'ding' },
    }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
    const s = loadSoundSettings()
    expect(s.customTones).toBeUndefined()
    expect(s.perCategory).toEqual({ all: 'chime', hook: 'ding' })
    expect(screen.getByText('No custom sounds yet.')).toBeTruthy()

    // Undo brings back the sound and every category that used it.
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    const back = loadSoundSettings()
    expect(back.customTones).toEqual({ mine: tones })
    expect(back.perCategory).toEqual({ all: customSoundId('mine'), cron: customSoundId('mine'), hook: 'ding' })
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull()
  })

  it('undo keeps what was saved after the delete', () => {
    const mine = [{ freq: 523, start: 0, dur: 0.2, gain: 1 }]
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      volume: 0.5,
      customTones: { mine, keep: mine },
      perCategory: { all: customSoundId('mine'), cron: customSoundId('mine'), hook: customSoundId('mine') },
    }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
    // While Undo is offered: add another sound, and repoint one category.
    addSound('later', [[440, 0, 0.3, 0.8]])
    const s = JSON.parse(localStorage.getItem(STORAGE_KEY)!)
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...s, perCategory: { ...s.perCategory, hook: 'ding' } }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    const back = loadSoundSettings()
    expect(Object.keys(back.customTones ?? {}).sort()).toEqual(['keep', 'later', 'mine'])
    expect(back.perCategory).toEqual({ all: customSoundId('mine'), cron: customSoundId('mine'), hook: 'ding' })
  })

  it('undo does not go past the sound limit or reuse a taken name', () => {
    const t = [{ freq: 523, start: 0, dur: 0.2, gain: 1 }]
    const full = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`s${i}`, t]))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: full, perCategory: { all: 'chime' } }))
    const { unmount } = render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete s0' }))
    addSound('s20', [[440, 0, 0.3, 0.8]])
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    // A bound refused it: a plain hint beside Undo, not an error alert.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getAllByRole('status').map(e => e.textContent).join(' ')).toContain('up to 20')
    // Nothing is lost: the newer sound stays, and nothing past the limit is stored.
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY)!).customTones
    expect(Object.keys(raw)).toHaveLength(20)
    expect(Object.hasOwn(raw, 's20')).toBe(true)
    expect(Object.hasOwn(raw, 's0')).toBe(false)
    unmount()

    for (const again of ['Mine', 'mine']) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { mine: t }, perCategory: { all: customSoundId('mine') } }))
      const r = render('custom')
      fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
      // Recreated under the same name (any case) with other tones.
      addSound(again, [[440, 0, 0.3, 0.8]])
      // The new sound replaces the deleted one, so Undo is no longer offered
      // and no notice says it was deleted while it is in the list.
      expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull()
      expect(screen.queryByText(/^Deleted /)).toBeNull()
      expect(screen.queryByRole('alert')).toBeNull()
      const s = loadSoundSettings()
      expect(s.customTones).toEqual({ [again]: [{ freq: 440, start: 0, dur: 0.3, gain: 0.8 }] })
      // The cleared category is not pointed at the replacement.
      expect(s.perCategory.all).toBe('chime')
      r.unmount()
    }

    // Another tab reused the name: Undo is refused once, then withdrawn.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { mine: t }, perCategory: { all: 'chime' } }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { MINE: t }, perCategory: { all: 'chime' } }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getAllByRole('status').map(e => e.textContent).join(' ')).toContain('already used')
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull()
  })

  it('undo leaves a category alone that another tab changed and changed back', () => {
    const mine = [{ freq: 523, start: 0, dur: 0.2, gain: 1 }]
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      customTones: { mine }, perCategory: { all: customSoundId('mine'), cron: customSoundId('mine') },
    }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
    // Another tab: all -> ding -> chime. Both writes land before this tab
    // handles either storage event, so each event must be read on its own.
    const events: StorageEvent[] = []
    for (const v of ['ding', 'chime']) {
      const oldValue = localStorage.getItem(STORAGE_KEY)
      const cur = JSON.parse(oldValue!)
      const newValue = JSON.stringify({ ...cur, perCategory: { ...cur.perCategory, all: v } })
      localStorage.setItem(STORAGE_KEY, newValue)
      events.push(new StorageEvent('storage', { key: STORAGE_KEY, oldValue, newValue }))
    }
    act(() => { for (const e of events) window.dispatchEvent(e) })
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    const back = loadSoundSettings()
    expect(back.customTones).toEqual({ mine })
    // The other tab's choice for "all" stands; the untouched category comes back.
    expect(back.perCategory).toEqual({ all: 'chime', cron: customSoundId('mine') })
  })

  it('adding checks the sounds saved now, not a stale list', () => {
    render('custom')
    // Another tab saves "mine" before this one hears about it.
    const theirs = [{ freq: 330, start: 0, dur: 0.5, gain: 0.5 }]
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { mine: theirs }, perCategory: { all: 'chime' } }))
    addSound('mine', [[523, 0, 0.2, 1]])
    expect(screen.getByText(/already used/)).toBeTruthy()
    expect(loadSoundSettings().customTones).toEqual({ mine: theirs })
  })

  it('undo restores a sound named __proto__ as its own entry', () => {
    localStorage.setItem(STORAGE_KEY,
      '{"perCategory":{"all":"custom:__proto__"},"customTones":{"__proto__":[{"freq":523,"start":0,"dur":0.2,"gain":1}]}}')
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete __proto__' }))
    expect(loadSoundSettings().customTones).toBeUndefined()
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(screen.queryByRole('alert')).toBeNull()
    const back = loadSoundSettings()
    expect(Object.hasOwn(back.customTones ?? {}, '__proto__')).toBe(true)
    expect(back.perCategory.all).toBe(customSoundId('__proto__'))
  })

  it('says so when a category choice cannot be saved, and plays nothing', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ enabled: true, volume: 0.5, perCategory: { all: 'chime' } }))
    render('percategory')
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError') })
    try {
      const trigger = screen.getAllByRole('combobox')[0]
      fireEvent.click(trigger)
      fireEvent.click(screen.getByRole('option', { name: 'Ding' }))
    } finally {
      setItem.mockRestore()
    }
    expect(screen.getByRole('alert').textContent).toContain('Could not save the sound setting')
    expect(vi.mocked(playPreset)).not.toHaveBeenCalled()
    expect(loadSoundSettings().perCategory.all).toBe('chime')
  })

  it('labels the starter tones as an example until they are changed', () => {
    render('custom')
    expect(screen.getByText(/^Starting example\./)).toBeTruthy()
    const row = screen.getByRole('group', { name: 'Tone 1' })
    // The tone's visible heading and its remove button share one line.
    expect(within(row).getByText('Tone 1')).toBeTruthy()
    expect(within(row).getByRole('button', { name: 'Remove tone 1' })).toBeTruthy()
    fireEvent.change(within(row).getByLabelText('Pitch (Hz)'), { target: { value: '600' } })
    expect(screen.queryByText(/^Starting example\./)).toBeNull()
  })

  it('says so when a delete or an undo cannot be saved', () => {
    const mine = [{ freq: 523, start: 0, dur: 0.2, gain: 1 }]
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { mine }, perCategory: { all: 'chime' } }))
    render('custom')
    const real = Storage.prototype.setItem
    let full = true
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, k: string, v: string) {
      if (full) throw new Error('QuotaExceededError')
      real.call(this, k, v)
    })
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
      expect(screen.getByRole('alert').textContent).toContain('Could not delete the sound')
      const alert = screen.getByRole('alert')
      // Shown with the list it is about, not down by the add form. (After a
      // failed delete the list is still there.)
      expect(screen.getByRole('list', { name: 'Custom sounds' }).parentElement!.contains(alert)).toBe(true)
      expect(screen.getByLabelText('Name').closest('div')!.parentElement!.contains(alert)).toBe(false)
      expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull()
      full = false
      fireEvent.click(screen.getByRole('button', { name: 'Delete mine' }))
      full = true
      fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
      expect(screen.getByRole('alert').textContent).toContain('Could not bring the sound back')
      expect(screen.getByRole('button', { name: 'Undo' })).toBeTruthy()
    } finally {
      setItem.mockRestore()
    }
  })
})
