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
    if (draftRef.current === savedRef.current) return
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

  const dirty = draft !== savedRef.current

  return (
    <aside
      className="flex-none w-[340px] border-l border-border bg-bg flex flex-col overflow-hidden"
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
          spellCheck
          className="flex-1 min-h-0 resize-none bg-transparent border-none outline-none p-3 text-[13px] leading-relaxed text-text font-body placeholder:text-muted/60"
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
