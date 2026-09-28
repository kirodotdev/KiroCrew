import { useId, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Check, Upload, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { parseErrorCode } from '../utils/errorReport'
import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from './ErrorNotice'

import { i18nT } from '../i18n/t'

/**
 * The endpoint's refusal codes, in the vocabulary of what the person chose.
 *
 * The server's own strings are written for the WIRE — "bundle", "request body",
 * a byte ceiling — and are English only, which reads oddly on a row whose every
 * other string comes from a catalog in 13 languages. The person picked a FILE, so
 * a code this row recognises is spoken as one.
 *
 * Thunks, not strings: the values must be read AFTER i18n initialises, and a
 * literal key per entry keeps every key statically resolvable rather than
 * assembled at the call site.
 */
const REFUSAL_COPY: Record<string, () => string> = {
  transfer_invalid_gzip: () => i18nT('components.importSessionItem.refusal_damaged_file'),
  transfer_invalid_json: () => i18nT('components.importSessionItem.refusal_not_a_session'),
  transfer_body_not_object: () => i18nT('components.importSessionItem.refusal_not_a_session'),
  transfer_bundle_too_large: () => i18nT('components.importSessionItem.refusal_too_large'),
  transfer_body_unreadable: () => i18nT('components.importSessionItem.refusal_upload_failed'),
  transfer_expansion_busy: () => i18nT('components.importSessionItem.refusal_busy'),
}

/**
 * What the row says when an import fails.
 *
 * A RECOGNISED code is rewritten from the catalog; anything else falls through
 * to the endpoint's own text, because a refusal this row cannot translate —
 * "bundle carries no messages", a validation code added after this map was
 * written — is far more use than a generic failure that hides what the server
 * actually said.
 */
function refusalMessage(e: unknown): string {
  const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
  const spoken = code ? REFUSAL_COPY[code] : undefined
  if (spoken) return spoken()
  if (e instanceof Error && e.message) return e.message
  return i18nT('components.importSessionItem.unknown_error')
}

/** Outcome of the most recent import attempt in this open menu. */
type ImportState =
  | { kind: 'idle' }
  | { kind: 'importing' }
  | { kind: 'done'; title: string }
  | { kind: 'error'; message: string }

interface ImportSessionItemProps {
  /** The Radix menu-item primitive of the hosting menu family. */
  readonly Item: ErrorNoticeMenuItemComponent
}

/**
 * "Import a session from a file" — the other half of `ExportSessionItem`.
 *
 * Until this existed the product could write a `.kcsession.json.gz` and had no
 * way to read one back: the import endpoint's only consumer was the
 * server-to-server tunnel, so the file a user was handed by the Export row was
 * a file nothing would take. That is why this sits directly beside it.
 *
 * Takes NO `slotKey`. Every other row in this menu acts on the session whose
 * menu is open; this one creates a NEW session and leaves that one untouched, so
 * binding it to a slot would suggest a relationship it does not have —
 * importing the same file twice is simply two sessions.
 *
 * The bytes go up exactly as they came off disk. The endpoint decides the format
 * from them, so there is no gunzip step here and no branch on the file's name: a
 * user who unpacked the archive by hand is not thereby holding a file the
 * product refuses.
 *
 * **The menu deliberately stays open on select** and the outcome renders on the
 * row, matching `ExportSessionItem` and `SendToInstanceSubmenu`. A new session
 * appearing in the sidebar is easy to miss, and there is no toast primitive in
 * this app; the sibling convention is an inline note next to the control, and
 * the row IS the control.
 */
export default function ImportSessionItem({ Item }: ImportSessionItemProps) {
  const errorId = useId()
  const [state, setState] = useState<ImportState>({ kind: 'idle' })
  const inputRef = useRef<HTMLInputElement>(null)

  const importMutation = useMutation({
    mutationFn: (file: File) => api.importSessionFromFile(file),
    onMutate: () => { setState({ kind: 'importing' }) },
    onSuccess: (r) => { setState({ kind: 'done', title: r.title }) },
    onError: (e) => {
      setState({ kind: 'error', message: refusalMessage(e) })
    },
  })

  return (
    <>
      <Item
        disabled={state.kind === 'importing'}
        onSelect={(event: Event) => {
          // Keep the menu open so the row can report the outcome.
          event.preventDefault()
          inputRef.current?.click()
        }}
      >
        <Upload size={13} className="shrink-0 text-muted" />
        {/* `truncate`, not a bare `flex-1`: when the outcome note appears beside
            it the row has two flexible children, and a shrinkable label with no
            nowrap wraps to one word per line. `truncate` carries the nowrap and
            ellipsizes in the worst case instead of overflowing the menu. */}
        <span className="grow truncate">
          {i18nT('components.importSessionItem.import_from_file')}
        </span>
        {/* Off-screen rather than `display:none`: a hidden input is still the
            accessible file control, and `.click()` on a display-none element is
            ignored by some engines. `accept` is a HINT for the picker's default
            filter only -- the endpoint sniffs the bytes, so a user who renamed
            or unpacked the file is not locked out by it. */}
        <input
          ref={inputRef}
          type="file"
          accept=".gz,.json,application/gzip,application/json"
          className="sr-only"
          aria-label={i18nT('components.importSessionItem.import_from_file')}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            const file = e.target.files?.[0]
            // Reset so choosing the SAME file twice fires change twice; without
            // it the second import silently does nothing.
            e.target.value = ''
            if (file) importMutation.mutate(file)
          }}
        />
        {state.kind === 'importing' && (
          <Loader2 size={13} className="ml-auto shrink-0 animate-spin text-muted" />
        )}
        {state.kind === 'done' && (
          // The TITLE, not just "Imported". A new session appears somewhere in a
          // sidebar that may be scrolled or collapsed, and this row is the only
          // place that knows which one it is. The menu is content-sized with no
          // width cap, so the CAP IS HERE: without `max-w` a long title stretches
          // the whole menu across the viewport and `truncate` never fires, which
          // is the truncation this row's contract promises. The full text stays
          // reachable as the element's own title attribute for a pointer user,
          // and the visible label already names the outcome for everyone else.
          <span className="ml-auto flex min-w-0 max-w-[13rem] items-center gap-1 text-[10px] text-ok">
            <Check size={12} className="shrink-0" />
            <span className="truncate" title={state.title || undefined}>
              {state.title
                ? i18nT('components.importSessionItem.imported_named', { title: state.title })
                : i18nT('components.importSessionItem.imported')}
            </span>
          </span>
        )}
        {state.kind === 'error' && (
          // The shared error surface, not a hand-rolled danger span: the message
          // has to be READABLE rather than hidden in a `title=` a keyboard or
          // touch user never reaches. Same menu-only wrapper as the export row:
          // it swallows pointer events so a click on the passive alert cannot
          // bubble back to the row and reopen the file picker. `max-w` is what
          // keeps a server message readable: ErrorNotice's inline variant sets
          // `overflow-wrap: anywhere`, so squeezed into a narrow flex remainder
          // it breaks mid-word — one letter per line at the limit.
          <span
            className="ml-auto min-w-0 max-w-[16rem]"
            role="presentation"
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <ErrorNotice
              id={errorId}
              message={state.message}
              title={i18nT('components.importSessionItem.failed')}
              variant="inline"
            />
          </span>
        )}
      </Item>
      {state.kind === 'error' && (
        <ErrorNoticeMenuItem
          Item={Item}
          message={state.message}
          describedBy={errorId}
        />
      )}
    </>
  )
}
