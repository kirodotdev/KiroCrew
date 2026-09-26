// The user's own note for a meeting — the one thing in this app the user writes
// rather than an agent.
//
// A plain textarea, not a rich editor: the content is markdown, and during a
// meeting the useful affordance is typing without the cursor jumping, which every
// WYSIWYG layer eventually breaks. Rendering lives behind a Preview toggle instead,
// which is also what makes a pasted image visible.
//
// Saving is DEBOUNCED and automatic. A meeting note has no natural moment to press
// Save — the meeting is the moment — and a note lost because the user closed the
// panel mid-thought is the exact failure this feature exists to prevent. The flush
// on unmount is what covers closing the panel, ending the meeting, and navigating
// away.

import { useCallback, useEffect, useRef, useState } from 'react'
import { Eye, ImagePlus, NotebookPen, Pencil, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import MarkdownRenderer, { BasePathCtx } from '../../../components/MarkdownRenderer'

/** Quiet period after the last keystroke before a save fires. */
const SAVE_DEBOUNCE_MS = 800

/**
 * Insert *snippet* into *text* at *caret*, on its own line.
 *
 * Pasted images are block content: dropping one mid-sentence would split the
 * sentence around it. Exported because the caret arithmetic is the part worth
 * testing, and it is pure.
 */
export function insertBlock(text: string, caret: number, snippet: string): string {
  const at = Math.max(0, Math.min(caret, text.length))
  const before = text.slice(0, at)
  const after = text.slice(at)
  // Only add separators that are missing, so repeated pastes do not accumulate
  // blank lines.
  const lead = before === '' || before.endsWith('\n') ? '' : '\n'
  const trail = after === '' || after.startsWith('\n') ? '' : '\n'
  return `${before}${lead}${snippet}${trail}${after}`
}

/** The markdown for one stored image. `alt` may be empty when a meeting is not live. */
export function imageSnippet(alt: string, src: string): string {
  return `![${alt}](${src})`
}

interface Props {
  /** Server content. Used to seed the editor and to adopt an external change. */
  content: string
  updatedAt: string
  /** Absolute path of the note file, so relative image links resolve when rendered. */
  path: string
  saving: boolean
  /**
   * Whether the last save FAILED. Load-bearing for honesty, not decoration:
   * `flush` advances `savedRef` before the response lands (it has to, or the
   * in-flight response would revert every keystroke), so without this the
   * footer reads "Saved" after a PUT that never stored anything.
   */
  saveFailed: boolean
  /**
   * Server sentence from a failed initial GET, or `''`. Separate from
   * `saveFailed` because the two are different losses: a refused save means the
   * text on screen is not on disk, a refused load means the text on screen is not
   * what IS on disk.
   */
  loadError: string
  /** Uploads one pasted image and resolves with the markdown to insert. */
  onUploadImage: (file: File) => Promise<{ alt: string; src: string } | null>
  onSave: (content: string) => void
  onClose: () => void
}

export default function NoteSidebar({
  content,
  updatedAt,
  path,
  saving,
  saveFailed,
  loadError,
  onUploadImage,
  onSave,
  onClose,
}: Props) {
  const [draft, setDraft] = useState(content)
  const [preview, setPreview] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fieldRef = useRef<HTMLTextAreaElement>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // What the server last confirmed. Compared on flush so an unmount cannot re-save
  // text nobody changed, and compared below to decide whether a server update is
  // genuinely external.
  const savedRef = useRef(content)
  const draftRef = useRef(content)
  draftRef.current = draft
  const onSaveRef = useRef(onSave)
  onSaveRef.current = onSave
  // Read through a ref for the same reason `draft` and `onSave` are: `flush` keeps an
  // empty dep list because the unmount effect must hold one stable identity.
  const saveFailedRef = useRef(saveFailed)
  saveFailedRef.current = saveFailed
  // A FAILED load leaves the field empty, which is indistinguishable from "no note
  // yet" — so every save path has to stay shut while it is set, or the debounce (or
  // the unmount flush, which closing the panel takes) replaces a note that exists on
  // disk with what the user typed into what looked like a blank one.
  const loadErrorRef = useRef(loadError)
  loadErrorRef.current = loadError

  // Adopt a server value that differs from what we last sent — another tab, or the
  // first load landing after the panel opened. Guarded on `savedRef` rather than on
  // `draft`, or every keystroke would be reverted by the in-flight response.
  useEffect(() => {
    if (content === savedRef.current) return
    savedRef.current = content
    setDraft(content)
  }, [content])

  const flush = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    // Before the dirty check, not after: a note that failed to load must not be
    // written back on ANY trigger, however the text compares.
    if (loadErrorRef.current) return
    // `savedRef` is advanced when a save is SENT, not when it lands, so after a
    // refusal it equals the draft and an equality check alone would never resend --
    // the footer would read "Unsaved changes" forever while nothing retried, and
    // closing the panel would still drop the note. A failed save is therefore dirty
    // however the text compares, and blur / preview / unmount each retry once.
    if (draftRef.current === savedRef.current && !saveFailedRef.current) return
    savedRef.current = draftRef.current
    onSaveRef.current(draftRef.current)
  }, [])

  // Flush on unmount: closing the panel or ending the meeting must not drop the
  // last few seconds of typing.
  useEffect(() => () => { flush() }, [flush])

  const schedule = useCallback((value: string) => {
    setDraft(value)
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(flush, SAVE_DEBOUNCE_MS)
  }, [flush])

  /**
   * Take an image off the clipboard, store it, and reference it from the note.
   *
   * The `types.includes('text/plain')` guard is the rule ChatInput established: some
   * apps (Office on macOS notably) put an image on the clipboard ALONGSIDE the text
   * the user actually copied, and treating that as an image paste silently swallows
   * their text.
   */
  const handlePaste = useCallback(
    async (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
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
      // Read the caret BEFORE awaiting — the upload is async and the element may
      // have lost focus (or the user may have clicked elsewhere) by the time it
      // resolves, at which point selectionStart no longer means what it meant.
      const caret = fieldRef.current?.selectionStart ?? draftRef.current.length
      setUploading(true)
      try {
        const stored = await onUploadImage(file)
        if (!stored) return
        schedule(insertBlock(draftRef.current, caret, imageSnippet(stored.alt, stored.src)))
      } finally {
        setUploading(false)
      }
    },
    [onUploadImage, schedule],
  )

  // A failed save counts as dirty however the text compares: the note is not on
  // disk, so "Saved" would be a lie the user acts on by closing the panel.
  const dirty = draft !== savedRef.current || saveFailed

  // Either failure gets a notice that STAYS, the way SettingsView's save failure
  // does: the toast is transient feedback, and "your note is not on disk" is a
  // state the panel has to keep showing for as long as it is true. The save
  // refusal wins when both are set — it is the one the user's next action (close
  // the panel) can turn into lost text.
  const failure = saveFailed
    ? i18nT('apps.meetings.session.noteSaveFailed')
    : loadError

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
              flush()
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

      {/* No `askAgent`. The hand-off navigates to a chat, which unmounts this panel
          and destroys `draft` — and this notice is on screen precisely because the
          draft is the only copy of the note. Handing an unsaved note to an agent is
          the one thing this panel must not offer. */}
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
          onChange={e => schedule(e.target.value)}
          onBlur={flush}
          onPaste={handlePaste}
          placeholder={i18nT('apps.meetings.note.placeholder')}
          // Distinct from the <aside>'s label on purpose: the region and the control
          // are different things, and giving both the same accessible name makes them
          // indistinguishable to a screen reader (and ambiguous to a test).
          aria-label={i18nT('apps.meetings.note.editorLabel')}
          // Read-only while the load failed, so the refusal is visible BEFORE a
          // paragraph is typed. Suppressing the save alone would let the footer sit
          // at "Unsaved changes" with no way to reach "Saved".
          readOnly={!!loadError}
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
            precedence, since that is the one the user is waiting on. */}
        {uploading ? (
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
