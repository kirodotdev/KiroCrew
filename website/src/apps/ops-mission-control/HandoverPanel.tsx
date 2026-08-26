/**
 * HandoverPanel — the shift handover digest.
 *
 * Hand-maintained handover documents are among the most-used artifacts a rotation has:
 * at shift change nobody wants a list of every incident, they want the few things that
 * will actually page them and what to do about each. Such a document costs hours of
 * upkeep and goes stale between edits; everything generic in it is already data this app
 * owns.
 *
 * Design choices worth keeping:
 *
 * - **The headline is the product.** Someone reading one line must get the right
 *   priority, so the backend composes it (no coverage → work waiting on you → the
 *   ordinary case) and this renders it prominently rather than re-deriving it.
 * - **Blind spots are shown, not just coverage.** An unconfigured source is silence
 *   that looks like health, and the incoming responder inherits it.
 * - **Unproven patterns are visibly unproven, and refuted ones are visibly refuted.**
 *   Flattening `observed/medium` into "the fix" is how a digest gets someone to apply the
 *   wrong thing confidently — and a fix that was applied while the signal kept firing is
 *   a third state, worse than unproven, on the entry this list ranks FIRST because the
 *   failure keeps recurring.
 * - **Copy-as-text is first-class.** The backend also returns a rendered text form, so
 *   pasting into a handover thread and reading it here cannot word things differently.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  ClipboardCopy,
  Clock,
  Radio,
  RefreshCw,
  ShieldCheck,
  UserCheck,
} from 'lucide-react'
import { Badge, Btn, Card, CardTitle, EmptyState } from '../../components/ui'
import {
  blockedLabel,
  describeSourceHealth,
  opsApi,
  SIGNALS_QUERY_KEY,
  type HandoverIncident,
} from './api'

import { i18nT } from '../../i18n/t'
/** Module-level frozen empty so render-time fallbacks stay referentially stable. */
const EMPTY_ROWS: readonly HandoverIncident[] = Object.freeze([])

function IncidentList({
  rows,
  emphasis,
}: {
  rows: readonly HandoverIncident[]
  emphasis?: boolean
}) {
  return (
    <ul className="flex flex-col divide-y divide-border">
      {rows.map((row) => (
        <li key={row.id} className="flex items-center gap-2 py-2 text-[13px]">
          <span className="font-mono text-[12px] text-muted shrink-0">{row.id}</span>
          <span className="truncate flex-1" title={row.title}>
            {row.title}
          </span>
          {row.blocked_reason ? (
            <span className={`text-[12px] shrink-0 ${emphasis ? 'text-warn font-medium' : 'text-muted'}`}>
              {blockedLabel(row.blocked_reason)}
            </span>
          ) : null}
          {/* Only when a person claimed it. The heartbeat is the overwhelming majority,
              so labelling it would be noise on every row; `''` (claimed before the field
              existed) renders nothing rather than guessing a path. */}
          {row.claimed_by === 'operator' ? (
            <span className="text-[12px] text-muted shrink-0" title={i18nT('apps.opsMissionControl.handoverPanel.claimed_by_hand_not_the_heartbeat')}>
              {i18nT('apps.opsMissionControl.handoverPanel.claimed_by_hand')}
            </span>
          ) : null}
          <span className="text-[12px] text-muted shrink-0 hidden lg:inline">{row.source}</span>
        </li>
      ))}
    </ul>
  )
}

export default function HandoverPanel() {
  const [copied, setCopied] = useState(false)

  // No refetchInterval: a handover is read at shift change, not watched. It is
  // computed fresh per request, so the Refresh button is the honest control.
  const query = useQuery({
    queryKey: ['ops-mission-control', 'handover'],
    queryFn: () => opsApi.handover(),
  })

  // The digest's coverage is derived from `configured` alone (handover.coverage), which is
  // the same look-deliberate-do-nothing shape this app exists to prevent: a source whose
  // every poll fails is listed under "Watching". The last explicit poll is the only thing
  // that knows better, so read its cached result — `enabled: false` and the SAME key the
  // Signals tab owns, because a digest read at shift change must not fire a paid poll of
  // every provider as a side effect of opening a tab.
  const cachedSignalsQuery = useQuery({
    queryKey: SIGNALS_QUERY_KEY,
    queryFn: () => opsApi.signals(),
    enabled: false,
  })

  // Cheap catalog read (a config check per adapter, no provider calls). Needed because
  // coverage names sources by display_name while poll_health is keyed by id.
  const providersQuery = useQuery({
    queryKey: ['ops-mission-control', 'providers'],
    queryFn: () => opsApi.providers(),
  })

  const digest = query.data
  const cachedSignals = cachedSignalsQuery.data

  /** Display names of signal sources whose last poll failed or is throttled. */
  const notAnswering = new Set(
    cachedSignals
      ? (providersQuery.data?.providers ?? [])
          .filter((p) => p.roles.includes('signal'))
          .filter((p) => {
            const state = describeSourceHealth(
              p.id,
              cachedSignals.poll_health,
              cachedSignals.errors,
              p.configured,
            ).state
            return state === 'failed' || state === 'backing_off'
          })
          .map((p) => p.display_name)
      : [],
  )
  const work = digest?.open_work
  const waiting = work?.waiting_on_you ?? EMPTY_ROWS
  const stalled = work?.stalled_without_diagnosis ?? EMPTY_ROWS
  const escalated = work?.escalated ?? EMPTY_ROWS
  const patterns = digest?.recurring_patterns ?? []

  const copy = async () => {
    if (!digest?.text || typeof navigator === 'undefined' || !navigator.clipboard) return
    try {
      await navigator.clipboard.writeText(digest.text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard blocked (permissions, insecure context) — the text is on screen */
    }
  }

  if (query.isLoading) {
    return (
      <Card>
        <p className="text-sm text-muted">{i18nT('apps.opsMissionControl.handoverPanel.building_the_digest')}</p>
      </Card>
    )
  }
  if (query.isError) {
    return (
      <Card>
        <p className="text-[13px] text-danger">{(query.error as Error).message}</p>
      </Card>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardTitle>
          <Clock className="lucide-inline" /> {i18nT('apps.opsMissionControl.handoverPanel.handover')}
        </CardTitle>
        {/* The one line someone gets if they read nothing else. */}
        <p className="text-sm mb-3">{digest?.headline}</p>
        <div className="flex items-center gap-2">
          <Btn disabled={query.isFetching} onClick={() => query.refetch()}>
            <RefreshCw className="lucide-inline" />{' '}
            {query.isFetching
              ? i18nT('apps.opsMissionControl.handoverPanel.refreshing')
              : i18nT('apps.opsMissionControl.handoverPanel.refresh')}
          </Btn>
          <Btn onClick={copy} title={i18nT('apps.opsMissionControl.handoverPanel.copy_the_digest_as_text_for_a_handover_thread')}>
            {copied ? <Check className="lucide-inline" /> : <ClipboardCopy className="lucide-inline" />}{' '}
            {copied
              ? i18nT('apps.opsMissionControl.handoverPanel.copied')
              : i18nT('apps.opsMissionControl.handoverPanel.copy_as_text')}
          </Btn>
          {digest?.autonomy?.mode ? (
            <Badge variant={digest.autonomy.mode === 'observe' ? 'ok' : 'warn'}>
              <ShieldCheck className="lucide-inline" /> {digest.autonomy.mode}
            </Badge>
          ) : null}
        </div>
      </Card>

      {waiting.length > 0 ? (
        <Card>
          <CardTitle>
            <UserCheck className="lucide-inline" /> {i18nT('apps.opsMissionControl.handoverPanel.waiting_on_you')}
          </CardTitle>
          <p className="text-[13px] text-muted mb-2">
            {i18nT('apps.opsMissionControl.handoverPanel.these_do_not_progress_on_their_own_an_unanswered')}
          </p>
          <IncidentList rows={waiting} emphasis />
        </Card>
      ) : null}

      {stalled.length > 0 ? (
        <Card>
          <CardTitle>
            <AlertTriangle className="lucide-inline" /> {i18nT('apps.opsMissionControl.handoverPanel.stopped_with_no_diagnosis')}
          </CardTitle>
          <p className="text-[13px] text-muted mb-2">
            {i18nT('apps.opsMissionControl.handoverPanel.the_investigation_ended_without_recording_anythi')}
          </p>
          <IncidentList rows={stalled} />
        </Card>
      ) : null}

      {escalated.length > 0 ? (
        <Card>
          <CardTitle>{i18nT('apps.opsMissionControl.handoverPanel.escalated_to_someone_else')}</CardTitle>
          <p className="text-[13px] text-muted mb-2">
            {i18nT('apps.opsMissionControl.handoverPanel.handed_to_another_owner_so_no_longer_this_app_s')}
          </p>
          <IncidentList rows={escalated} />
        </Card>
      ) : null}

      <Card>
        <CardTitle>{i18nT('apps.opsMissionControl.handoverPanel.what_keeps_happening')}</CardTitle>
        {patterns.length === 0 ? (
          <EmptyState
            icon={<Radio className="lucide-inline" />}
            title={i18nT('apps.opsMissionControl.handoverPanel.no_recurring_patterns_yet')}
            subtitle={i18nT('apps.opsMissionControl.handoverPanel.a_pattern_appears_here_once_the_same_failure_has')}
          />
        ) : (
          <ul className="flex flex-col divide-y divide-border mt-1">
            {patterns.map((pat) => (
              <li key={pat.pattern} className="py-2">
                <div className="flex items-start gap-2 text-[13px]">
                  <span className="font-mono text-[12px] text-muted shrink-0 w-8 text-right">
                    {pat.uses}×
                  </span>
                  <span className="flex-1">{pat.pattern}</span>
                  {/* An unproven entry must not look like an answer, and a REFUTED one
                      must not look merely unproven. Three states, worst first: this list
                      is ranked by how often the failure recurs, so the entry most likely
                      to be reached for is the one at the top — and "this fix has already
                      failed twice" is precisely what the ranking would otherwise hide. */}
                  {pat.demoted ? (
                    <Badge
                      variant="warn"
                      title={i18nT('apps.opsMissionControl.handoverPanel.applied_and_the_signal_kept_firing_afterwards')}
                    >
                      {i18nT('apps.opsMissionControl.handoverPanel.failed')} {pat.misses}&times;
                    </Badge>
                  ) : (
                    <Badge variant={pat.proven ? 'ok' : 'muted'}>
                      {pat.proven
                        ? i18nT('apps.opsMissionControl.handoverPanel.pattern_proven')
                        : `${pat.confidence}/${pat.trust}`}
                    </Badge>
                  )}
                </div>
                <p className="text-[12px] text-muted mt-1 pl-10">{pat.fix}</p>
                {pat.demoted ? (
                  <p className="text-[12px] text-warn mt-1 pl-10">
                    {i18nT('apps.opsMissionControl.handoverPanel.this_fix_was_applied_and_the_signal_was_still_fi')}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardTitle>{i18nT('apps.opsMissionControl.handoverPanel.coverage')}</CardTitle>
        {/* Configured is not the same as working. Each name is marked when the last poll
            says that source is not answering, because the incoming responder must not
            inherit a blind spot dressed up as coverage — "Watching: cloudwatch" beside a
            provider whose every poll 401s is the worst sentence this panel could hand over.
            The backend-rendered digest.text is deliberately left alone: it is what gets
            pasted into the handover thread, and editorialising here would let the paste and
            the screen word things differently. */}
        <p className="text-[13px]">
          {/* One interpolated sentence, not a "Watching:" label beside a list. A bare
              colon-terminated label is untranslatable in isolation (it "ends mid-sentence"),
              and per-name styling cannot live in a catalog string — so the names are
              pre-joined into the interpolation, with any "not answering" marker folded into
              the joined text. The whole phrase is one key a translator can reorder. */}
          {digest?.coverage?.watching?.length
            ? i18nT('apps.opsMissionControl.handoverPanel.watching_sources', {
                sources: digest.coverage.watching
                  .map((name) =>
                    notAnswering.has(name)
                      ? i18nT('apps.opsMissionControl.handoverPanel.source_not_answering', { name })
                      : name,
                  )
                  .join(', '),
              })
            : i18nT('apps.opsMissionControl.handoverPanel.watching_nothing')}
        </p>
        {digest?.coverage?.not_configured?.length ? (
          <p className="text-[13px] text-muted mt-1">
            {i18nT('apps.opsMissionControl.handoverPanel.not_configured_blind_spots', {
              sources: digest.coverage.not_configured.join(', '),
            })}
          </p>
        ) : null}
        {!digest?.coverage?.any_watching ? (
          <p className="text-[13px] text-warn mt-2 flex items-start gap-1.5">
            <AlertTriangle className="lucide-inline" />
            <span>
              {i18nT('apps.opsMissionControl.handoverPanel.nothing_is_being_watched_so_a_quiet_board_means')}
            </span>
          </p>
        ) : null}
        {/* Say which question this list actually answered. Without a poll it is a config
            listing, and presenting it plainly would imply it had been checked. */}
        {digest?.coverage?.any_watching ? (
          !cachedSignals ? (
            <p className="text-[13px] text-muted mt-2">
              {i18nT('apps.opsMissionControl.handoverPanel.derived_from_configuration_only_no_source_has_be')}
            </p>
          ) : !cachedSignals.all_sources_healthy ? (
            <p className="text-[13px] text-warn mt-2 flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                {notAnswering.size > 0
                  ? i18nT('apps.opsMissionControl.handoverPanel.sources_did_not_answer_the_last_poll', {
                      sources: [...notAnswering].join(', '),
                    })
                  : i18nT('apps.opsMissionControl.handoverPanel.at_least_one_source_did_not_answer_the_last_poll')}{' '}
                {i18nT('apps.opsMissionControl.handoverPanel.until_that_is_fixed_a_quiet_board_is_not_evidenc')}
              </span>
            </p>
          ) : (
            <p className="text-[13px] text-ok mt-2">
              {i18nT('apps.opsMissionControl.handoverPanel.every_configured_source_answered_the_last_poll')}
            </p>
          )
        ) : null}
      </Card>
    </div>
  )
}
