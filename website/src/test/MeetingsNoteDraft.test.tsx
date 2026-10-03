// The meeting-note DRAFT state machine.
//
// Everything worth testing here is about NOT LOSING what the user typed, since the
// note is the one thing in this app they cannot regenerate: the debounce must not
// swallow the last keystrokes, a stale save response must not revert the field, a
// refused save must stay dirty and retry, and closing the panel — which unmounts it
// — must leave the draft where reopening finds it. The last two are why the draft is
// a hook mounted with the meeting rather than state inside the panel.
//
// The draft lives exactly as long as the meeting view does. Leaving the view goes
// through `exit`, which awaits the save so the view can refuse to navigate on a
// refusal; the one loss this design accepts — the view unmounting anyway, on a
// refusal — is asserted here as accepted, not left to be rediscovered as a bug.

import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from 'vitest'
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { useState } from 'react'

import NoteSidebar from '../apps/meetings/components/NoteSidebar'
import {
  MeetingsApiError,
  NOTE_IMAGE_UPLOAD_TIMEOUT_MS,
  meetingsApi,
} from '../apps/meetings/api'
import { useNoteDraft } from '../apps/meetings/hooks/useNoteDraft'
import EN_CATALOG from '../i18n/locales/en.json'

const NOTE = EN_CATALOG.apps.meetings.note
const SESSION = EN_CATALOG.apps.meetings.session

type Stored = { alt: string; src: string }

interface Over {
  meetingId?: string
  serverContent?: string | undefined
  loadFailed?: boolean
  save?: (content: string) => Promise<unknown>
  uploadImage?: (file: File) => Promise<Stored>
}

/** A promise a test settles by hand, to hold a save or an upload in flight. */
function deferred<T>() {
  let resolve: (value: T) => void = () => {}
  let reject: (reason?: unknown) => void = () => {}
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

let nextTestMeetingId = 0

// Every body any test in this file handed to `save`, so the placeholder invariant
// can be asserted over the whole suite at the end rather than per case.
const everySavedBody: string[] = []
const PLACEHOLDER_CHARS = /[\uE000\uE001]/u

function recording(save: (content: string) => Promise<unknown>) {
  return vi.fn((content: string) => {
    everySavedBody.push(content)
    return save(content)
  })
}

function mount(over: Over = {}) {
  const save = recording(over.save ?? (async () => ({})))
  const uploadImage = over.uploadImage ?? vi.fn(async () => ({ alt: '10:23', src: 'images/abc.png' }))
  const initial = {
    meetingId: over.meetingId ?? `test-meeting-${++nextTestMeetingId}`,
    serverContent: 'serverContent' in over ? over.serverContent : '',
    loadFailed: over.loadFailed ?? false,
    save,
    uploadImage,
  }
  const view = renderHook((props: typeof initial) => useNoteDraft(props), { initialProps: initial })
  const update = (patch: Partial<typeof initial>) => {
    view.rerender({ ...initial, ...patch })
    Object.assign(initial, patch)
  }
  const type = (value: string) => { act(() => { view.result.current.change(value) }) }
  const flush = () => { act(() => { view.result.current.flush() }) }
  const settle = async () => { await act(async () => { await Promise.resolve() }) }
  return { view, save: save as ReturnType<typeof vi.fn>, uploadImage, update, type, flush, settle }
}

const pngFile = () => new File([new Uint8Array([0x89, 0x50])], 'shot.png', { type: 'image/png' })

/** Whether *promise* has settled yet, without awaiting it. */
async function settledState<T>(promise: Promise<T>) {
  const state = { settled: false, value: undefined as T | undefined }
  void promise.then(value => { state.settled = true; state.value = value })
  await act(async () => { await Promise.resolve() })
  return state
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

afterAll(() => {
  // No save argument anywhere in this file carries a placeholder. The gates that
  // hold one back (a flush waits for the upload; the exit flush strips it) are each
  // tested on their own; this is the invariant they exist for, asserted as such.
  expect(everySavedBody.length).toBeGreaterThan(0)
  expect(everySavedBody.filter(body => PLACEHOLDER_CHARS.test(body))).toEqual([])
})

describe('loading', () => {
  it('seeds the draft from the server value once it arrives', () => {
    const { view, update } = mount({ serverContent: undefined })
    expect(view.result.current.loaded).toBe(false)
    expect(view.result.current.draft).toBe('')
    update({ serverContent: 'ship on Friday' })
    expect(view.result.current.loaded).toBe(true)
    expect(view.result.current.draft).toBe('ship on Friday')
    expect(view.result.current.dirty).toBe(false)
  })

  it('treats an empty note as loaded, since that is every meeting\'s first state', () => {
    const { view } = mount({ serverContent: '' })
    expect(view.result.current.loaded).toBe(true)
  })

  it('refuses to save anything before the note has loaded', () => {
    // Typing into a field whose note has not arrived would be typing OVER it, so
    // the panel locks the field; this is the hook's half of that guard.
    const { view, save, type, flush } = mount({ serverContent: undefined })
    type('typed into what looked like an empty note')
    act(() => { vi.advanceTimersByTime(1000) })
    flush()
    view.unmount()
    expect(save).not.toHaveBeenCalled()
  })
})

describe('autosave', () => {
  it('does not save on every keystroke', () => {
    // One request per character would hammer the endpoint for a whole meeting.
    const { save, type } = mount()
    type('a')
    type('ab')
    expect(save).not.toHaveBeenCalled()
  })

  it('saves once the typing stops', () => {
    const { save, type } = mount()
    type('decision: ship')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith('decision: ship')
  })

  it('restarts the debounce while typing continues', () => {
    const { save, type } = mount()
    type('a')
    act(() => { vi.advanceTimersByTime(500) })
    type('ab')
    act(() => { vi.advanceTimersByTime(500) })
    expect(save).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(400) })
    expect(save).toHaveBeenCalledWith('ab')
  })

  it('flushes on demand, which is what blur, preview and closing the panel call', () => {
    const { save, type, flush } = mount()
    type('clicked away')
    flush()
    expect(save).toHaveBeenCalledWith('clicked away')
  })

  it('does not resend unchanged text', () => {
    const { save, flush } = mount({ serverContent: 'untouched' })
    flush()
    expect(save).not.toHaveBeenCalled()
  })

  it('does not resend a body that is already on the wire', () => {
    // Blur right after the debounce fired would otherwise PUT the same text twice.
    const pending = deferred<unknown>()
    const { save, type, flush } = mount({ save: vi.fn(() => pending.promise) })
    type('once')
    act(() => { vi.advanceTimersByTime(1000) })
    flush()
    expect(save).toHaveBeenCalledTimes(1)
  })

  it('saves an empty note, because clearing it is a real edit', () => {
    const { save, type } = mount({ serverContent: 'delete me' })
    type('')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenCalledWith('')
  })

  it('reports Saving while a save is in flight and clean once it lands', async () => {
    const pending = deferred<unknown>()
    const { view, type, settle } = mount({ save: vi.fn(() => pending.promise) })
    type('x')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(view.result.current.saving).toBe(true)
    expect(view.result.current.dirty).toBe(true)
    pending.resolve({})
    await settle()
    expect(view.result.current.saving).toBe(false)
    expect(view.result.current.dirty).toBe(false)
    expect(view.result.current.saveFailed).toBe(false)
  })

  it('flushes on unmount, the exit leaving the meeting takes', () => {
    const { view, save, type } = mount()
    type('half a thought')
    view.unmount()
    expect(save).toHaveBeenCalledWith('half a thought')
  })
})

describe('a stale response never replaces newer typing', () => {
  it('keeps the text typed while an older save was in flight', async () => {
    // The classic autosave bug: the response for "ab" lands while the user has
    // typed "abcd", and adopting it blindly rewinds their cursor and their text.
    const first = deferred<unknown>()
    const save = vi.fn(() => first.promise)
    const { view, type, update, settle } = mount({ save })
    type('ab')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenCalledWith('ab')
    type('abcd')
    // React Query seeds the cache in `onSuccess` BEFORE `mutateAsync` resolves, so the
    // echoed server value reaches the hook first — with the baseline still at the
    // pre-save text. Adopting it here is exactly the bug.
    update({ serverContent: 'ab' })
    expect(view.result.current.draft).toBe('abcd')
    first.resolve({})
    await settle()
    // The acknowledgement moved the baseline to "ab"; the draft is still newer.
    expect(view.result.current.draft).toBe('abcd')
    expect(view.result.current.dirty).toBe(true)
    // The pending debounce then saves what the user actually has.
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenLastCalledWith('abcd')
  })

  it('keeps a dirty draft when the server value changes underneath it', () => {
    // Another tab wrote while the user was mid-sentence. Theirs is newer than
    // anything the server can send, so it is kept and the next flush saves it —
    // last-write-wins, which is the store's documented semantics.
    const { view, type, update, save } = mount({ serverContent: 'original' })
    type('original, plus mine')
    update({ serverContent: 'theirs' })
    expect(view.result.current.draft).toBe('original, plus mine')
    expect(view.result.current.dirty).toBe(true)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenCalledWith('original, plus mine')
  })

  it('ignores an older acknowledgement that lands after a newer one', async () => {
    // Saves are serialized upstream, so this is the invariant holding even if they
    // were not: the baseline is monotonic in SEND order.
    const first = deferred<unknown>()
    const second = deferred<unknown>()
    const save = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const { view, type, flush, settle } = mount({ save })
    type('ab')
    flush()
    type('abcd')
    flush()
    expect(save).toHaveBeenCalledTimes(2)
    second.resolve({})
    await settle()
    expect(view.result.current.dirty).toBe(false)
    first.resolve({})
    await settle()
    // The older ack did not rewind the baseline to "ab".
    expect(view.result.current.dirty).toBe(false)
    expect(view.result.current.draft).toBe('abcd')
  })

  it('adopts a genuinely external change while the draft is clean', () => {
    // Another tab, or the first load landing after the panel opened.
    const { view, update } = mount({ serverContent: '' })
    update({ serverContent: 'written elsewhere' })
    expect(view.result.current.draft).toBe('written elsewhere')
    expect(view.result.current.dirty).toBe(false)
  })
})

describe('a refused save', () => {
  it('stays dirty and is retried by the next flush, instead of being stranded', async () => {
    // The baseline advances when a save LANDS, so a refusal leaves the draft dirty
    // and every exit retries it. The footer reads "Unsaved changes" AND there is a
    // way out of that state.
    const save = vi.fn()
      .mockImplementationOnce(() => Promise.reject(new Error('refused')))
      .mockImplementationOnce(() => Promise.resolve({}))
    const { view, type, flush, settle } = mount({ save })
    type('x')
    flush()
    await settle()
    expect(view.result.current.saveFailed).toBe(true)
    expect(view.result.current.dirty).toBe(true)
    expect(view.result.current.draft).toBe('x')
    flush()
    await settle()
    expect(save).toHaveBeenCalledTimes(2)
    expect(save).toHaveBeenLastCalledWith('x')
    expect(view.result.current.saveFailed).toBe(false)
    expect(view.result.current.dirty).toBe(false)
  })

  it('does not turn every flush on clean text into a write', () => {
    // The guard the retry must not trample: with no failure, a flush on untouched
    // text must stay silent, or every panel close writes the note again.
    const { save, flush } = mount({ serverContent: 'x' })
    flush()
    flush()
    expect(save).not.toHaveBeenCalled()
  })

  it('is lost when the meeting view itself unmounts on the refusal — the accepted residual', async () => {
    // Recorded on purpose. The draft lives exactly as long as the meeting view: the
    // view asks `exit` first and stays on a refusal (tested below), so this is the
    // path that unmounts WITHOUT asking — and nothing is retained past it. A
    // module-level copy once was, and every defect found since lived inside it.
    const meetingId = 'unmount-refusal'
    const first = mount({
      meetingId,
      serverContent: 'disk copy',
      save: () => Promise.reject(new Error('refused')),
    })
    first.type('the only unsaved copy')
    first.view.unmount()
    expect(first.save).toHaveBeenCalledWith('the only unsaved copy')
    await first.settle()

    const second = mount({ meetingId, serverContent: 'disk copy' })
    expect(second.view.result.current.draft).toBe('disk copy')
    expect(second.view.result.current.dirty).toBe(false)
    expect(second.view.result.current.saveFailed).toBe(false)
  })
})

// `exit` is what the meeting view calls BEFORE it lets the user leave. It answers
// once the save has landed or been refused, so the view can navigate on the first
// and stay on the second — where the draft still is, with the failure rendered.
describe('leaving the meeting view', () => {
  it('does not answer while the save is pending, then answers true once it lands', async () => {
    const pending = deferred<unknown>()
    const { view, save, type } = mount({ save: () => pending.promise })
    type('the last line')
    let exit!: Promise<boolean>
    act(() => { exit = view.result.current.exit() })
    expect(save).toHaveBeenCalledWith('the last line')
    expect((await settledState(exit)).settled).toBe(false)
    pending.resolve({})
    const state = await settledState(exit)
    expect(state.settled).toBe(true)
    expect(state.value).toBe(true)
    expect(view.result.current.dirty).toBe(false)
  })

  it('answers false on a refusal, keeping the draft dirty with the failure set', async () => {
    const { view, type } = mount({ save: () => Promise.reject(new Error('refused')) })
    type('not on disk')
    let exit!: Promise<boolean>
    act(() => { exit = view.result.current.exit() })
    const state = await settledState(exit)
    expect(state.settled).toBe(true)
    expect(state.value).toBe(false)
    expect(view.result.current.draft).toBe('not on disk')
    expect(view.result.current.dirty).toBe(true)
    expect(view.result.current.saveFailed).toBe(true)
  })

  it('answers true at once when there is nothing to save', async () => {
    const { view, save } = mount({ serverContent: 'already on disk' })
    let exit!: Promise<boolean>
    act(() => { exit = view.result.current.exit() })
    expect(save).not.toHaveBeenCalled()
    expect((await settledState(exit)).value).toBe(true)
  })

  it('awaits a save already on the wire instead of sending it twice', async () => {
    const pending = deferred<unknown>()
    const { view, save, type, flush } = mount({ save: () => pending.promise })
    type('sent by the blur')
    flush()
    let exit!: Promise<boolean>
    act(() => { exit = view.result.current.exit() })
    expect(save).toHaveBeenCalledTimes(1)
    expect((await settledState(exit)).settled).toBe(false)
    pending.resolve({})
    expect((await settledState(exit)).value).toBe(true)
  })

  it('saves the text with an unresolved placeholder removed, rather than nothing', async () => {
    // An ordinary flush waits for the upload, because the hook will still be here
    // when it lands. On the way out there is no later, so the words are saved and
    // the token is not — and it stays in the FIELD, so if the refusal keeps the user
    // here the upload can still land in its place.
    const upload = deferred<Stored>()
    const { view, save, type, flush } = mount({ uploadImage: vi.fn(() => upload.promise) })
    type('before after')
    act(() => { view.result.current.pasteImage(pngFile(), 6) })
    flush()
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).not.toHaveBeenCalled()

    let exit!: Promise<boolean>
    act(() => { exit = view.result.current.exit() })
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith('before after')
    expect((await settledState(exit)).value).toBe(true)
    expect(view.result.current.draft).toMatch(/\uE000\d+\uE001/u)
    expect(view.result.current.uploading).toBe(true)
  })

  it('does the same on unmount, the exit that cannot wait', () => {
    const upload = deferred<Stored>()
    const { view, save, type } = mount({ uploadImage: vi.fn(() => upload.promise) })
    type('before after')
    act(() => { view.result.current.pasteImage(pngFile(), 6) })
    view.unmount()
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith('before after')
  })
})

describe('a note that failed to load is never autosaved over', () => {
  // The field is empty because the GET failed, NOT because the note is empty.
  // Typing into it and letting the debounce fire would replace a note that exists
  // on disk with whatever the user just typed.

  it('does not save on the debounce, on flush, or on unmount', () => {
    const { view, save, type, flush } = mount({ serverContent: undefined, loadFailed: true })
    type('typed into what looked like an empty note')
    act(() => { vi.advanceTimersByTime(1000) })
    flush()
    view.unmount()
    expect(save).not.toHaveBeenCalled()
  })

  it('takes no paste either', () => {
    const { view, uploadImage } = mount({ serverContent: undefined, loadFailed: true })
    act(() => { view.result.current.pasteImage(pngFile(), 0) })
    expect(uploadImage).not.toHaveBeenCalled()
    expect(view.result.current.uploading).toBe(false)
  })
})

describe('pasting an image', () => {
  it('uploads it and inserts the markdown at the caret, then saves', async () => {
    const { view, save, uploadImage, type, settle } = mount()
    type('one two')
    act(() => { view.result.current.pasteImage(pngFile(), 3) })
    expect(uploadImage).toHaveBeenCalledTimes(1)
    expect(view.result.current.uploading).toBe(true)
    await settle()
    expect(view.result.current.uploading).toBe(false)
    expect(view.result.current.draft).toBe('one\n![10:23](images/abc.png)\n two')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenLastCalledWith('one\n![10:23](images/abc.png)\n two')
  })

  it('inserts into the text as it is when the upload RESOLVES, not as it was', async () => {
    // The user kept typing while the upload ran. A real textarea change carries
    // the private placeholder already present in its value.
    const pending = deferred<Stored>()
    const { view, type, settle } = mount({ uploadImage: vi.fn(() => pending.promise) })
    type('one')
    act(() => { view.result.current.pasteImage(pngFile(), 3) })
    type(`${view.result.current.draft} two`)
    pending.resolve({ alt: '0:05', src: 'images/b.png' })
    await settle()
    expect(view.result.current.draft).toBe('one\n![0:05](images/b.png)\n two')
  })

  it('keeps concurrent pastes at their own placeholders when uploads resolve in reverse', async () => {
    const first = deferred<Stored>()
    const second = deferred<Stored>()
    const uploadImage = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const { view, save, type, settle } = mount({ uploadImage })
    type('left middle right')
    act(() => { view.result.current.pasteImage(pngFile(), 4) })
    const beforeRight = view.result.current.draft.indexOf(' right')
    act(() => { view.result.current.pasteImage(pngFile(), beforeRight) })

    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).not.toHaveBeenCalled()
    second.resolve({ alt: 'second', src: 'images/second.png' })
    await settle()
    first.resolve({ alt: 'first', src: 'images/first.png' })
    await settle()

    expect(view.result.current.draft).toBe(
      'left\n![first](images/first.png)\n middle\n![second](images/second.png)\n right',
    )
    expect(view.result.current.draft).not.toMatch(/[\uE000-\uF8FF]/u)
  })

  it('leaves the note untouched and names the reason when the upload is refused', async () => {
    const { view, save, type, settle } = mount({
      uploadImage: vi.fn(() => Promise.reject(new MeetingsApiError('too large', 413, 'image_too_large'))),
    })
    type('untouched')
    act(() => { vi.advanceTimersByTime(1000) })
    save.mockClear()
    act(() => { view.result.current.pasteImage(pngFile(), 9) })
    await settle()
    expect(view.result.current.draft).toBe('untouched')
    expect(view.result.current.uploadError).toBe('tooLarge')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).not.toHaveBeenCalled()
  })

  it('reports any other refusal as a format problem', async () => {
    const { view, settle } = mount({
      uploadImage: vi.fn(() => Promise.reject(new MeetingsApiError('not an image', 400))),
    })
    act(() => { view.result.current.pasteImage(pngFile(), 0) })
    await settle()
    expect(view.result.current.uploadError).toBe('failed')
  })

  it('clears the refusal on the next edit, so it does not outlive its moment', async () => {
    const { view, type, settle } = mount({
      uploadImage: vi.fn(() => Promise.reject(new MeetingsApiError('nope', 400))),
    })
    act(() => { view.result.current.pasteImage(pngFile(), 0) })
    await settle()
    expect(view.result.current.uploadError).toBe('failed')
    type('moved on')
    expect(view.result.current.uploadError).toBeNull()
  })

  it('resumes autosave when the user deleted the placeholder before the upload finished', async () => {
    // Every debounce is held while an upload is pending, so the completion is what
    // must re-arm it — even though its token is gone and it has nothing to insert.
    const pending = deferred<Stored>()
    const { view, save, type, settle } = mount({ uploadImage: vi.fn(() => pending.promise) })
    type('draft')
    act(() => { view.result.current.pasteImage(pngFile(), 5) })
    type('rewritten entirely')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).not.toHaveBeenCalled()
    pending.reject(new MeetingsApiError('stalled', 0))
    await settle()
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenLastCalledWith('rewritten entirely')
    await settle()
    expect(view.result.current.dirty).toBe(false)
  })

  it('never saves a lone delimiter left by a partly edited placeholder', async () => {
    const pending = deferred<Stored>()
    const { view, save, type, settle } = mount({ uploadImage: vi.fn(() => pending.promise) })
    type('ab')
    act(() => { view.result.current.pasteImage(pngFile(), 1) })
    // Delete the token's closing delimiter, as a Backspace over it would.
    type(view.result.current.draft.replace('\uE001', ''))
    let exit: Promise<boolean> = Promise.resolve(false)
    act(() => { exit = view.result.current.exit() })
    await settle()
    expect(await exit).toBe(true)
    expect(save).toHaveBeenCalledTimes(1)
    expect(save.mock.calls[0][0]).not.toMatch(/[\uE000\uE001]/u)
    pending.resolve({ alt: 'late', src: 'images/late.png' })
    await settle()
    act(() => { vi.advanceTimersByTime(1000) })
    for (const [body] of save.mock.calls) expect(body).not.toMatch(/[\uE000\uE001]/u)
  })
})

describe('the image upload deadline', () => {
  it('aborts a stalled request so the hook can remove its placeholder and resume saves', async () => {
    let signal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      signal = init.signal ?? undefined
      signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })))

    const upload = meetingsApi.uploadNoteImage('stalled', pngFile())
    const rejection = expect(upload).rejects.toMatchObject({ name: 'AbortError' })
    vi.advanceTimersByTime(NOTE_IMAGE_UPLOAD_TIMEOUT_MS)
    await rejection
    expect(signal?.aborted).toBe(true)
  })
})

describe('a different meeting is a different note', () => {
  it('resets the draft when the meeting id changes', () => {
    const { view, type, update, save } = mount({ serverContent: 'first meeting' })
    type('first meeting, edited')
    update({ meetingId: 'm2', serverContent: undefined })
    expect(view.result.current.draft).toBe('')
    expect(view.result.current.loaded).toBe(false)
    act(() => { vi.advanceTimersByTime(1000) })
    // The pending debounce from the old meeting was dropped, not saved into the new one.
    expect(save).not.toHaveBeenCalled()
  })
})

// The two losses that motivated moving the draft out of the panel, driven through
// the real panel: the hook stays mounted (as the meeting view keeps it) while the
// panel is unmounted and remounted the way closing and reopening does.
describe('the draft outlives the panel', () => {
  function Harness({ meetingId, save, uploadImage }: { meetingId: string; save: (c: string) => Promise<unknown>; uploadImage: (f: File) => Promise<Stored> }) {
    const [open, setOpen] = useState(true)
    const draft = useNoteDraft({
      meetingId,
      serverContent: '',
      loadFailed: false,
      save: content => { everySavedBody.push(content); return save(content) },
      uploadImage,
    })
    return (
      <div>
        <button onClick={() => setOpen(true)}>reopen</button>
        <output data-testid="hook">{JSON.stringify({ dirty: draft.dirty, saveFailed: draft.saveFailed })}</output>
        {open ? (
          <NoteSidebar
            draft={draft.draft}
            dirty={draft.dirty}
            loaded={draft.loaded}
            updatedAt=""
            path="/data/notes/m1/note.md"
            saving={draft.saving}
            saveFailed={draft.saveFailed}
            loadFailed={false}
            uploading={draft.uploading}
            uploadError={draft.uploadError}
            onChange={draft.change}
            onFlush={draft.flush}
            onPasteImage={draft.pasteImage}
            onClose={() => setOpen(false)}
          />
        ) : null}
      </div>
    )
  }

  const field = () => screen.getByLabelText(NOTE.editorLabel) as HTMLTextAreaElement

  it('keeps a draft whose close-time save was refused, and shows it on reopen', async () => {
    // Before: the unmount flush sent one PUT and destroyed the component state, so a
    // refusal lost the only copy with a transient toast as its epitaph.
    const save = vi.fn()
      .mockImplementationOnce(() => Promise.reject(new Error('refused')))
      .mockImplementationOnce(() => Promise.resolve({}))
    render(<Harness meetingId="panel-refusal" save={save} uploadImage={vi.fn()} />)
    act(() => { fireEvent.change(field(), { target: { value: 'the only copy' } }) })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.close)) })
    expect(save).toHaveBeenCalledWith('the only copy')
    await act(async () => { await Promise.resolve() })
    // The panel is gone; the HOOK still holds the text, dirty, with the refusal.
    expect(screen.queryByLabelText(NOTE.editorLabel)).toBeNull()
    expect(JSON.parse(screen.getByTestId('hook').textContent ?? '{}')).toEqual({ dirty: true, saveFailed: true })

    act(() => { fireEvent.click(screen.getByText('reopen')) })
    expect(field().value).toBe('the only copy')
    expect(screen.getByRole('alert').textContent).toContain(SESSION.noteSaveFailed)
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
    // And the next exit retries it.
    act(() => { fireEvent.blur(field()) })
    expect(save).toHaveBeenCalledTimes(2)
  })

  it('finishes a paste whose upload outlives the panel, and saves the image reference', async () => {
    // Before: the unmount flush saved the pre-image draft, the upload then resolved
    // into an unmounted component, and the stored image was never referenced.
    const pending = deferred<Stored>()
    const save = vi.fn(async () => ({}))
    render(<Harness meetingId="panel-late-image" save={save} uploadImage={vi.fn(() => pending.promise)} />)
    act(() => { fireEvent.change(field(), { target: { value: 'before the shot' } }) })
    act(() => { field().setSelectionRange(15, 15) })
    act(() => {
      fireEvent.paste(field(), {
        clipboardData: { types: ['Files'], items: [{ kind: 'file', getAsFile: () => pngFile() }] },
      })
    })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.close)) })
    // The unresolved private placeholder must never reach disk.
    expect(save).not.toHaveBeenCalled()

    pending.resolve({ alt: '12:40', src: 'images/late.png' })
    await act(async () => { await Promise.resolve() })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(save).toHaveBeenLastCalledWith('before the shot\n![12:40](images/late.png)')
  })
})
