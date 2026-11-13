import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Puzzle } from 'lucide-react'

import { api, ApiError, type AgentPluginRecord, type AgentPluginsResponse } from '../../api/client'
import { Badge, Btn, Card, CardTitle, ContentSkeleton, EmptyState } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { parseErrorCode, reportForError, type ErrorReport } from '../../utils/errorReport'
import { queryReadFailed } from '../../utils/queryReadFailed'

/**
 * Customize > Kiro Powers: every Power and Agent Plugin installed for Kiro, as
 * `GET /api/agent-plugins` reports it. Read-only. Kiro Crew loads none of them
 * yet, so every row says "Not loaded" and why; there is no switch here, because
 * turning a plugin on is held (`docs/system-specs/modules/agent-plugins.md` §6).
 *
 * A read that failed on this host (the route marks it `failed`) is an error and
 * shows as an `ErrorNotice` the agent can take over; a rule a plugin's files
 * break is the loader's verdict and stays plain text. The list holds no draft,
 * so every hand-off here loses nothing.
 */

function ownerOnly(error: unknown): boolean {
  return error instanceof ApiError && parseErrorCode(error.body) === 'dashboard_owner_required'
}

/** A non-owner's 403, with the journal entry the client recorded for it. The
 *  notice words the refusal in the owner-only sentence, which is not that
 *  entry's message, so the entry travels here for the agent hand-off. */
interface OwnerRefusal {
  refused: true
  report: ErrorReport | undefined
}

/** The list, or the owner's refusal. A refusal is an answer that no retry
 *  changes, so it is cached as data rather than as an error: a re-read of a
 *  query with no data clears `error`, which would turn the owner-only notice
 *  into a Retry while the read runs. */
async function readPowers(): Promise<AgentPluginsResponse | OwnerRefusal> {
  try {
    return await api.agentPlugins()
  } catch (e) {
    if (ownerOnly(e)) return { refused: true, report: reportForError(e) }
    throw e
  }
}

function PowerRow({ rec }: { rec: AgentPluginRecord }) {
  const { t } = useTranslation()
  const reason = rec.legacy_power
    ? t('pages.overview.kiroPowersTab.reason_legacy_power')
    : rec.state === 'not-a-plugin'
      ? t('pages.overview.kiroPowersTab.reason_no_manifest')
      : rec.state === 'invalid'
        ? t('pages.overview.kiroPowersTab.reason_invalid', { reason: rec.reason ?? '' })
        : t('pages.overview.kiroPowersTab.reason_valid')
  // A failed record's `plugin` issue repeats its reason, which the record's own
  // notice already shows.
  const failures = rec.issues.filter(issue => issue.failed && !(rec.failed && issue.component === 'plugin'))
  // An invalid record's `plugin` issue is its loader reason, already shown above.
  const problems = rec.issues.filter(
    issue => !issue.failed && !(rec.state === 'invalid' && issue.component === 'plugin'),
  )
  return (
    <Card className="flex flex-col gap-2" data-testid="kiro-power-row">
      <div className="flex items-center gap-2 flex-wrap">
        <CardTitle className="font-mono">{rec.name}</CardTitle>
        {rec.version && <span className="text-[13px] text-muted font-mono">{rec.version}</span>}
        <Badge variant="muted" data-testid="kiro-power-not-loaded">{t('pages.overview.kiroPowersTab.not_loaded')}</Badge>
      </div>
      {rec.description && <p className="text-[13px]">{rec.description}</p>}
      {rec.failed ? (
        <ErrorNotice
          message={t('pages.overview.kiroPowersTab.reason_read_failed', { name: rec.name, reason: rec.reason ?? '' })}
          askAgent
          testId="kiro-power-read-failed"
        />
      ) : (
        <p className="text-[13px] text-muted" data-testid="kiro-power-reason">{reason}</p>
      )}
      {rec.skills.length > 0 && (
        <div className="text-[13px]">
          <div className="text-muted">{t('pages.overview.kiroPowersTab.would_add_skills')}</div>
          <ul className="list-disc pl-5">
            {rec.skills.map(s => <li key={s.name}><span className="font-mono">{s.name}</span></li>)}
          </ul>
        </div>
      )}
      {rec.servers.length > 0 && (
        <div className="text-[13px]">
          <div className="text-muted">{t('pages.overview.kiroPowersTab.would_add_servers')}</div>
          <ul className="list-disc pl-5">
            {rec.servers.map(s => (
              <li key={s.name}>
                <span className="font-mono">{s.name}</span> <span className="text-muted font-mono">{s.type}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {failures.map((issue, i) => (
        <ErrorNotice
          key={i}
          message={issue.entry
            ? t('pages.overview.kiroPowersTab.issue_read_failed_entry', { name: rec.name, entry: issue.entry, message: issue.message })
            : t('pages.overview.kiroPowersTab.issue_read_failed', { name: rec.name, message: issue.message })}
          askAgent
          testId="kiro-power-issue-read-failed"
        />
      ))}
      {problems.length > 0 && (
        <div className="text-[13px]">
          <div className="text-muted">{t('pages.overview.kiroPowersTab.problems_found')}</div>
          <ul className="list-disc pl-5">
            {problems.map((issue, i) => (
              <li key={i}>
                {issue.entry && <span className="font-mono">{issue.entry}</span>} {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}

export default function KiroPowersTab() {
  const { t } = useTranslation()
  // Kiro IDE and kiro-cli change this tree, and nothing tells the dashboard, so
  // every visit to the tab reads it again (the client default never goes stale).
  const query = useQuery({ queryKey: ['agent-plugins'], queryFn: readPowers, refetchOnMount: 'always' })
  const { data, isPending, isFetching, error, refetch } = query
  const failed = queryReadFailed(query)
  const refusal = data && 'refused' in data ? data : undefined
  const list = data && !('refused' in data) ? data : undefined
  const plugins = list?.plugins ?? []
  return (
    <div className="flex flex-col gap-3" data-testid="kiro-powers-tab">
      <p className="text-[13px] text-muted">{t('pages.overview.kiroPowersTab.intro')}</p>
      {isPending && !failed && <ContentSkeleton rows={3} />}
      {/* The refusal is cached data, never `failed`, so it gets no Retry: only
          the owner's session can read this list. The list holds no draft, so
          the agent hand-off loses nothing. */}
      {refusal && (
        <ErrorNotice
          message={t('pages.overview.kiroPowersTab.owner_only')}
          report={refusal.report}
          askAgent
          testId="kiro-powers-owner-only"
        />
      )}
      {/* queryReadFailed, not `error`: a retry with no cached list clears
          `error` while it reads, which would unmount the Retry just pressed.
          The list holds no draft, so the agent hand-off loses nothing. */}
      {failed && (
        <div className="flex flex-col items-start gap-2">
          {error && <ErrorNotice title={t('pages.overview.kiroPowersTab.failed_to_load')} message={error.message} askAgent />}
          <Btn
            className="aria-disabled:opacity-30 aria-disabled:cursor-default"
            aria-disabled={isFetching || undefined}
            onClick={() => { if (!isFetching) void refetch() }}
          >
            {t('pages.overview.kiroPowersTab.retry')}
          </Btn>
        </div>
      )}
      {/* Nothing could be listed, which is not the same as nothing installed,
          so the empty state stays away. */}
      {list?.index_error && (
        <ErrorNotice
          message={t('pages.overview.kiroPowersTab.index_read_failed', { reason: list.index_error })}
          askAgent
          testId="kiro-powers-index-read-failed"
        />
      )}
      {list && !list.index_error && plugins.length === 0 && (
        <EmptyState
          icon={<Puzzle className="lucide-inline" />}
          title={t('pages.overview.kiroPowersTab.empty_title')}
          subtitle={t('pages.overview.kiroPowersTab.empty_subtitle')}
          testId="kiro-powers-empty"
        />
      )}
      {plugins.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label={t('pages.overview.kiroPowersTab.list_label')}>
          {plugins.map(rec => <li key={rec.name}><PowerRow rec={rec} /></li>)}
        </ul>
      )}
    </div>
  )
}
