// The meeting-note panel.
//
// The panel is presentation: the draft, the debounce and the pending paste live in
// `useNoteDraft` (see MeetingsNoteDraft.test.tsx). What is worth testing HERE is
// that every state the hook can be in is shown honestly — a field that cannot be
// edited says so, a failure the user can act on is rendered in place, and each
// exit (blur, preview, close) hands the flush back to the hook.

import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import NoteSidebar, {
  imageSnippet,
  insertBlock,
} from '../apps/meetings/components/NoteSidebar'
import EN_CATALOG from '../i18n/locales/en.json'

const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const ViewSource = readFileSync('src/apps/meetings/MeetingView.tsx', 'utf-8')
const ApiSource = readFileSync('src/apps/meetings/api.ts', 'utf-8')
const StoreSource = readFileSync(
  '../src/kiro_crew/apps/builtins/meetings/backend/store.py', 'utf-8',
)
const SecuritySource = readFileSync('../src/kiro_crew/security/paths.py', 'utf-8')

const NOTE = EN_CATALOG.apps.meetings.note
const SESSION = EN_CATALOG.apps.meetings.session

type Props = Parameters<typeof NoteSidebar>[0]

function setup(over: Partial<Props> = {}) {
  const onChange = vi.fn()
  const onFlush = vi.fn()
  const onPasteImage = vi.fn()
  const onClose = vi.fn()
  const view = render(
    <NoteSidebar
      draft=""
      dirty={false}
      loaded
      updatedAt=""
      path="/data/notes/m1/note.md"
      saving={false}
      saveFailed={false}
      loadFailed={false}
      uploading={false}
      uploadError={null}
      onChange={onChange}
      onFlush={onFlush}
      onPasteImage={onPasteImage}
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
   * Dispatch a paste. Returns `fireEvent`'s own verdict, which is `false` exactly
   * when the handler called `preventDefault` — React owns the synthetic event, so
   * passing a spy in the init object would not be consulted.
   */
  const paste = (opts: { file?: File | null; text?: boolean } = {}) => {
    let notCancelled = true
    act(() => {
      notCancelled = fireEvent.paste(field, { clipboardData: clipboard(opts) })
    })
    return { defaultPrevented: !notCancelled }
  }
  return {
    view,
    onChange,
    onFlush,
    onPasteImage,
    onClose,
    field,
    type,
    paste,
  }
}

const pngFile = () => new File([new Uint8Array([0x89, 0x50])], 'shot.png', { type: 'image/png' })

afterEach(() => {
  vi.restoreAllMocks()
})

describe('NoteSidebar', () => {
  it('renders the draft the hook holds', () => {
    const { field } = setup({ draft: 'ship on Friday' })
    expect(field.value).toBe('ship on Friday')
  })

  it('hands every keystroke to the hook, and never saves on its own', () => {
    const { onChange, onFlush, type } = setup()
    type('a')
    type('ab')
    expect(onChange).toHaveBeenCalledTimes(2)
    expect(onChange).toHaveBeenLastCalledWith('ab')
    expect(onFlush).not.toHaveBeenCalled()
  })

  it('flushes on blur', () => {
    const { onFlush, field } = setup({ draft: 'clicked away', dirty: true })
    act(() => { fireEvent.blur(field) })
    expect(onFlush).toHaveBeenCalledTimes(1)
  })

  it('flushes on unmount, so closing the panel cannot leave the last words unsent', () => {
    const { view, onFlush } = setup({ draft: 'half a thought', dirty: true })
    view.unmount()
    expect(onFlush).toHaveBeenCalledTimes(1)
  })

  it('flushes before switching to preview, so previewing cannot lose the text', () => {
    const { onFlush } = setup({ draft: 'not yet saved', dirty: true })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.preview)) })
    expect(onFlush).toHaveBeenCalledTimes(1)
  })

  it('closes through the hook, not by itself', () => {
    const { onClose } = setup()
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.close)) })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('the footer answers "did it save?" in every state', () => {
  it('Saved', () => {
    setup({ draft: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    expect(screen.getByText(NOTE.saved)).toBeTruthy()
  })

  it('Saving…', () => {
    setup({ saving: true })
    expect(screen.getByText(NOTE.saving)).toBeTruthy()
  })

  it('Unsaved changes', () => {
    setup({ draft: 'typing', dirty: true })
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
  })

  it('never reads Saved over a refused save, because the hook keeps that draft dirty', () => {
    setup({ draft: 'x', updatedAt: '2026-08-04T00:00:00Z', dirty: true, saveFailed: true })
    expect(screen.queryByText(NOTE.saved)).toBeNull()
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
  })

  it('Adding image… takes precedence, since that is the one the user is waiting on', () => {
    setup({ draft: 'x', dirty: true, uploading: true })
    expect(screen.getByText(NOTE.uploading)).toBeTruthy()
    expect(screen.queryByText(NOTE.unsaved)).toBeNull()
  })

  it('the autosave hint before anything was ever saved', () => {
    setup()
    expect(screen.getByText(NOTE.hint)).toBeTruthy()
  })

  it('says it is loading, and locks the field, until the note has been read', () => {
    // Typing into a field whose note has not arrived would be typing over it.
    const { field } = setup({ loaded: false })
    expect(field.readOnly).toBe(true)
    expect(screen.getByText(NOTE.loading)).toBeTruthy()
    expect(screen.queryByText(NOTE.hint)).toBeNull()
  })

  it('does not promise to save while the load failed and the field is read-only', () => {
    // The old footer read "Saves automatically as you type." under a field that
    // could not be typed into — the panel promised the exact thing it had revoked.
    const { field } = setup({ loadFailed: true })
    expect(field.readOnly).toBe(true)
    expect(screen.getByText(NOTE.paused)).toBeTruthy()
    expect(screen.queryByText(NOTE.hint)).toBeNull()
    expect(screen.queryByText(NOTE.unsaved)).toBeNull()
  })

  it('stays editable once the note loaded cleanly', () => {
    const { field } = setup()
    expect(field.readOnly).toBe(false)
  })
})

// The panel's own error surface. A toast fades; "your note is not on disk" is a
// state, and the rule the dashboard holds itself to (`errors-use-error-notice`) is
// that a state like that is rendered where the user is working.
describe('a failure the user can act on is shown in the panel', () => {
  it('renders a notice when the save was refused', () => {
    setup({ draft: 'x', dirty: true, saveFailed: true })
    expect(screen.getByRole('alert').textContent).toContain(SESSION.noteSaveFailed)
  })

  it('renders a CATALOG string when the note could not be loaded, not the server sentence', () => {
    // The backend's "the note could not be read from disk" is English in all 13
    // locales; the error CODE is the contract and the copy is the catalog's, with a
    // next step in it.
    setup({ loadFailed: true })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain(NOTE.loadFailed)
    expect(alert.textContent).not.toContain('could not be read from disk')
  })

  it('renders a refused paste in the same slot, instead of only behind the bell', () => {
    // After a refused paste the footer used to return to "Saved" with nothing
    // inserted and the explanation in the notification feed a user mid-meeting
    // will not open. Same slot as the save failure, same persistence.
    setup({ uploadError: 'tooLarge' })
    expect(screen.getByRole('alert').textContent).toContain(SESSION.noteImageTooLarge)
  })

  it('distinguishes "too large" from "not an image we take"', () => {
    setup({ uploadError: 'failed' })
    expect(screen.getByRole('alert').textContent).toContain(SESSION.noteImageFailed)
  })

  it('ranks the failures by what the next action can lose: save, then load, then paste', () => {
    const both = setup({ draft: 'x', dirty: true, saveFailed: true, loadFailed: true, uploadError: 'failed' })
    expect(screen.getByRole('alert').textContent).toContain(SESSION.noteSaveFailed)
    both.view.unmount()

    setup({ loadFailed: true, uploadError: 'failed' })
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain(NOTE.loadFailed)
    expect(alert.textContent).not.toContain(SESSION.noteImageFailed)
  })

  it('stays quiet when nothing failed', () => {
    setup({ draft: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('offers NO agent hand-off, because the draft is the only copy of the note', () => {
    // The hand-off navigates to a chat, which unmounts the meeting view and with it
    // the hook that holds the draft -- and the notice is on screen precisely because
    // the draft is not on disk. This is the reason the prop is opt-in, asserted
    // rather than trusted.
    setup({ draft: 'x', dirty: true, saveFailed: true })
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
  it('hands the file and the caret to the hook', () => {
    // The caret is read at paste time, BEFORE the upload: by the time it resolves the
    // field may have lost focus or been unmounted, and the hook inserts at this index.
    const { onPasteImage, field, paste } = setup({ draft: 'one two' })
    act(() => { field.setSelectionRange(3, 3) })
    const file = pngFile()
    const result = paste({ file })
    expect(onPasteImage).toHaveBeenCalledTimes(1)
    expect(onPasteImage).toHaveBeenCalledWith(file, 3)
    expect(result.defaultPrevented).toBe(true)
  })

  it('ignores a paste that also carries text', () => {
    // Office on macOS puts an image on the clipboard ALONGSIDE the copied text;
    // treating that as an image paste silently swallows what the user copied.
    const { onPasteImage, paste } = setup()
    const result = paste({ file: pngFile(), text: true })
    expect(onPasteImage).not.toHaveBeenCalled()
    // And the default is left alone, so the text still pastes.
    expect(result.defaultPrevented).toBe(false)
  })

  it('leaves a plain text paste alone', () => {
    const { onPasteImage, paste } = setup()
    const result = paste({ text: true })
    expect(onPasteImage).not.toHaveBeenCalled()
    expect(result.defaultPrevented).toBe(false)
  })
})

describe('preview', () => {
  it('swaps the editor for rendered markdown', () => {
    const { view } = setup({ draft: '# Heading' })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.preview)) })
    expect(view.queryByLabelText(NOTE.editorLabel)).toBeNull()
    expect(screen.getByText('Heading')).toBeTruthy()
  })

  it('goes back to the editor', () => {
    const { view } = setup({ draft: 'x' })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.preview)) })
    act(() => { fireEvent.click(screen.getByLabelText(NOTE.edit)) })
    expect(view.getByLabelText(NOTE.editorLabel)).toBeTruthy()
  })
})

describe('note wiring', () => {
  it('is not polled', () => {
    // The draft is the authoritative copy; refetching under the user is how an
    // autosaving editor loses a sentence.
    const block = SessionSource.match(/const noteQuery = useQuery\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('refetchInterval: false')
    expect(block![0]).toContain('refetchOnWindowFocus: false')
    expect(block![0]).toContain('noteOpen')
  })

  it('owns the draft above the panel, through the hook, and awaits each save', () => {
    // The panel is unmounted when closed; a draft in its state died with it. The
    // hook has to see the ACKNOWLEDGEMENT (not just the send) to advance its
    // baseline, which is why it is handed `mutateAsync` rather than `mutate`.
    const block = SessionSource.match(/const noteDraft = useNoteDraft\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('noteMutation.mutateAsync(')
    expect(block![0]).toContain('serverContent: noteQuery.data?.content')
    expect(block![0]).toContain('loadFailed: noteQuery.isError')
  })

  it('hands the GET failure out as a flag, so the panel renders catalog copy for it', () => {
    // Without this the only trace of a failed load is an empty textarea; with the
    // server SENTENCE it was unlocalized English in every locale.
    expect(SessionSource).toContain('loadFailed: noteQuery.isError')
    expect(SessionSource).not.toContain('(noteQuery.error as Error).message')
  })

  it('serializes saves, so an older response cannot seed the cache last', () => {
    // Completions in SEND order are what let the hook's acknowledgement sequence
    // stay monotonic in practice. Asserted on the source because the ordering lives
    // in React Query's mutation scope rather than in code this suite can drive.
    // `scope` is the same mechanism ChatPanel uses on the hidden-models save.
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

  it('leaves the view only through the note\'s exit, on every back control', () => {
    // The draft unmounts with the view, so each way out awaits `leaveNote` and
    // navigates on `true` alone (the behaviour itself is driven in
    // UseMeetingSessionCoverage). No control may call the `onBack` prop directly.
    expect(ViewSource).toContain('if (await session.leaveNote()) onBack()')
    const directBackCalls = ViewSource.match(/\bonBack\(\)/g) ?? []
    expect(directBackCalls).toHaveLength(1)
    expect(ViewSource).not.toMatch(/onClick=\{onBack\}/)
    expect(ViewSource).toContain("onClick={() => { void leave() }} aria-label={i18nT('apps.meetings.meeting.back')}")
  })

  it('retains nothing past the hook: no module-level draft, no conflict arbitration', () => {
    // The mechanism that once kept a refused exit save alive across the view's
    // unmount is gone with its two-button conflict UI; the exit awaits the save
    // instead. Asserted so it does not come back by accident.
    const HookSource = readFileSync('src/apps/meetings/hooks/useNoteDraft.ts', 'utf-8')
    expect(HookSource).not.toMatch(/retainedDrafts|conflicted|keepMyVersion|useSavedVersion/)
    expect(SessionSource).not.toMatch(/conflicted|keepMyNoteVersion|useSavedNoteVersion/)
    expect(EN_CATALOG.apps.meetings.note).not.toHaveProperty('conflict')
    expect(EN_CATALOG.apps.meetings.note).not.toHaveProperty('keepMyVersion')
    expect(EN_CATALOG.apps.meetings.note).not.toHaveProperty('useSavedVersion')
  })

  it('sends the save with keepalive, so a closing tab can still deliver it', () => {
    // The exit that cannot wait for `leaveNote`. Bounded by the browser's 64 KiB
    // keepalive cap, which the comment on `saveNote` records.
    const block = ApiSource.match(/saveNote: \(id: string, content: string\) =>[\s\S]*?\n {4}\}\),/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('keepalive: true')
  })
})

describe('the note lives outside the agent-writable meeting directory', () => {
  it('is stored under an app-owned notes/ root the shared write gate fences', () => {
    // Every meeting agent ships fs_write and is handed the meeting directory, so a
    // note in there was one prompt-injected write away from being overwritten. The
    // Python side pins the location and the gate; this is the frontend-side reminder
    // that the path the panel renders images from is a security property.
    expect(StoreSource).toContain('def notes_root(')
    expect(StoreSource).toContain('k.NOTES_DIR')
    expect(SecuritySource).toContain('apps/meetings/data/notes')
  })
})
