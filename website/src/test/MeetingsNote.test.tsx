// The meeting-note panel.
//
// Everything worth testing here is about NOT LOSING what the user typed, since the
// note is the one thing in this app they cannot regenerate: the debounce must not
// swallow the last keystrokes, an in-flight save must not revert the field, and
// closing the panel must flush rather than discard.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import NoteSidebar, {
  imageSnippet,
  insertBlock,
} from '../apps/meetings/components/NoteSidebar'
import EN_CATALOG from '../i18n/locales/en.json'

const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const StoreSource = readFileSync(
  '../src/kiro_crew/apps/builtins/meetings/backend/store.py', 'utf-8',
)

const NOTE = EN_CATALOG.apps.meetings.note
const SESSION = EN_CATALOG.apps.meetings.session

function setup(over: Partial<Parameters<typeof NoteSidebar>[0]> = {}) {
  const onSave = vi.fn()
  const onClose = vi.fn()
  const onUploadImage = vi.fn(async () => ({ alt: '10:23', src: 'images/abc.png' }))
  const view = render(
    <NoteSidebar
      content=""
      updatedAt=""
      path="/data/meetings/m1/_note.md"
      saving={false}
      saveFailed={false}
      loadError=""
      onUploadImage={onUploadImage}
      onSave={onSave}
      onClose={onClose}
      {...over}
    />,
  )
  // The editor's own label, distinct from the region's — see NoteSidebar.
  const field = screen.getByLabelText(NOTE.editorLabel) as HTMLTextAreaElement
  // `fireEvent.change`, not a hand-dispatched input event: React's internal value
  // tracker suppresses onChange when `.value` is assigned directly, so the naive
  // version silently never reaches the component.
  const type = (value: string) => {
    act(() => { fireEvent.change(field, { target: { value } }) })
  }
  const clipboard = (opts: { file?: File | null; text?: boolean }) => {
    const items: Array<{ kind: string; getAsFile: () => File | null }> = []
    if (opts.file !== undefined) {
      items.push({ kind: 'file', getAsFile: () => opts.file ?? null })
    }
    const types = opts.text ? ['text/plain'] : opts.file !== undefined ? ['Files'] : []
    return { types, items }
  }

  /**
   * Dispatch a paste WITHOUT waiting for the upload, so a test can assert on the
   * in-flight state. Returns `fireEvent`'s own verdict, which is `false` exactly
   * when the handler called `preventDefault` — React owns the synthetic event, so
   * passing a spy in the init object would not be consulted.
   */
  const pasteSync = (opts: { file?: File | null; text?: boolean } = {}) => {
    let notCancelled = true
    act(() => {
      notCancelled = fireEvent.paste(field, { clipboardData: clipboard(opts) })
    })
    return { defaultPrevented: !notCancelled }
  }

  /** Paste and let the upload settle. */
  const paste = async (opts: { file?: File | null; text?: boolean } = {}) => {
    const result = pasteSync(opts)
    await act(async () => { await Promise.resolve() })
    return result
  }

  return { view, onSave, onClose, onUploadImage, field, type, paste, pasteSync }
}

const pngFile = () => new File([new Uint8Array([0x89, 0x50])], 'shot.png', { type: 'image/png' })

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('NoteSidebar', () => {
  it('seeds the field from the server value', () => {
    const { field } = setup({ content: 'ship on Friday' })
    expect(field.value).toBe('ship on Friday')
  })

  it('does not save on every keystroke', () => {
    // One request per character would hammer the endpoint for a whole meeting.
    const { onSave, type } = setup()
    type('a')
    type('ab')
    expect(onSave).not.toHaveBeenCalled()
  })

  it('saves once the typing stops', () => {
    const { onSave, type } = setup()
    type('decision: ship')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledTimes(1)
    expect(onSave).toHaveBeenCalledWith('decision: ship')
  })

  it('restarts the debounce while typing continues', () => {
    const { onSave, type } = setup()
    type('a')
    act(() => { vi.advanceTimersByTime(500) })
    type('ab')
    act(() => { vi.advanceTimersByTime(500) })
    expect(onSave).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(400) })
    expect(onSave).toHaveBeenCalledWith('ab')
  })

  it('flushes on unmount, so closing the panel cannot drop the last words', () => {
    // The failure this exists for: type, close, lose the sentence.
    const { view, onSave, type } = setup()
    type('half a thought')
    view.unmount()
    expect(onSave).toHaveBeenCalledWith('half a thought')
  })

  it('flushes on blur', () => {
    const { onSave, field, type } = setup()
    type('clicked away')
    act(() => { fireEvent.blur(field) })
    expect(onSave).toHaveBeenCalledWith('clicked away')
  })

  it('does not re-save unchanged text on unmount', () => {
    const { view, onSave } = setup({ content: 'untouched' })
    view.unmount()
    expect(onSave).not.toHaveBeenCalled()
  })

  it('saves an empty note, because clearing it is a real edit', () => {
    const { onSave, type } = setup({ content: 'delete me' })
    type('')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledWith('')
  })

  it('does NOT revert the field when the in-flight save echoes back', () => {
    // The classic autosave bug: the response for "ab" lands while the user has
    // typed "abcd", and adopting it blindly rewinds their cursor and their text.
    const { view, field, type, onSave } = setup()
    type('ab')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledWith('ab')
    type('abcd')
    view.rerender(
      <NoteSidebar
        content="ab"
        updatedAt="2026-08-04T00:00:00Z"
        saving={false}
        onSave={onSave}
        onClose={() => {}}
      />,
    )
    expect(field.value).toBe('abcd')
  })

  it('adopts a genuinely external change', () => {
    // Another tab, or the first load landing after the panel opened.
    const { view, field, onSave } = setup({ content: '' })
    view.rerender(
      <NoteSidebar
        content="written elsewhere"
        updatedAt="2026-08-04T00:00:00Z"
        saving={false}
        onSave={onSave}
        onClose={() => {}}
      />,
    )
    expect(field.value).toBe('written elsewhere')
  })

  it('answers "did it save?" in each of its four states', () => {
    const idle = setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    expect(screen.getByText(NOTE.saved)).toBeTruthy()
    idle.view.unmount()

    const busy = setup({ saving: true })
    expect(screen.getByText(NOTE.saving)).toBeTruthy()
    busy.view.unmount()

    const dirty = setup()
    dirty.type('typing')
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
    dirty.view.unmount()

    // The state that used to lie. `flush` advances `savedRef` before the PUT
    // resolves, so on failure the text compares EQUAL to "what the server has" and
    // the footer showed "Saved" over a note that was never stored — a user who
    // closes the panel on that reading loses it.
    const failed = setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z', saveFailed: true })
    expect(screen.queryByText(NOTE.saved)).toBeNull()
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
    failed.view.unmount()
  })

  it('retries a refused save instead of stranding it', () => {
    // The loss this closes: `savedRef` advances when a save is SENT, so after a
    // refusal it equals the draft and `flush` used to return early -- the footer read
    // "Unsaved changes" while nothing ever resent, and closing the panel dropped the
    // note. Blur is one exit from that state.
    const { field, onSave } = setup({ content: 'x', saveFailed: true })
    expect(onSave).not.toHaveBeenCalled()
    fireEvent.blur(field)
    expect(onSave).toHaveBeenCalledWith('x')
  })

  it('retries a refused save when the panel closes', () => {
    // The exit that actually loses the note, so it is pinned separately.
    const { view, onSave } = setup({ content: 'x', saveFailed: true })
    view.unmount()
    expect(onSave).toHaveBeenCalledWith('x')
  })

  it('does not resend an unchanged note that saved cleanly', () => {
    // The guard the retry must not trample: with no failure, an unmount on untouched
    // text must stay silent, or every panel close writes the note again.
    const { view, onSave } = setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    view.unmount()
    expect(onSave).not.toHaveBeenCalled()
  })
})

// The panel's own error surface. A toast fades; "your note is not on disk" is a
// state, and the rule the dashboard holds itself to (`errors-use-error-notice`) is
// that a state like that is rendered where the user is working.
describe('a failure the user can act on is shown in the panel', () => {
  it('renders a notice when the save was refused', () => {
    setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z', saveFailed: true })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain(SESSION.noteSaveFailed)
  })

  it('renders the server sentence when the note could not be loaded', () => {
    // A failed GET leaves the field empty, which reads as "no note yet" -- so the
    // panel has to say otherwise before the user types over a note that exists.
    setup({ loadError: 'note directory is not readable' })
    expect(screen.getByRole('alert').textContent).toContain('note directory is not readable')
  })

  it('shows the save refusal when both failed, because that is the one that loses text', () => {
    setup({ content: 'x', saveFailed: true, loadError: 'note directory is not readable' })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain(SESSION.noteSaveFailed)
    expect(alert.textContent).not.toContain('note directory is not readable')
  })

  it('stays quiet when nothing failed', () => {
    setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('offers NO agent hand-off, because the draft is the only copy of the note', () => {
    // The hand-off navigates to a chat, which unmounts this panel and destroys the
    // draft -- and the notice is on screen precisely because the draft is not on
    // disk. This is the reason the prop is opt-in, asserted rather than trusted.
    setup({ content: 'x', saveFailed: true })
    // The hand-off is the only control ErrorNotice renders here, so its absence is
    // the absence of any button inside the alert.
    expect(screen.getByRole('alert').querySelector('button')).toBeNull()
  })
})

describe('insertBlock', () => {
  it('puts the snippet on its own line', () => {
    // A pasted image is block content; dropping one mid-sentence would split it.
    // Caret 6 splits 'before' | ' after'.
    expect(insertBlock('before after', 6, 'IMG')).toBe('before\nIMG\n after')
  })

  it('does not pile up blank lines when a boundary already has one', () => {
    expect(insertBlock('a\n', 2, 'IMG')).toBe('a\nIMG')
    expect(insertBlock('', 0, 'IMG')).toBe('IMG')
    expect(insertBlock('\nb', 0, 'IMG')).toBe('IMG\nb')
  })

  it('clamps a caret outside the text', () => {
    expect(insertBlock('abc', 99, 'IMG')).toBe('abc\nIMG')
    expect(insertBlock('abc', -5, 'IMG')).toBe('IMG\nabc')
  })
})

describe('imageSnippet', () => {
  it('uses the elapsed time as alt text', () => {
    // Which is what lets a reader line the image up against the transcript.
    expect(imageSnippet('10:23', 'images/a.png')).toBe('![10:23](images/a.png)')
  })

  it('tolerates no elapsed time', () => {
    // A meeting that has not started yet — honest empty alt beats an invented time.
    expect(imageSnippet('', 'images/a.png')).toBe('![](images/a.png)')
  })
})

describe('pasting an image', () => {
  it('uploads it and inserts the markdown at the caret', async () => {
    const { onUploadImage, onSave, field, type, paste } = setup()
    type('one two')
    act(() => { field.setSelectionRange(3, 3) })
    await paste({ file: pngFile() })

    expect(onUploadImage).toHaveBeenCalledTimes(1)
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledWith('one\n![10:23](images/abc.png)\n two')
  })

  it('ignores a paste that also carries text', async () => {
    // Office on macOS puts an image on the clipboard ALONGSIDE the copied text;
    // treating that as an image paste silently swallows what the user copied.
    const { onUploadImage, paste } = setup()
    const result = await paste({ file: pngFile(), text: true })
    expect(onUploadImage).not.toHaveBeenCalled()
    // And the default is left alone, so the text still pastes.
    expect(result.defaultPrevented).toBe(false)
  })

  it('leaves a plain text paste alone', async () => {
    const { onUploadImage, paste } = setup()
    const result = await paste({ text: true })
    expect(onUploadImage).not.toHaveBeenCalled()
    expect(result.defaultPrevented).toBe(false)
  })

  it('prevents the default only for a paste it handles', async () => {
    const { paste } = setup()
    const result = await paste({ file: pngFile() })
    expect(result.defaultPrevented).toBe(true)
  })

  it('leaves the note untouched when the upload fails', async () => {
    // A rejected image must not corrupt the note; the toast is the session hook's job.
    const onUploadImage = vi.fn(async () => null)
    const { onSave, type, paste } = setup({ onUploadImage })
    type('untouched')
    act(() => { vi.advanceTimersByTime(1000) })
    onSave.mockClear()

    await paste({ file: pngFile() })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).not.toHaveBeenCalled()
  })

  it('reports the upload while it is in flight', async () => {
    // The user is waiting on this one, so it takes precedence over the save status.
    let release: (v: { alt: string; src: string } | null) => void = () => {}
    const onUploadImage = vi.fn(
      () => new Promise<{ alt: string; src: string } | null>(r => { release = r }),
    )
    const { pasteSync } = setup({ onUploadImage })

    // Dispatched but NOT awaited: the upload promise is still pending here.
    pasteSync({ file: pngFile() })
    expect(screen.getByText(NOTE.uploading)).toBeTruthy()

    await act(async () => {
      release({ alt: '0:05', src: 'images/b.png' })
      await Promise.resolve()
    })
    expect(screen.queryByText(NOTE.uploading)).toBeNull()
  })
})

describe('preview', () => {
  it('swaps the editor for rendered markdown', async () => {
    const { view, type } = setup()
    type('# Heading')
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.preview)) })
    expect(view.queryByLabelText(NOTE.editorLabel)).toBeNull()
    expect(screen.getByText('Heading')).toBeTruthy()
  })

  it('flushes before switching, so previewing cannot lose the text', () => {
    const { onSave, type } = setup()
    type('not yet saved')
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.preview)) })
    expect(onSave).toHaveBeenCalledWith('not yet saved')
  })

  it('goes back to the editor', () => {
    const { view } = setup({ content: 'x' })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.preview)) })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.edit)) })
    expect(view.getByLabelText(NOTE.editorLabel)).toBeTruthy()
  })
})

describe('note wiring', () => {
  it('is not polled', () => {
    // The textarea is the authoritative copy; refetching under the user is how an
    // autosaving editor loses a sentence.
    const block = SessionSource.match(/const noteQuery = useQuery\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('refetchInterval: false')
    expect(block![0]).toContain('refetchOnWindowFocus: false')
    expect(block![0]).toContain('noteOpen')
  })

  it('hands the GET failure out so the panel can render it', () => {
    // Without this the only trace of a failed load is an empty textarea.
    expect(SessionSource).toContain('loadError: noteQuery.isError')
  })

  it('serializes saves, so an older response cannot seed the cache last', () => {
    // The bug without this: two debounced saves are in flight, the OLDER response
    // completes last, `onSuccess` seeds the cache with its stale content, and the
    // panel's adopt effect (`content !== savedRef.current`) puts the older text back
    // in the field. Completions in SEND order self-heal, so serializing is the whole
    // fix. Asserted on the source because the ordering lives in React Query's
    // mutation scope rather than in code this suite can drive: the hook needs a live
    // QueryClient and a real fetch boundary, and a test that stubbed both would be
    // asserting the stub. `scope` is the same mechanism ChatPanel uses on the
    // hidden-models save, for the same reason.
    const block = SessionSource.match(/const noteMutation = useMutation\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('scope: {')
  })

  it('seeds the cache from the save response instead of invalidating', () => {
    // An invalidate would refetch and hand the editor a value mid-keystroke.
    const block = SessionSource.match(/const noteMutation = useMutation\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('setQueryData')
    expect(block![0]).not.toContain('invalidateQueries')
  })
})

describe('the note filename cannot be owned by an agent', () => {
  it('is documented at the store, not just in the constant', () => {
    // Agent outputs share the meeting directory and are named from the agent id.
    // The leading underscore is what makes this path unreachable by that
    // derivation; the Python side pins it, and this is the frontend-side reminder
    // that the filename is a security property rather than a style choice.
    expect(StoreSource).toContain('k.NOTE_FILE')
    expect(StoreSource).toContain('un-ownable by any agent')
  })
})

describe('a note that failed to load is never autosaved over', () => {
  // The whole point of the load notice: the field is empty because the GET failed,
  // NOT because the note is empty. Typing into it and letting the debounce fire
  // would replace a note that exists on disk with whatever the user just typed,
  // and the note is the one thing in this app they cannot regenerate.

  it('does not save when the debounce fires', () => {
    const { onSave, type } = setup({ loadError: 'the note could not be read' })
    type('typed into what looked like an empty note')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).not.toHaveBeenCalled()
  })

  it('does not save on blur', () => {
    const { onSave, field, type } = setup({ loadError: 'the note could not be read' })
    type('clicked away')
    act(() => { fireEvent.blur(field) })
    expect(onSave).not.toHaveBeenCalled()
  })

  it('does not save on unmount, the path that closing the panel takes', () => {
    const { view, onSave, type } = setup({ loadError: 'the note could not be read' })
    type('half a thought')
    view.unmount()
    expect(onSave).not.toHaveBeenCalled()
  })

  it('makes the field read-only, so the block is visible before anything is typed', () => {
    // Suppressing the save alone would let someone type a paragraph and watch the
    // footer never reach "Saved". The field says up front that it is not editable.
    const { field } = setup({ loadError: 'the note could not be read' })
    expect(field.readOnly).toBe(true)
  })

  it('still autosaves, and stays editable, once the note loaded', () => {
    // The guard must key off the load failure and nothing else. One render per
    // test: a second `setup()` in the same test renders a second panel, and the
    // field lookup then matches both.
    const { onSave, type, field } = setup()
    expect(field.readOnly).toBe(false)
    type('decision: ship')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledWith('decision: ship')
  })
})
