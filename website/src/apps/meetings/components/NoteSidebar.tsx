// The user's own note for a meeting — the one thing in this app the user writes
// rather than an agent.
//
// A plain textarea, not a rich editor: the content is markdown, and during a
// meeting the useful affordance is typing without the cursor jumping, which every
// WYSIWYG layer eventually breaks. Rendering lives behind a Preview toggle instead,
// which is also what makes a pasted image visible.
//
// The panel is PRESENTATION. The draft, the debounce, the flush and the pending
// paste all live in `hooks/useNoteDraft.ts`, which is mounted with the meeting and
// not with this panel — so closing the panel flushes, and a save refused on the way
// out leaves the text where reopening finds it, instead of dying with this
// component's state. Every prop here is read from that hook; every callback writes
// to it.

import { useCallback, useEffect, useRef, useState } from 'react'
import { Eye, ImagePlus, NotebookPen, Pencil, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import MarkdownRenderer, { BasePathCtx } from '../../../components/MarkdownRenderer'
import type { NoteUploadError } from '../hooks/useNoteDraft'

// Re-exported so the caret arithmetic keeps one import site for its tests.
export { imageSnippet, insertBlock } from '../noteText'

interface Props {
  /** The live text. Owned by the draft hook, never by this component. */
  draft: string
  /** The draft differs from what the server has confirmed. */
  dirty: boolean
  /** The first GET has answered. Until then the field is read-only. */
  loaded: boolean
  updatedAt: string
  /** Absolute path of the note file, so relative image links resolve when rendered. */
  path: string
  saving: boolean
  /** The most recent save was refused and nothing has landed since. */
  saveFailed: boolean
  /**
   * The initial GET failed. Separate from `saveFailed` because the two are
   * different losses: a refused save means the text on screen is not on disk, a
   * refused load means the text on screen is not what IS on disk — so the field is
   * read-only and every save path is shut while this is set.
   */
  loadFailed: boolean
  uploading: boolean
  uploadError: NoteUploadError | null
  onChange: (value: string) => void
  onFlush: () => void
  onPasteImage: (file: File, caret: number) => void
  onClose: () => void
}

export default function NoteSidebar({
  draft,
  dirty,
  loaded,
  updatedAt,
  path,
  saving,
  saveFailed,
  loadFailed,
  uploading,
  uploadError,
  onChange,
  onFlush,
  onPasteImage,
  onClose,
}: Props) {
  const [preview, setPreview] = useState(false)
  const fieldRef = useRef<HTMLTextAreaElement>(null)
  // Through a ref so the unmount effect can hold one stable identity while still
  // calling whatever `onFlush` is current.
  const onFlushRef = useRef(onFlush)
  onFlushRef.current = onFlush

  // Flush on unmount: closing the panel or ending the meeting must not leave the
  // last few seconds of typing unsent. The draft itself survives this — it lives in
  // the hook — so a refusal here is retried, not lost.
  useEffect(() => () => { onFlushRef.current() }, [])

  /**
   * Take an image off the clipboard and hand it to the draft hook.
   *
   * The `types.includes('text/plain')` guard is the rule ChatInput established: some
   * apps (Office on macOS notably) put an image on the clipboard ALONGSIDE the text
   * the user actually copied, and treating that as an image paste silently swallows
   * their text.
   */
  const handlePaste = useCallback(
    (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const data = event.clipboardData
      if (!data) return
      if (Array.from(data.types).includes('text/plain')) return
      const file = Array.from(data.items)
        .filter(item => item.kind === 'file')
        .map(item => item.getAsFile())
        .find((candidate): candidate is File => candidate != null)
      if (!file) return

      // Only now: a paste we are not handling must keep its default behaviour.
      event.preventDefault()
      // Read the caret NOW — the upload is async and the element may have lost focus
      // (or been unmounted) by the time it resolves, at which point selectionStart no
      // longer means what it meant.
      const caret = fieldRef.current?.selectionStart ?? draft.length
      onPasteImage(file, caret)
    },
    [draft.length, onPasteImage],
  )

  const readOnly = loadFailed || !loaded

  // Each failure gets a notice that STAYS, the way SettingsView's save failure does:
  // a toast is transient feedback, and "your note is not on disk" is a state the
  // panel has to keep showing for as long as it is true. Precedence is by what the
  // user's next action can turn into lost text: the save refusal first (closing the
  // panel on it loses the note), then the load refusal (typing on it would overwrite
  // the note), then a refused paste (the image is still on the clipboard).
  const failure = saveFailed
    ? i18nT('apps.meetings.session.noteSaveFailed')
    : loadFailed
      ? i18nT('apps.meetings.note.loadFailed')
      : uploadError === 'tooLarge'
        ? i18nT('apps.meetings.session.noteImageTooLarge')
        : uploadError === 'failed'
          ? i18nT('apps.meetings.session.noteImageFailed')
          : ''

  return (
    // Stacked below `lg` with a bounded height, side-by-side at 340px from `lg`
    // up — the same responsive shape TaskSidebar and TranslationSidebar use,
    // because a fixed 340px column beside the meeting clips the panel inside a
    // 320px viewport.
    <aside
      className="flex-none w-full h-[42%] min-h-[260px] border-t border-border lg:h-full lg:w-[340px] lg:border-t-0 lg:border-l bg-bg flex flex-col overflow-hidden"
      aria-label={i18nT('apps.meetings.note.title')}
    >
      <div className="flex-none px-3 py-2.5 border-b border-border flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <NotebookPen className="lucide-inline text-muted" />
          <span className="text-[13px] font-semibold text-text-strong truncate">
            {i18nT('apps.meetings.note.title')}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <Btn
            onClick={() => {
              // Flush first: previewing text that has not been saved would show the
              // right thing but leave the note behind if the panel then closed.
              onFlush()
              setPreview(open => !open)
            }}
            aria-label={
              preview ? i18nT('apps.meetings.note.edit') : i18nT('apps.meetings.note.preview')
            }
            title={
              preview ? i18nT('apps.meetings.note.edit') : i18nT('apps.meetings.note.preview')
            }
            aria-pressed={preview}
          >
            {preview ? <Pencil className="lucide-inline" /> : <Eye className="lucide-inline" />}
          </Btn>
          <Btn onClick={onClose} aria-label={i18nT('apps.meetings.note.close')}>
            <X className="lucide-inline" />
          </Btn>
        </div>
      </div>

      {/* No `askAgent`. The hand-off navigates to a chat, which unmounts the whole
          meeting view and with it the draft hook — and this notice is on screen
          precisely because the draft is the only copy of the note. Handing an
          unsaved note to an agent is the one thing this panel must not offer. */}
      {failure ? <ErrorNotice className="flex-none m-3 mb-0" message={failure} /> : null}

      {preview ? (
        // `BasePathCtx` is what makes `![10:23](images/xxx.png)` work: the shared
        // renderer resolves a relative image src against this path and fetches it
        // through the dashboard's own hardened file route, so this app needs no
        // image-serving endpoint of its own.
        <div className="flex-1 min-h-0 overflow-y-auto p-3 text-[13px]">
          <BasePathCtx.Provider value={path}>
            <MarkdownRenderer content={draft} />
          </BasePathCtx.Provider>
        </div>
      ) : (
        <textarea
          ref={fieldRef}
          value={draft}
          onChange={e => onChange(e.target.value)}
          onBlur={onFlush}
          onPaste={handlePaste}
          placeholder={i18nT('apps.meetings.note.placeholder')}
          // Distinct from the <aside>'s label on purpose: the region and the control
          // are different things, and giving both the same accessible name makes them
          // indistinguishable to a screen reader (and ambiguous to a test).
          aria-label={i18nT('apps.meetings.note.editorLabel')}
          // Read-only until the note has loaded and while the load failed, so the
          // block is visible BEFORE a paragraph is typed. Suppressing the save alone
          // would let the footer sit at "Unsaved changes" with no way to reach "Saved".
          readOnly={readOnly}
          spellCheck
          // The cue is an INSET ring: the panel is `overflow-hidden`, so the global
          // `:focus-visible` outline (2px, offset 2px, painted outside the box) would
          // be clipped on the edges this field sits flush against.
          className="flex-1 min-h-0 resize-none bg-transparent border-none p-3 text-[13px] leading-relaxed text-text font-body placeholder:text-muted/60 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
        />
      )}

      <div className="flex-none px-3 py-2 border-t border-border text-[12px] text-muted flex items-center gap-1.5">
        {/* "Did my note save?" is the only question a user asks of an autosaving
            field, so it is answered in every state — with the image upload taking
            precedence, since that is the one the user is waiting on. A field that is
            not editable says so instead of promising to save what cannot be typed. */}
        {loadFailed ? (
          i18nT('apps.meetings.note.paused')
        ) : !loaded ? (
          i18nT('apps.meetings.note.loading')
        ) : uploading ? (
          <>
            <ImagePlus className="lucide-inline animate-pulse" />
            {i18nT('apps.meetings.note.uploading')}
          </>
        ) : saving ? (
          i18nT('apps.meetings.note.saving')
        ) : dirty ? (
          i18nT('apps.meetings.note.unsaved')
        ) : updatedAt ? (
          i18nT('apps.meetings.note.saved')
        ) : (
          i18nT('apps.meetings.note.hint')
        )}
      </div>
    </aside>
  )
}
