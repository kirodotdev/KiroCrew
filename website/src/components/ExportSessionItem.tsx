import { useId, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Check, Download, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from './ErrorNotice'

import { i18nT } from '../i18n/t'

/** Outcome of the most recent export attempt in this open menu. */
type ExportState =
  | { kind: 'idle' }
  | { kind: 'exporting' }
  | { kind: 'done' }
  | { kind: 'error'; message: string }

interface ExportSessionItemProps {
  /** The session to export. */
  readonly slotKey: string
  /** The Radix menu-item primitive of the hosting menu family. */
  readonly Item: ErrorNoticeMenuItemComponent
  /**
   * Memory mode of the session. An incognito or temporary transcript exists
   * under a promise that nothing is kept, so the backend refuses to export one;
   * the row renders disabled with the reason rather than offering a click that
   * only ever 400s.
   */
  readonly memoryMode?: 'persistent' | 'incognito' | 'temporary'
  /**
   * Which rendering this row downloads. `'json'` (the default) is the gzipped
   * transfer bundle an Install reads back; `'md'` is the human-readable Markdown
   * transcript, which installs nowhere.
   *
   * A prop rather than a submenu because the hosting menu passes in one `Item`
   * primitive and not the Sub/SubTrigger/SubContent family a nested menu needs:
   * two sibling rows need nothing new from the host, and a format choice with
   * exactly two options reads no worse flat than nested.
   */
  readonly format?: 'json' | 'md'
}

/**
 * "Export to a file" — download this session as one `.kcsession.json.gz`, or as
 * one `.kcsession.md` when `format` is `'md'`.
 *
 * Sits beside `SendToInstanceSubmenu` because it is the same act with the live
 * hop removed: the tunnel needs both machines up and reachable at the same
 * moment, and a file does not, so a sleeping laptop or a machine on another
 * account is reachable this way and not the other.
 *
 * **The two formats answer different questions, so each row says which one it
 * is in its own label.** The JSON bundle is the one an Install reads back, so it
 * is what moves a session to another machine. The Markdown document is for a
 * person: a text editor opens it, a forge renders it, and its fenced code blocks
 * survive a paste into a review or a ticket. Nothing reads Markdown back.
 *
 * Naming the OUTCOME rather than the file type is what makes the pair legible:
 * two rows both reading "Export to a file", separated only by a muted format
 * suffix, ask the user to already know which of the two the Import row below can
 * read — and a user who guesses Markdown to move a session finds out at import
 * time. Putting it in the label also keeps the row's identifying words from
 * shifting when the trailing "Exported" note mounts beside them, and keeps a
 * non-persistent session's row to two parts rather than three.
 *
 * **The menu deliberately stays open on select** (`preventDefault` on the item's
 * select event) and the outcome renders on the row, matching
 * `SendToInstanceSubmenu` for the same reason: a download's only visible effect
 * is in the browser's own download surface, so closing the menu and saying
 * nothing would leave the user unable to tell a saved file from a silent
 * refusal. There is no toast primitive in this app — the sibling convention is
 * an inline note next to the control, and the row IS the control.
 *
 * Nothing is written and nothing is moved: an export is a read of one session,
 * so a repeat click is harmless and needs no confirm step.
 */
export default function ExportSessionItem(
  { slotKey, Item, memoryMode, format = 'json' }: ExportSessionItemProps,
) {
  const errorId = useId()
  const [state, setState] = useState<ExportState>({ kind: 'idle' })
  const notPersistent = memoryMode !== undefined && memoryMode !== 'persistent'

  const exportMutation = useMutation({
    mutationFn: () => api.exportSession(slotKey, format),
    onMutate: () => { setState({ kind: 'exporting' }) },
    onSuccess: () => { setState({ kind: 'done' }) },
    onError: (e) => {
      setState({
        kind: 'error',
        // The API client throws ApiError carrying the endpoint's own message, so
        // this surfaces "this session has no messages to export" rather than a
        // generic failure.
        message: e instanceof Error && e.message
          ? e.message
          : i18nT('components.exportSessionItem.unknown_error'),
      })
    },
  })

  return (
    <>
      <Item
        disabled={notPersistent || state.kind === 'exporting'}
        onSelect={notPersistent
          ? undefined
          : (event: Event) => {
            // Keep the menu open so the row can report the outcome.
            event.preventDefault()
            exportMutation.mutate()
          }}
      >
        <Download size={13} className="shrink-0 text-muted" />
        <span className="flex-1">
          {format === 'md'
            ? i18nT('components.exportSessionItem.export_markdown')
            : i18nT('components.exportSessionItem.export_json')}
        </span>
        {notPersistent && (
          <span className="ml-auto text-[10px] text-muted shrink-0">
            {i18nT('components.exportSessionItem.not_saved_to_disk')}
          </span>
        )}
        {state.kind === 'exporting' && (
          <Loader2 size={13} className="ml-auto shrink-0 animate-spin text-muted" />
        )}
        {state.kind === 'done' && (
          <span className="ml-auto flex items-center gap-1 text-[10px] text-ok shrink-0">
            <Check size={12} />
            {i18nT('components.exportSessionItem.exported')}
          </span>
        )}
        {state.kind === 'error' && (
          // The shared error surface, not a hand-rolled danger span: the message has
          // to be READABLE rather than hidden in a `title=` a keyboard or touch user
          // never reaches.
          //
          // The hand-off is the sibling ErrorNoticeMenuItem below. Keeping it out
          // of this row gives Radix a real focus stop without changing what Enter,
          // Space, or a pointer click on this export row does.
          //
          // This menu-only wrapper swallows pointer events on the passive alert. A
          // click there would otherwise bubble to the row and replace the error with
          // a fresh spinner. Other ErrorNotice hosts keep their ordinary bubbling.
          <span
            className="ml-auto"
            // Not interactive: these handlers BLOCK events rather than acting on
            // them, so the element carries no meaning of its own for a screen
            // reader -- the alert inside it does.
            role="presentation"
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <ErrorNotice
              id={errorId}
              message={state.message}
              title={i18nT('components.exportSessionItem.failed')}
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
