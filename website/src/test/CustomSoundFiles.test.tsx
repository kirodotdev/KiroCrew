/**
 * Adding, auditioning, picking and deleting an uploaded sound file through the
 * real Notifications settings panel. The byte store and the decoder are
 * mocked; what is asserted is what the panel saves and when it touches bytes.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render as rtlRender, fireEvent, screen, act, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NotificationsPanel } from '../pages/settings/NotificationsPanel'
import { __resetForTests, playPreset, playSoundFile, fileSoundId, CUSTOM_TONE_LIMITS } from '../hooks/useNotificationSound'
import {
  putSoundFile, getSoundFile, deleteSoundFile, pruneSoundFiles, readAudioFile,
  newSoundFileKey, ORPHAN_GRACE_MS, SOUND_FILE_KEY_RE,
} from '../hooks/customSoundFiles'

vi.mock('../hooks/useNotificationSound', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return { ...actual, playPreset: vi.fn(), playSoundFile: vi.fn() }
})
vi.mock('../hooks/customSoundFiles', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/customSoundFiles')>()
  return {
    ...actual,
    putSoundFile: vi.fn(async () => true),
    getSoundFile: vi.fn(async () => new Blob(['x'])),
    deleteSoundFile: vi.fn(async () => undefined),
    pruneSoundFiles: vi.fn(async () => null),
    readAudioFile: vi.fn(async (f: Blob) => ({ bytes: f.size, seconds: 1.2 })),
  }
})

const STORAGE_KEY = 'mc-notification-sound'
/** Each form's own card: both share the words "Name", "Play" and "Save
 *  sound", so every query is scoped to the card it means. */
const fileCard = () => within(screen.getByRole('region', { name: 'Use an audio file' }))
const toneCard = () => within(screen.getByRole('region', { name: 'Build from tones' }))
const stored = () => JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')
const tone = [{ freq: 440, start: 0, dur: 0.2, gain: 1 }]
/** The one instant every key and grace check in this file is judged against:
 *  `Date.now` is pinned to it in `beforeEach`, so no age depends on the host clock. */
const NOW = Date.parse('2026-01-15T12:00:00Z')
/** A key old enough to be past the orphan grace window. */
const oldKey = () => newSoundFileKey(NOW - 2 * ORPHAN_GRACE_MS)
const rec = (key = oldKey()) => ({ bytes: 10, seconds: 1, key })

function render(sub: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return rtlRender(
    <MemoryRouter initialEntries={[`/settings?tab=notifications&sub=${sub}`]}>
      <QueryClientProvider client={qc}><NotificationsPanel /></QueryClientProvider>
    </MemoryRouter>,
  )
}

const audio = (name = 'doorbell.mp3', bytes = 2000) => new File([new Uint8Array(bytes)], name, { type: 'audio/mpeg' })

const pick = async (file: File) => {
  await act(async () => {
    fireEvent.change(fileCard().getByLabelText('File'), { target: { files: [file] } })
  })
}

const addFile = async (name?: string) => {
  if (name !== undefined) fireEvent.change(fileCard().getByLabelText('Name'), { target: { value: name } })
  await act(async () => { fireEvent.click(fileCard().getByRole('button', { name: 'Save sound' })) })
}

const clickUndo = async () => {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Undo' })) })
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(NOW)
  localStorage.clear()
  __resetForTests()
  vi.mocked(putSoundFile).mockReset().mockResolvedValue(true)
  vi.mocked(getSoundFile).mockReset().mockResolvedValue(new Blob(['x']))
  vi.mocked(deleteSoundFile).mockClear()
  vi.mocked(pruneSoundFiles).mockReset().mockResolvedValue(null)
  vi.mocked(readAudioFile).mockReset().mockImplementation(async (f: Blob) => ({ bytes: f.size, seconds: 1.2 }))
  vi.mocked(playSoundFile).mockClear()
  vi.mocked(playPreset).mockClear()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:x'), revokeObjectURL: vi.fn() }))
})

afterEach(() => { vi.mocked(Date.now).mockRestore() })

describe('Custom sound files', () => {
  it('adds a picked file under a fresh storage key and offers it in the picker', async () => {
    const { unmount } = render('custom')
    const file = audio('door-bell!.mp3')
    await pick(file)
    expect(fileCard().getByLabelText('Name')).toHaveValue('door-bell')
    await addFile()
    const [key, blob] = vi.mocked(putSoundFile).mock.calls[0]
    expect(blob).toBe(file)
    // Stored under a per-write key, never under the sound's name.
    expect(SOUND_FILE_KEY_RE.test(key)).toBe(true)
    expect(stored().customFiles).toEqual({ 'door-bell': { bytes: 2000, seconds: 1.2, key } })
    expect(screen.getByText('Saved door-bell.')).toBeInTheDocument()
    const list = screen.getByRole('list', { name: 'Your sounds' })
    expect(within(list).getByText('door-bell')).toBeTruthy()
    // The list's Play button plays it by its file id, not as a tone sound.
    fireEvent.click(screen.getByRole('button', { name: 'Play door-bell' }))
    expect(vi.mocked(playPreset).mock.calls[0][0]).toBe(fileSoundId('door-bell'))
    unmount()
    // A category set to the file shows it by its own name.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...stored(), perCategory: { all: fileSoundId('door-bell') } }))
    const { container } = render('percategory')
    expect(container.textContent).toContain('door-bell (yours)')
  })

  it('lets the user hear the picked file before saving, from a revoked blob: URL', async () => {
    render('custom')
    await pick(audio())
    fireEvent.click(fileCard().getByRole('button', { name: 'Play' }))
    expect(playSoundFile).toHaveBeenCalledWith('blob:x', expect.any(Number), expect.any(Function))
    vi.mocked(playSoundFile).mock.calls[0][2]?.()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:x')
    expect(putSoundFile).not.toHaveBeenCalled()
  })

  it('says why a picked file is refused, marks the picker invalid, and stores nothing', async () => {
    render('custom')
    vi.mocked(readAudioFile).mockResolvedValueOnce({ problem: 'file_length' })
    await pick(audio())
    expect(screen.getByText('The sound must be 5 seconds or shorter.')).toBeInTheDocument()
    expect(fileCard().getByLabelText('File')).toHaveAttribute('aria-invalid', 'true')
    expect(fileCard().getByRole('button', { name: 'Play' })).toBeDisabled()
    // Nothing to save: Save stays off, and the real reason stays on screen
    // rather than being replaced by a different one.
    fireEvent.change(fileCard().getByLabelText('Name'), { target: { value: 'Long' } })
    expect(fileCard().getByRole('button', { name: 'Save sound' })).toBeDisabled()
    expect(screen.getByText('The sound must be 5 seconds or shorter.')).toBeInTheDocument()
    expect(putSoundFile).not.toHaveBeenCalled()
    expect(stored().customFiles).toBeUndefined()
  })

  it('keeps a name typed while the file was still being checked', async () => {
    render('custom')
    let finish: (v: { bytes: number; seconds: number }) => void = () => {}
    vi.mocked(readAudioFile).mockReturnValueOnce(new Promise(r => { finish = r }))
    fireEvent.change(fileCard().getByLabelText('File'), { target: { files: [audio('doorbell.mp3')] } })
    fireEvent.change(fileCard().getByLabelText('Name'), { target: { value: 'My bell' } })
    await act(async () => { finish({ bytes: 2000, seconds: 1 }) })
    expect(fileCard().getByLabelText('Name')).toHaveValue('My bell')
  })

  it('refuses a name a tone sound already uses, before writing any bytes', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { Bell: tone } }))
    render('custom')
    await pick(audio())
    await addFile('bell')
    expect(screen.getByText('That name is already used.')).toBeInTheDocument()
    expect(putSoundFile).not.toHaveBeenCalled()
  })

  it('counts files toward the shared limit', async () => {
    const tones = Object.fromEntries(Array.from({ length: CUSTOM_TONE_LIMITS.maxSounds }, (_, i) => [`t${i}`, tone]))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: tones }))
    render('custom')
    await pick(audio())
    await addFile('One more')
    expect(screen.getByText(`You can keep up to ${CUSTOM_TONE_LIMITS.maxSounds} custom sounds.`)).toBeInTheDocument()
    expect(putSoundFile).not.toHaveBeenCalled()
  })

  it('checks the limit against both kinds as saved NOW, when another tab fills the last slot after the pane opened', async () => {
    const files = Object.fromEntries(Array.from({ length: CUSTOM_TONE_LIMITS.maxSounds - 1 }, (_, i) => [`f${i}`, rec()]))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: files }))
    render('custom')
    // Another tab adds a TONE sound, filling the last slot.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...stored(), customTones: { late: tone } }))
    await pick(audio())
    await addFile('One more')
    expect(putSoundFile).not.toHaveBeenCalled()
    expect(Object.keys(stored().customFiles)).toHaveLength(CUSTOM_TONE_LIMITS.maxSounds - 1)
    // And the tone form refuses the same way when another tab adds a FILE.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { ...files, lateFile: rec() } }))
    fireEvent.change(toneCard().getByLabelText('Name'), { target: { value: 'Tone more' } })
    fireEvent.click(toneCard().getByRole('button', { name: 'Save sound' }))
    expect(stored().customTones).toBeUndefined()
  })

  it('when another tab saves the SAME name while the bytes are written, deletes only its own bytes', async () => {
    render('custom')
    await pick(audio())
    const theirs = rec()
    vi.mocked(putSoundFile).mockImplementationOnce(async () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: theirs } }))
      return true
    })
    await addFile('Door')
    const [myKey] = vi.mocked(putSoundFile).mock.calls[0]
    expect(myKey).not.toBe(theirs.key)
    expect(deleteSoundFile).toHaveBeenCalledTimes(1)
    expect(deleteSoundFile).toHaveBeenCalledWith(myKey)
    // The other tab's record, and the bytes its key names, are untouched.
    expect(stored().customFiles).toEqual({ Door: theirs })
    expect(screen.getByText('That name is already used.')).toBeInTheDocument()
  })

  it('shows a save failure when the bytes cannot be stored, and saves no record', async () => {
    vi.mocked(putSoundFile).mockResolvedValueOnce(false)
    render('custom')
    await pick(audio())
    await addFile('Door')
    expect(screen.getByText(/Could not save the sound/)).toBeInTheDocument()
    expect(stored().customFiles).toBeUndefined()
  })

  it('deleting a file clears its categories but keeps the bytes, so Undo restores both', async () => {
    const door = rec()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      perCategory: { all: 'chime', turn: fileSoundId('Door') },
      customFiles: { Door: door },
    }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete Door' }))
    expect(stored().customFiles).toEqual({})
    expect(stored().perCategory.turn).toBeUndefined()
    expect(deleteSoundFile).not.toHaveBeenCalled()
    await clickUndo()
    expect(getSoundFile).toHaveBeenCalledWith(door.key)
    expect(stored().customFiles).toEqual({ Door: door })
    expect(stored().perCategory.turn).toBe('file:Door')
  })

  it('Undo says so, and restores nothing, when the bytes were collected meanwhile', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      perCategory: { turn: fileSoundId('Door') },
      customFiles: { Door: rec() },
    }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete Door' }))
    // Another tab opened this pane and collected the orphaned bytes.
    vi.mocked(getSoundFile).mockResolvedValueOnce(null)
    await clickUndo()
    expect(stored().customFiles).toEqual({})
    expect(stored().perCategory.turn).toBeUndefined()
    expect(screen.getByText(/no longer stored in this browser/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull()
  })

  it('Undo of a file does not go past the sound limit or reuse a taken name', async () => {
    const files = Object.fromEntries(Array.from({ length: CUSTOM_TONE_LIMITS.maxSounds - 1 }, (_, i) => [`f${i}`, rec()]))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { ...files, Door: rec() } }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete Door' }))
    // Another tab fills the freed slot.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...stored(), customTones: { late: tone } }))
    await clickUndo()
    expect(Object.hasOwn(stored().customFiles, 'Door')).toBe(false)
    expect(screen.getByText(`You can keep up to ${CUSTOM_TONE_LIMITS.maxSounds} custom sounds.`)).toBeInTheDocument()
    // Freeing room and reusing the name elsewhere: Undo is refused as taken.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { door: rec() } }))
    await clickUndo()
    expect(Object.keys(stored().customFiles)).toEqual(['door'])
    expect(screen.getByText('That name is already used.')).toBeInTheDocument()
  })

  it('on opening, asks the store to keep the keys saved records point at, read fresh each time', async () => {
    const door = rec()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: door } }))
    render('custom')
    await waitFor(() => expect(pruneSoundFiles).toHaveBeenCalledTimes(1))
    const referenced = vi.mocked(pruneSoundFiles).mock.calls[0][0]
    expect([...referenced()]).toEqual([door.key])
    // A record another tab saves later is seen by the next read.
    const late = rec()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: door, Late: late } }))
    expect(new Set(referenced())).toEqual(new Set([door.key, late.key]))
  })

  it('forgets a file whose bytes are not in this browser, with its categories', async () => {
    // Settings restored from a backup made in another browser: the record came
    // back, the bytes did not.
    const door = rec()
    const here = rec()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      perCategory: { all: fileSoundId('Door'), turn: fileSoundId('Door') },
      customFiles: { Door: door, Here: here },
    }))
    vi.mocked(pruneSoundFiles).mockResolvedValueOnce([here.key])
    render('custom')
    await waitFor(() => expect(stored().customFiles).toEqual({ Here: here }))
    expect(stored().perCategory).toEqual({ all: 'chime' })
    expect(screen.queryByText('Door')).toBeNull()
  })

  it('never judges a just-saved record missing: its bytes may not be listed yet', async () => {
    const young = { bytes: 10, seconds: 1, key: newSoundFileKey() }
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Fresh: young } }))
    vi.mocked(pruneSoundFiles).mockResolvedValueOnce([])
    render('custom')
    await waitFor(() => expect(pruneSoundFiles).toHaveBeenCalledTimes(1))
    await act(async () => {})
    expect(stored().customFiles).toEqual({ Fresh: young })
  })

  it('keeps every record when storage cannot be read at all', async () => {
    const door = rec()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: door } }))
    vi.mocked(pruneSoundFiles).mockResolvedValueOnce(null)
    render('custom')
    await waitFor(() => expect(pruneSoundFiles).toHaveBeenCalledTimes(1))
    await act(async () => {})
    expect(stored().customFiles).toEqual({ Door: door })
  })

  it('drops a record whose key is dated far in the future and has no bytes', async () => {
    // A clock that ran ahead, or a hand-edited record: without this it would
    // be protected until that date and listed as a sound that plays nothing.
    const ghost = { bytes: 10, seconds: 1, key: newSoundFileKey(NOW + 2 * ORPHAN_GRACE_MS) }
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Ghost: ghost } }))
    vi.mocked(pruneSoundFiles).mockResolvedValueOnce([])
    render('custom')
    await waitFor(() => expect(stored().customFiles).toEqual({}))
  })

  it('keeps Save off until a file has passed its checks', async () => {
    render('custom')
    const save = () => fileCard().getByRole('button', { name: 'Save sound' })
    // Nothing picked yet, even with a name typed.
    fireEvent.change(fileCard().getByLabelText('Name'), { target: { value: 'Door' } })
    expect(save()).toBeDisabled()
    // While the file is being checked.
    let finish: (v: { bytes: number; seconds: number }) => void = () => {}
    vi.mocked(readAudioFile).mockReturnValueOnce(new Promise(r => { finish = r }))
    fireEvent.change(fileCard().getByLabelText('File'), { target: { files: [audio()] } })
    expect(save()).toBeDisabled()
    await act(async () => { finish({ bytes: 2000, seconds: 1 }) })
    expect(save()).toBeEnabled()
  })

  it('a refusal that is not about the file leaves the picker valid', async () => {
    const tones = Object.fromEntries(Array.from({ length: CUSTOM_TONE_LIMITS.maxSounds }, (_, i) => [`t${i}`, tone]))
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: tones }))
    render('custom')
    await pick(audio())
    await addFile('One more')
    expect(screen.getByText(`You can keep up to ${CUSTOM_TONE_LIMITS.maxSounds} custom sounds.`)).toBeInTheDocument()
    expect(fileCard().getByLabelText('File')).not.toHaveAttribute('aria-invalid')
  })

  it('a later pick wins over an earlier one whose check finishes last', async () => {
    render('custom')
    let finishFirst: (v: { bytes: number; seconds: number }) => void = () => {}
    vi.mocked(readAudioFile)
      .mockReturnValueOnce(new Promise(r => { finishFirst = r }))
      .mockResolvedValueOnce({ bytes: 3, seconds: 1 })
    const first = audio('first.mp3', 9)
    const second = audio('second.mp3', 3)
    fireEvent.change(fileCard().getByLabelText('File'), { target: { files: [first] } })
    await pick(second)
    await act(async () => { finishFirst({ bytes: 9, seconds: 1 }) })
    expect(fileCard().getByLabelText('Name')).toHaveValue('second')
    await addFile()
    expect(vi.mocked(putSoundFile).mock.calls[0][1]).toBe(second)
    expect(stored().customFiles.second.bytes).toBe(3)
  })

  it('deletes its own bytes when the record cannot be saved after they were written', async () => {
    render('custom')
    await pick(audio())
    const real = Storage.prototype.setItem
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, k: string, v: string) {
      if (k === STORAGE_KEY) throw new Error('QuotaExceededError')
      real.call(this, k, v)
    })
    try {
      await addFile('Door')
    } finally {
      setItem.mockRestore()
    }
    const [myKey] = vi.mocked(putSoundFile).mock.calls[0]
    expect(deleteSoundFile).toHaveBeenCalledWith(myKey)
    expect(screen.getByText(/Could not save the sound/)).toBeInTheDocument()
  })

  it('when another tab saves a TONE sound under the name while the bytes are written, deletes its own bytes', async () => {
    render('custom')
    await pick(audio())
    vi.mocked(putSoundFile).mockImplementationOnce(async () => {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { door: tone } }))
      return true
    })
    await addFile('Door')
    const [myKey] = vi.mocked(putSoundFile).mock.calls[0]
    expect(deleteSoundFile).toHaveBeenCalledWith(myKey)
    expect(stored().customFiles).toBeUndefined()
    expect(screen.getByText('That name is already used.')).toBeInTheDocument()
  })

  it('a double-clicked Undo restores once and says nothing false', async () => {
    const door = rec()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: door } }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete Door' }))
    let release: (b: Blob) => void = () => {}
    vi.mocked(getSoundFile).mockReturnValueOnce(new Promise(r => { release = r }))
    const undo = screen.getByRole('button', { name: 'Undo' })
    fireEvent.click(undo)
    expect(undo).toBeDisabled()
    fireEvent.click(undo)
    await act(async () => { release(new Blob(['x'])) })
    expect(getSoundFile).toHaveBeenCalledTimes(1)
    expect(stored().customFiles).toEqual({ Door: door })
    expect(screen.queryByText('That name is already used.')).toBeNull()
  })

  it('after a save, leads to Per-category sounds, where the sound is set to play', async () => {
    render('custom')
    await pick(audio('doorbell.mp3'))
    await addFile()
    const status = within(fileCard().getByRole('status'))
    expect(status.getByText('Saved doorbell.')).toBeInTheDocument()
    fireEvent.click(status.getByRole('button', { name: 'Choose where it plays in Per-category sounds' }))
    expect(await screen.findByText('Default (all categories)')).toBeInTheDocument()
  })

  it('drops "Saved <name>." once that sound is deleted from the list', async () => {
    render('custom')
    await pick(audio('doorbell.mp3'))
    await addFile()
    expect(fileCard().getByText('Saved doorbell.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Delete doorbell' }))
    expect(fileCard().queryByText('Saved doorbell.')).toBeNull()
    expect(screen.getByText('Deleted doorbell.')).toBeInTheDocument()
  })

  it('an Undo of the deleted sound does not bring "Saved <name>." back', async () => {
    render('custom')
    await pick(audio('doorbell.mp3'))
    await addFile()
    fireEvent.click(screen.getByRole('button', { name: 'Delete doorbell' }))
    await clickUndo()
    expect(Object.keys(stored().customFiles)).toEqual(['doorbell'])
    expect(fileCard().queryByText('Saved doorbell.')).toBeNull()
  })

  it('a slow Undo that answers after a second delete leaves that delete\'s notice alone', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: rec(), Beep: rec() } }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete Door' }))
    let release: (b: Blob | null) => void = () => {}
    vi.mocked(getSoundFile).mockReturnValueOnce(new Promise(r => { release = r }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete Beep' }))
    // Door's bytes turn out to be gone: that is about Door, not Beep.
    await act(async () => { release(null) })
    expect(screen.getByText('Deleted Beep.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled()
    expect(screen.queryByText(/no longer stored in this browser/)).toBeNull()
  })

  it('a slow Undo that answers after a failed second delete leaves that delete\'s error alone', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customFiles: { Door: rec(), Beep: rec() } }))
    render('custom')
    fireEvent.click(screen.getByRole('button', { name: 'Delete Door' }))
    let release: (b: Blob | null) => void = () => {}
    vi.mocked(getSoundFile).mockReturnValueOnce(new Promise(r => { release = r }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    const real = Storage.prototype.setItem
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, k: string, v: string) {
      if (k === STORAGE_KEY) throw new Error('QuotaExceededError')
      real.call(this, k, v)
    })
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Delete Beep' }))
      expect(screen.getByRole('alert').textContent).toContain('Could not delete the sound')
    } finally {
      setItem.mockRestore()
    }
    // Door's answer arrives after Beep's failed delete: it is about nothing on
    // screen now, so it must neither clear that error nor replace it.
    await act(async () => { release(new Blob(['x'])) })
    expect(screen.getByRole('alert').textContent).toContain('Could not delete the sound')
    expect(screen.queryByText(/Could not bring the sound back/)).toBeNull()
  })

  it('locks the Name field while a file is saving, and saves under the name as clicked', async () => {
    render('custom')
    await pick(audio('doorbell.mp3'))
    let finish: (v: boolean) => void = () => {}
    vi.mocked(putSoundFile).mockReturnValueOnce(new Promise(r => { finish = r }))
    fireEvent.click(fileCard().getByRole('button', { name: 'Save sound' }))
    expect(fileCard().getByLabelText('Name')).toBeDisabled()
    await act(async () => { finish(true) })
    expect(fileCard().getByLabelText('Name')).toBeEnabled()
    expect(Object.keys(stored().customFiles)).toEqual(['doorbell'])
  })

  it('the file picker always describes the limits, and the refusal too when there is one', async () => {
    render('custom')
    const describedBy = () => (fileCard().getByLabelText('File').getAttribute('aria-describedby') ?? '')
      .split(' ').map(id => document.getElementById(id)?.textContent ?? '')
    expect(describedBy()).toEqual([expect.stringContaining('up to 256 KB and 5 seconds')])
    vi.mocked(readAudioFile).mockResolvedValueOnce({ problem: 'file_length' })
    await pick(audio('long.mp3'))
    expect(describedBy()).toEqual([
      expect.stringContaining('up to 256 KB and 5 seconds'),
      'The sound must be 5 seconds or shorter.',
    ])
  })

  it('titles the two ways of making a sound and tags each saved one with its kind', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ customTones: { Beep: tone }, customFiles: { Door: rec() } }))
    render('custom')
    expect(screen.getByRole('region', { name: 'Your sounds' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Use an audio file' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Build from tones' })).toBeInTheDocument()
    const rows = within(screen.getByRole('list', { name: 'Your sounds' })).getAllByRole('listitem')
    expect(rows.map(r => r.textContent)).toEqual(['BeepTones', 'DoorFile'])
  })
})
