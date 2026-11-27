import { Loader2, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtPercent } from '../../i18n/format'
import {
  cancelComposerUpload,
  useComposerUploads,
  useUploadStalled,
  type ComposerUpload,
} from '../../utils/composerSendHolds'

/** One running upload: status, the file name, and a cancel for this file only.
 *  The status span is mounted empty with the chip, so a stall is announced
 *  once when it is written; the percent is not announced on every step. */
function UploadingChip({ upload, onCancel }: { upload: ComposerUpload; onCancel: (button: HTMLButtonElement) => void }) {
  const { controller, name, ratio, loaded, sending } = upload
  const stalled = useUploadStalled(controller, sending, loaded)
  const stalledText = stalled ? i18nT('components.chatInput.upload_stalled') : ''
  const cancelLabel = i18nT('components.chatInput.cancel_uploading_name', { name })
  return (
    <div
      role="group"
      aria-label={name}
      title={name}
      data-testid="composer-upload-chip"
      className="relative min-w-0 max-w-[220px] flex items-center gap-1.5 px-2 py-1 rounded border border-border bg-bg-hover text-[12px] text-text"
    >
      <Loader2 size={12} aria-hidden="true" className="shrink-0 animate-spin text-muted" />
      <span className="min-w-0 truncate">{name}</span>
      <span className="shrink-0 tabular-nums" data-testid="composer-upload-progress">
        {!stalled && ratio !== null && <span className="text-muted">{fmtPercent(ratio)}</span>}
        <span role="status" className="text-warn-text">{stalledText}</span>
      </span>
      <button
        type="button"
        className="shrink-0 text-muted hover:text-danger cursor-pointer bg-transparent border-none p-0"
        onClick={e => { onCancel(e.currentTarget); cancelComposerUpload(controller) }}
        aria-label={cancelLabel}
        title={cancelLabel}
      ><X size={12} /></button>
    </div>
  )
}

/** The uploads still on their way for `slot`, one chip per file, in the same
 *  strip area and chip look as the staged files below them. It subscribes
 *  here, in the leaf, so a progress step re-renders these chips and not the
 *  composer. */
export function UploadingStrip({ slot, focusAfterLast, rootRef }: {
  slot?: string | null
  rootRef?: (node: HTMLDivElement | null) => void
  /** Where focus goes when the cancelled chip was the last one. */
  focusAfterLast: () => void
}) {
  const uploads = useComposerUploads(slot)
  /* The cancelled chip unmounts once its request ends, which would drop a
     keyboard user's focus to <body>: it moves to a neighbouring chip's
     cancel first, or to `focusAfterLast` when no chip is left. */
  const onCancel = (button: HTMLButtonElement) => {
    if (document.activeElement !== button) return
    const all = Array.from(button.closest('[data-testid="composer-upload-strip"]')?.querySelectorAll<HTMLButtonElement>('button') ?? [])
    const at = all.indexOf(button)
    const next = all[at + 1] ?? all[at - 1]
    if (next) next.focus()
    else focusAfterLast()
  }
  if (!uploads.length) return null
  /* Wrap to keep every cancel within the width, and scroll vertically so a
     full batch cannot push its first chips above the dock's clipped pane.
     Use svh like the sibling composer-status-stack; rootRef measures the cap. */
  return (
    <div ref={rootRef} data-testid="composer-upload-strip" className="flex flex-wrap max-h-[20svh] overflow-y-auto gap-2 px-4 py-2 border-t border-border bg-chrome/50 items-start">
      {uploads.map(upload => <UploadingChip key={uploadKey(upload.controller)} upload={upload} onCancel={onCancel} />)}
    </div>
  )
}

/** A stable React key per controller (an object has no string form of its own). */
const keys = new WeakMap<AbortController, string>()
let nextKey = 0
function uploadKey(controller: AbortController): string {
  let key = keys.get(controller)
  if (!key) { key = `upload-${++nextKey}`; keys.set(controller, key) }
  return key
}
