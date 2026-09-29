import { useId } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { LayoutList } from 'lucide-react'

import { api } from '../api/client'
import { isNotFoundError } from '../api/apiError'
import { crewBoardQueryKey } from '../api/crewBoard'
import ErrorNotice, {
  ErrorNoticeMenuItem,
  type ErrorNoticeMenuItemComponent,
} from './ErrorNotice'
import { i18nT } from '../i18n/t'

interface CrewBoardMenuItemProps {
  /** The session whose board this opens — also the conductor the board is keyed on. */
  readonly slotKey: string
  /** The Radix menu-item primitive of the hosting menu family; items must match their parent menu. */
  readonly Item: ErrorNoticeMenuItemComponent
}

/**
 * "Crew board" — the entry point to one conductor's work-item board.
 *
 * Renders NOTHING for a session that owns no work ledger, which is most of
 * them: the board is keyed on a conductor and a session that never conducted
 * anything has no items, so an always-present entry would lead almost everyone
 * to an empty page. `/api/crew-board` answers 404 `no_ledger` for such a
 * session, the same self-hiding contract SendToInstanceSubmenu uses for an
 * unconfigured feature.
 *
 * Any OTHER failure is shown rather than hidden. The two cases look identical
 * from `data` alone, so hiding on both would turn a 500 or a dropped connection
 * into "this session has no board" — a wrong answer the reader has no way to
 * question, and the one shape `errors-use-error-notice` exists to forbid. The
 * surface is the shared `ErrorNotice` plus the sibling `ErrorNoticeMenuItem`
 * hand-off, following ExportSessionItem: a message in a `title=` is a message a
 * keyboard or touch user never reaches.
 *
 * The probe is also the cheapest available existence test: it is the very
 * response the page then renders, so opening the menu warms the page's cache
 * instead of adding a round trip of its own. A menu's Content only mounts while
 * it is open (Radix), so nothing is fetched until a user actually opens the menu.
 */
export default function CrewBoardMenuItem({ slotKey, Item }: CrewBoardMenuItemProps) {
  const navigate = useNavigate()
  const errorId = useId()

  const { data, error } = useQuery({
    queryKey: crewBoardQueryKey(slotKey),
    queryFn: () => api.crewBoard(slotKey),
    // The board's own page polls; for a menu the last read is fresh enough, and
    // a ledger does not appear and vanish between two openings of one menu.
    staleTime: 30_000,
    retry: false,
  })

  // The expected absence, and the only one that hides the entry.
  if (error && isNotFoundError(error)) return null

  if (error) {
    const message = error instanceof Error ? error.message : String(error)
    return (
      <>
        <Item>
          <LayoutList size={13} className="shrink-0 text-muted" />{' '}
          {i18nT('components.sessionActionsMenu.crew_board')}
          {/* This menu-only wrapper swallows pointer events on the passive alert,
              so a click on the message cannot also activate the row. */}
          <span
            className="ml-auto"
            role="presentation"
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <ErrorNotice
              id={errorId}
              message={message}
              title={i18nT('components.sessionActionsMenu.crew_board_failed')}
              variant="inline"
            />
          </span>
        </Item>
        <ErrorNoticeMenuItem Item={Item} message={message} describedBy={errorId} />
      </>
    )
  }

  if (!data) return null

  return (
    <Item onSelect={() => navigate(`/crew-board?conductor=${encodeURIComponent(slotKey)}`)}>
      <LayoutList size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.crew_board')}
    </Item>
  )
}
