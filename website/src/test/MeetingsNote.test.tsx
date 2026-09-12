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

  it('answers "did it save?" in each of its three states', () => {
    const idle = setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    expect(screen.getByText(NOTE.saved)).toBeTruthy()
    idle.view.unmount()

    const busy = setup({ saving: true })
    expect(screen.getByText(NOTE.saving)).toBeTruthy()
    busy.view.unmount()

    const dirty = setup()
    dirty.type('typing')
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
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
