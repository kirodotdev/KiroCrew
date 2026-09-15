/* Fire (design: Crew Member = Custom Agent + Wrapper, rollout step 5).
 *
 * The reverse of hire, from the member's own drawer. Two steps, like the crew
 * editor's delete -- a misclick in a drawer is likelier than in a table -- and
 * the confirm step says exactly what goes and what stays: the row, the agent
 * file and the pristine copy go; the private memory is archived, never erased;
 * the thread, activity, briefing and rules are ARCHIVED (the transcript stays
 * in History, the space moves to members/.retired) unless the user ticks the
 * purge box, which is the design's "destroy only on explicit request".
 *
 * The outcome -- where the thread went, and whether a purge actually reached it
 * (the history path can refuse) -- is handed UP to the page: the roster
 * refreshes the moment the row is gone and this drawer goes with the member, so
 * a notice that must be read cannot live here. */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation } from '@tanstack/react-query'
import { api, type FireMemberResult, type MemberRosterRow } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { ApiError } from '../../api/apiError'

/** The i18n key of the sentence saying where the fired member's thread went.
 *  A KEPT thread names WHY when the server said (a scheduled job's claim the
 *  history path could not read, an unreadable schedule store), so the notice
 *  points at the repair rather than guessing at one. The archived/kept
 *  sentences carry a self-closing `<history/>` tag the roster renders as a
 *  link to the History pane (its label is `fired_history_link`) -- "History"
 *  is not on this surface, so a bare word would leave the reader to find it. */
export function firedOutcomeKey(result: FireMemberResult): string {
  const thread = result.thread.state
  if (thread === 'archived') return 'pages.membersPage.fired_thread_archived'
  if (thread === 'purged') return 'pages.membersPage.fired_thread_purged'
  if (thread === 'kept') {
    const why = result.thread.kept_reason
    if (why === 'cron_claim_unreadable') return 'pages.membersPage.fired_thread_kept_cron_claim'
    if (why === 'store_unreadable') return 'pages.membersPage.fired_thread_kept_store'
    return 'pages.membersPage.fired_thread_kept'
  }
  return result.lived_state === 'purged'
    ? 'pages.membersPage.fired_thread_none_purged'
    : 'pages.membersPage.fired_thread_none'
}

/** The one sentence the roster shows once the member is gone, as plain text
 *  (the `<history/>` tag replaced by the link's label) -- what a screen reader
 *  announces and what tests compare. */
export function firedOutcomeText(
  label: string,
  result: FireMemberResult,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  const where = t(firedOutcomeKey(result)).replace(/<history\s*\/>/g, t('pages.membersPage.fired_history_link'))
  return `${t('pages.membersPage.fired_summary', { name: label })} ${where}`
}

/** Who a fire is FOR, fixed the moment the confirm button is pressed: the
 *  member and label of that render, and the completion handler that closes
 *  over them. The mutation callbacks read these, never the panel's current
 *  props -- the user can select another member while the request is in
 *  flight, and a completion read off the current props would evict and
 *  announce THAT member as fired while the server deleted the first. */
interface FireRequest {
  name: string
  label: string
  purge: boolean
  onFired: FiredHandler
}

export type FiredHandler = (result: FireMemberResult, fired: { name: string; label: string }) => void

/** The structured half of a 409 `slug_collision` from the fire route, or null
 *  for any other failure (a body without it keeps the server's sentence). */
export function slugCollisionOf(err: unknown): { member: string; slug: string } | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  try {
    const parsed = JSON.parse(err.body) as { code?: unknown; collision?: { member?: unknown; slug?: unknown } }
    if (parsed.code !== 'slug_collision') return null
    const member = parsed.collision?.member
    const slug = parsed.collision?.slug
    return typeof member === 'string' && typeof slug === 'string' ? { member, slug } : null
  } catch {
    return null
  }
}

export default function FirePanel({
  member,
  label,
  onFired,
}: {
  member: MemberRosterRow
  label: string
  onFired: FiredHandler
}) {
  const { t } = useTranslation()
  const [confirming, setConfirming] = useState(false)
  const [purge, setPurge] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setConfirming(false)
    setPurge(false)
    setError(null)
  }, [member.name])

  const fireMut = useMutation({
    mutationFn: (req: FireRequest) => api.fireMember(req.name, { purge: req.purge }),
    onSuccess: (result, req) => {
      if (req.name === member.name) setError(null)
      req.onFired(result, { name: req.name, label: req.label })
    },
    onError: (err: unknown, req) => {
      // A refusal is shown beside the member it concerns; one for a member
      // no longer in view has nothing truthful to attach to here (that
      // member is still on the roster, unfired, and its panel starts clean).
      if (req.name !== member.name) return
      // A shared short name is said in the user's language, with the labels the
      // navigation shows (the server's sentence names the same places in English).
      const collision = slugCollisionOf(err)
      if (collision) {
        setError(t('pages.membersPage.fire_slug_collision', { name: req.label, other: collision.member, slug: collision.slug }))
        return
      }
      setError(err instanceof Error ? err.message : t('pages.membersPage.fire_failed'))
    },
  })
  // "Firing…" belongs to the member whose fire is in flight, not to whichever
  // member the panel shows; the buttons stay disabled for any in-flight fire
  // so a second one cannot start under it.
  const firingThis = fireMut.isPending && fireMut.variables?.name === member.name

  return (
    <div className="mt-3 flex flex-col gap-2 rounded-md border border-danger-subtle bg-danger-subtle px-2.5 py-2 text-[12.5px] text-text" data-testid="member-fire">
      {/* Decision-critical copy at body tone, not the panel's muted weight. The
          confirm sentence follows the purge tick: with it on, the copy says
          what the purge does -- never "archived, not deleted" beside a box
          that says the opposite. */}
      <p className="m-0 leading-relaxed" data-testid="member-fire-explain">
        {confirming
          ? purge
            ? t('pages.membersPage.fire_confirm_explain_purge', { name: label })
            : t('pages.membersPage.fire_confirm_explain', { name: label })
          : t('pages.membersPage.fire_explain')}
      </p>
      {confirming && (
        <label className="flex items-start gap-2 text-[12.5px]">
          <input
            type="checkbox"
            checked={purge}
            onChange={(e) => setPurge(e.target.checked)}
            className="mt-0.5"
            aria-label={t('pages.membersPage.fire_purge_label')}
            data-testid="member-fire-purge"
          />
          <span>{t('pages.membersPage.fire_purge_label')}</span>
        </label>
      )}
      {/* No hand-off (askAgent={false}): the panel holds unsaved state -- the
          purge checkbox the user ticked and the open confirm step -- and Ask
          Agent navigates to a chat, unmounting the panel and losing that
          destructive selection with it. The member is still in view and the
          retry is the same button. */}
      <ErrorNotice message={error} variant="inline" askAgent={false} testId="member-fire-error" />
      <div className="flex items-center justify-end gap-2">
        {confirming ? (
          <>
            <Btn onClick={() => setConfirming(false)} disabled={fireMut.isPending} data-testid="cancel-fire-member">
              {t('components.confirmDialog.cancel')}
            </Btn>
            <Btn
              danger
              disabled={fireMut.isPending}
              onClick={() => fireMut.mutate({ name: member.name, label, purge, onFired })}
              data-testid="confirm-fire-member"
            >
              {firingThis ? t('pages.membersPage.firing') : purge ? t('pages.membersPage.fire_confirm_purge') : t('pages.membersPage.fire_confirm')}
            </Btn>
          </>
        ) : (
          <Btn danger onClick={() => setConfirming(true)} data-testid="member-fire-start">
            {t('pages.membersPage.fire', { name: label })}
          </Btn>
        )}
      </div>
    </div>
  )
}
