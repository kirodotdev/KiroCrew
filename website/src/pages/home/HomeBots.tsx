/**
 * Home's Bots roster: each crewmate with whether it is mid-turn and when it was
 * last active, opening its thread on the Bots page. Reads the Bots page's own
 * roster query, so the two render one cache.
 */
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Bot } from 'lucide-react'
import { membersRosterQuery } from '../../api/membersQuery'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge, Card, EmptyState, PanelSectionHeader } from '../../components/ui'
import { fmtNumber, fmtRelative } from '../../i18n/format'
import { botCounts } from './homeModel'
import { ListRow, RowLink } from './homeRows'

export default function HomeBots() {
  const { t } = useTranslation()
  const roster = useQuery(membersRosterQuery)
  const rows = roster.data ?? []
  const counts = botCounts(rows)
  // Working crewmates first, then the most recently active.
  const ordered = [...rows].sort((a, b) => Number(b.running) - Number(a.running) || (b.last_active_ts ?? 0) - (a.last_active_ts ?? 0))
  return <section aria-label={t('nav.crew_members')} className="space-y-3" data-testid="home-bots">
    <PanelSectionHeader label={t('nav.crew_members')} count={roster.data ? t('pages.homePage.bots_count', { running: fmtNumber(counts.running), total: fmtNumber(counts.total) }) : undefined}
      trailing={<RowLink to="/members">{t('pages.homePage.open_page')}</RowLink>} />
    <Card className="py-1">
      {/* No hand-off: the Needs-you cards on this page hold unsent answer drafts. */}
      <ErrorNotice message={roster.isError ? t('pages.homePage.bots_unavailable') : undefined} testId="home-bots-error" />
      {roster.data && !rows.length && <EmptyState icon={<Bot size={24} />} title={t('pages.membersPage.empty_title')} testId="home-bots-empty" />}
      {ordered.length > 0 && <ul className="list-none m-0 p-0">
        {ordered.map(row => (
          <ListRow key={row.slug} data-testid="home-bot-row" data-running={row.running ? 'true' : 'false'}>
            <CrewStateAvatar seed={row.name} avatar={row.avatar} slotKey={row.slot_key} running={row.running} size={28} working="subtle" className="shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium truncate">{crewDisplayName(row)}</div>
              {row.last_active_ts != null && <div className="text-[12px] text-muted truncate">{t('pages.homePage.last_active', { when: fmtRelative(row.last_active_ts * 1000) })}</div>}
            </div>
            <Badge variant={row.running ? 'aim' : 'muted'} className="shrink-0">{t(row.running ? 'commandCenter.running' : 'commandCenter.idle')}</Badge>
            <RowLink to={`/members?member=${encodeURIComponent(row.name)}`}>{t('pages.homePage.open')}</RowLink>
          </ListRow>
        ))}
      </ul>}
    </Card>
  </section>
}
