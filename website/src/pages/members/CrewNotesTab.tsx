/**
 * Notes tab — the crewmate's own standing notes ("what it learned").
 *
 * The body is the crewmate's self-maintained briefing markdown
 * (`members/<slug>/briefing.md`), read through
 * `GET /api/members/{slug}/briefing` and rendered by the real
 * `MarkdownRenderer`. Read-only by design, with no editor: the crewmate
 * writes this file as it works, and the dashboard's file viewer reads through
 * a redacting path whose Save writes the buffer back — an in-dashboard edit of
 * an agent-written file could replace a secret the crewmate wrote in the
 * meantime with its placeholder. The notes are changed where the crewmate
 * keeps them, outside the dashboard. The states, never conflated: loading, a
 * refused read because two crewmates share the slug (the file would belong to
 * neither), failed first read, unsupported platform (the backend fails closed
 * rather than reading the file racily), no notes yet (the normal state of a
 * fresh crewmate — an empty state, not an error), and content — which, when a
 * later refetch fails, stays on screen under a notice saying it may be stale,
 * and which says so above the text when part of it is hidden (a secret
 * replaced by its placeholder, a tail past the cap).
 */
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { EyeOff } from 'lucide-react'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { memberBriefingQueryKey } from '../../api/membersQuery'
import { Skeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { timeAgo } from '../../utils/timeAgo'

interface CrewNotesTabProps {
  slug: string
  /** The exact crew name — slugs are lossy, so the read is keyed by both. */
  member: string
  /** The identity row (avatar + name + live status) the three panel tabs share. */
  header: ReactNode
  /** Whether this tab body is on screen — gates the briefing read, so a panel
   *  showing another tab does not pay for notes nobody is looking at. */
  visible: boolean
}

/** The backend refused the read because two crewmates derive this slug: the
 *  notes file is shared, so it is nobody's to show or edit. A state of its own,
 *  in plain words, not a generic failure — the fix is a rename, not a retry. */
function isSlugCollision(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

export default function CrewNotesTab({ slug, member, header, visible }: CrewNotesTabProps) {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: memberBriefingQueryKey(slug, member),
    queryFn: () => api.memberBriefing(slug, member),
    enabled: visible && !!slug && !!member,
    // No retries: a 409 is a stable answer about the roster, and a failed read
    // is re-issued the moment the tab is shown again (`enabled` flips), so a
    // backoff here would only delay the notice.
    retry: false,
    // Always stale: the key sits under the registry prefix so a roster
    // `refresh` frame revalidates it, and a return to the tab refetches too --
    // cached notes render at once, but the collision check runs every time.
    staleTime: 0,
  })
  const data = query.data
  // Pending and failed are told apart the way every block on this page does
  // it: a failed read must not render the affirmative empty state. A collision
  // outranks cached notes — the roster changed under them, so the cached text
  // is nobody's any more. A failed REFETCH over cached notes keeps them on
  // screen (they were true once) under a notice, never silently.
  const loading = data === undefined && !query.isError
  const collision = query.isError && isSlugCollision(query.error)
  const failed = data === undefined && query.isError && !collision
  const stale = data !== undefined && query.isError && !collision
  // Part of the text is not what the file holds: a secret replaced by its
  // placeholder, or a tail past the cap standing behind the marker. Said in a
  // visible line above the notes -- a placeholder with no reason reads as the
  // crewmate's own words, and a tooltip would reach neither keyboard nor touch.
  const hidden = data?.redacted
    ? 'pages.membersPage.notes_redacted_notice'
    : data?.truncated
      ? 'pages.membersPage.notes_truncated_notice'
      : null

  return (
    <div className="flex flex-col h-full" data-testid="member-notes">
      <div className="px-3 pt-3 shrink-0">{header}</div>
      <div className="flex-1 min-h-0 overflow-y-auto px-3 pb-3">
        {loading ? (
          <div className="space-y-2" data-testid="member-notes-loading" aria-hidden>
            <Skeleton className="h-3 w-3/4" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
          </div>
        ) : collision ? (
          <p className="text-[12px] text-muted" data-testid="member-notes-collision">
            {t('pages.membersPage.notes_collision')}
          </p>
        ) : failed ? (
          /* The shared notice, not a hand-rolled alert. askAgent is safe here:
             a read failure on a read-only surface holds no draft to lose. */
          <ErrorNotice
            message={t('pages.membersPage.notes_error')}
            variant="inline"
            askAgent
            testId="member-notes-error"
          />
        ) : (
          <>
            {stale && (
              /* Cached notes stay below; the notice says they may be behind.
                 Same shape as the dashboard's "couldn't refresh" notice. */
              <div className="mb-2">
                <ErrorNotice
                  message={t('pages.membersPage.notes_refresh_error')}
                  variant="inline"
                  askAgent
                  testId="member-notes-refresh-error"
                />
              </div>
            )}
            {data?.supported === false ? (
              <p className="text-[12px] text-muted" data-testid="member-notes-unsupported">
                {t('pages.membersPage.notes_unsupported')}
              </p>
            ) : !data?.text ? (
              <div className="space-y-1.5" data-testid="member-notes-empty">
                <p className="text-[12px] text-text">{t('pages.membersPage.notes_empty', { name: member })}</p>
                <p className="text-[11.5px] text-muted">{t('pages.membersPage.notes_empty_hint')}</p>
              </div>
            ) : (
              <>
                {hidden && (
                  <p
                    className="flex items-start gap-1.5 mb-2 text-[11.5px] text-muted"
                    role="status"
                    data-testid="member-notes-hidden"
                  >
                    <EyeOff className="lucide-inline mt-0.5 shrink-0" aria-hidden="true" />
                    <span>{t(hidden)}</span>
                  </p>
                )}
                <div className="msg-content text-[13px] leading-relaxed" data-testid="member-notes-body">
                  <MarkdownRenderer content={data.text} />
                </div>
              </>
            )}
          </>
        )}
      </div>
      {/* Footer: when the notes were last written. Rendered once a read has
          answered with a dated file — a footer over a skeleton would date
          notes that have not arrived — and withdrawn on a collision, when the
          cached notes are no longer anyone's. */}
      {data && !collision && data.updated_ts ? (
        <div
          className="px-3 py-1.5 border-t border-border text-[10.5px] text-muted shrink-0 truncate"
          data-testid="member-notes-footer"
        >
          {t('pages.membersPage.notes_updated', { when: timeAgo(data.updated_ts) })}
        </div>
      ) : null}
    </div>
  )
}
