import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { OrganizationMember, OrganizationSnapshot } from '../../api/organization'
import { PanelSectionHeader } from '../../components/ui'
import { ORGANIZATION_LABEL_KEYS } from '../../i18n/organizationLabels'

/** Organization work runs in each persistent member's own conversation. */
export default function OrganizationMemberWork({
  member, data,
}: { member: OrganizationMember; data: OrganizationSnapshot }) {
  const { t } = useTranslation()
  const reports = data.members.filter(report => report.manager_id === member.id && report.state === 'active')
  const assignments = data.tasks.filter(task => task.recipient === member.id || task.sender === member.id).slice(0, 5)
  const stateLabel = (state: string) => {
    const catalogKey = ORGANIZATION_LABEL_KEYS[state]
    return catalogKey ? t(catalogKey) : state
  }
  const runState = (id: string) => {
    const runs = data.runs.filter(run => run.member_id === id)
    const state = (runs.find(run => run.state === 'running')
      || runs.find(run => run.state !== 'coalesced'))?.state
    if (!state) return t('pages.membersPage.driving_idle')
    return stateLabel(state === 'running' ? 'working' : state)
  }
  const name = (id: string) => data.members.find(person => person.id === id)?.name || id

  return <div className="mb-4 space-y-2" data-testid="member-organization-work">
    <PanelSectionHeader label={t('organization.work')} />
    <p className="text-[11px] text-muted">{t('organization.memberExecution')}</p>
    <p className="text-[11px]">{t('organization.latestTurn')}: {runState(member.id)}</p>
    {reports.length > 0 && <ul className="list-none m-0 p-0 space-y-1">
      {reports.map(report => <li key={report.id}>
        <Link to={`/members?member=${encodeURIComponent(report.name)}`}
          className="flex items-center justify-between gap-2 rounded px-1.5 py-1 text-[11px] hover:bg-accent/40">
          <span className="min-w-0 truncate">{report.name}</span>
          <span className="shrink-0 text-muted">{runState(report.id)}</span>
        </Link>
      </li>)}
    </ul>}
    {assignments.length > 0 && <ul className="list-none m-0 p-0 space-y-2">
      {assignments.map(task => <li key={task.id} className="text-[11px]">
        <div className="break-words">{task.title}</div>
        <div className="text-muted">{name(task.recipient)} · {stateLabel(task.state)}</div>
      </li>)}
    </ul>}
    <Link to="/capabilities?tab=organization" className="inline-block text-[11px] text-accent hover:underline">
      {t('organization.title')}
    </Link>
  </div>
}
