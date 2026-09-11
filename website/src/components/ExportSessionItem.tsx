import type React from 'react'
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Check, Download, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import ErrorNotice from './ErrorNotice'

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
  readonly Item: React.ComponentType<{
    title?: string
    disabled?: boolean
    onSelect?: (event: Event) => void
    children?: React.ReactNode
  }>
  /**
   * Memory mode of the session. An incognito or temporary transcript exists
   * under a promise that nothing is kept, so the backend refuses to export one;
   * the row renders disabled with the reason rather than offering a click that
   * only ever 400s.
   */
  readonly memoryMode?: 'persistent' | 'incognito' | 'temporary'
}

/**
 * "Export to a file" — download this session as one `.kcsession.json.gz`.
 *
 * Sits beside `SendToInstanceSubmenu` because it is the same act with the live
 * hop removed: the tunnel needs both machines up and reachable at the same
 * moment, and a file does not, so a sleeping laptop or a machine on another
 * account is reachable this way and not the other.
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
export default function ExportSessionItem({ slotKey, Item, memoryMode }: ExportSessionItemProps) {
  const [state, setState] = useState<ExportState>({ kind: 'idle' })
  const notPersistent = memoryMode !== undefined && memoryMode !== 'persistent'

  const exportMutation = useMutation({
    mutationFn: () => api.exportSession(slotKey),
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
      <span className="flex-1">{i18nT('components.exportSessionItem.export_to_file')}</span>
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
        // `askAgent` is on because nothing here is unsaved: an export is a read, so
        // the hand-off's navigation destroys no draft. Worth recording what was
        // measured against a real Radix menu, though -- arrow keys move between
        // ITEMS and Tab is swallowed, so focus does not enter the notice and the
        // button is reachable by pointer only. The readable text is what serves
        // every other input method, and pressing Enter on the row retries.
        //
        // The wrapper swallows pointer events because a click inside the notice
        // would otherwise bubble to the item and re-fire the export, replacing the
        // error with a fresh spinner.
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
            message={state.message}
            title={i18nT('components.exportSessionItem.failed')}
            variant="inline"
            askAgent
          />
        </span>
      )}
    </Item>
  )
}
