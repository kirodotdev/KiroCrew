import { useEffect, useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { GitBranch, MessageSquare, MoreHorizontal, Pause, Play, Plus, ShieldCheck, Users } from 'lucide-react'
import { organizationAction, organizationQuery, type OrganizationMember, type OrganizationRole, type OrganizationSnapshot } from '../api/organization'
import Clickable from '../components/Clickable'
import { useConfirm } from '../components/ConfirmDialog'
import ErrorNotice from '../components/ErrorNotice'
import SimpleSelect from '../components/SimpleSelect'
import { useSidePanelLeaveGuard } from '../components/SidePanelLayout'
import { Btn, Card, Input, PageHeader, PanelSectionHeader } from '../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../components/ui/tabsPill'
import { fmtNumber } from '../i18n/format'
import { ORGANIZATION_LABEL_KEYS } from '../i18n/organizationLabels'

const ROLES: OrganizationRole[] = ['conductor', 'manager', 'engineer', 'researcher']
const ROLE_POLICY_KEYS = {
  conductor: 'organization.conductorPolicy',
  manager: 'organization.managerPolicy',
  engineer: 'organization.engineerPolicy',
  researcher: 'organization.researcherPolicy',
} as const
const FIELD = 'w-full rounded-lg border border-[var(--border)] bg-[var(--bg)] p-2 text-sm text-[var(--text)]'

export default function OrganizationPage({ memberName, embedded = false, onDirtyChange }: { memberName?: string; embedded?: boolean; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useTranslation()
  const client = useQueryClient()
  const query = useQuery(organizationQuery)
  const { confirm, confirmDialog } = useConfirm()
  const [selectedId, setSelectedId] = useState('')
  const tabId = useId()
  const [name, setName] = useState('')
  const [role, setRole] = useState<OrganizationRole>('engineer')
  const [title, setTitle] = useState('')
  const [acceptance, setAcceptance] = useState('')
  const [note, setNote] = useState('')
  const [decisions, setDecisions] = useState<Record<string, string>>({})
  const [capacity, setCapacity] = useState<number | null>(null)
  const [staffing, setStaffing] = useState<OrganizationSnapshot['settings']['staffing'] | null>(null)
  const mutate = useMutation({
    mutationFn: ({ action, values }: { action: string; values?: Record<string, unknown> }) =>
      organizationAction(action, values),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: organizationQuery.queryKey }),
        client.invalidateQueries({ queryKey: ['kirocrew-agents'] }),
      ])
    },
  })
  const setup = useMutation({
    mutationFn: async () => {
      const root = await organizationAction('create_member', { role: 'conductor' })
      const manager = await organizationAction('create_member', { role: 'manager', manager_id: root.member_id })
      await organizationAction('create_member', { role: 'engineer', manager_id: manager.member_id })
      await organizationAction('create_member', { role: 'researcher', manager_id: root.member_id })
      setSelectedId(root.member_id || '')
    },
    onSettled: async () => {
      await client.invalidateQueries({ queryKey: organizationQuery.queryKey })
      await client.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    },
  })
  const data = query.data
  const displayedRuns = data?.runs.filter(run => ['failed', 'interrupted'].includes(run.state)).slice(0, 10) || []
  const settingsDirty = data && (
    (capacity !== null && capacity !== data.settings.concurrency)
    || (staffing !== null && ROLES.some(manager => ROLES.some(report =>
      (staffing[manager][report] ?? 0) !== (data.settings.staffing[manager][report] ?? 0))))
  )
  const dirty = Boolean(name || role !== 'engineer' || title || acceptance || note
    || Object.values(decisions).some(Boolean) || settingsDirty)
  const mayLeave = () => !dirty || window.confirm(t('organization.discardChanges'))
  useSidePanelLeaveGuard(mayLeave, dirty)
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange])
  const active = data?.members.filter(member => member.state === 'active') || []
  const selected = data?.members.find(member => member.id === selectedId)
    || data?.members.find(member => member.name === memberName)
    || (!memberName ? active.find(member => !member.manager_id) : undefined)
  const busy = mutate.isPending || setup.isPending
  const memberLabel = (id: string | null) => id && id !== 'owner'
    ? data?.members.find(member => member.id === id)?.name || id
    : t('organization.human')
  const label = (key: string) => {
    const catalogKey = ORGANIZATION_LABEL_KEYS[key]
    return catalogKey ? t(catalogKey) : key
  }
  const send = (action: string, values?: Record<string, unknown>) => mutate.mutateAsync({ action, values })
  const settingsValues = () => ({
    revision: data!.settings.revision,
    enabled: data!.settings.enabled,
    concurrency: capacity ?? data!.settings.concurrency,
    staffing: staffing ?? data!.settings.staffing,
  })
  const error = query.error || mutate.error || setup.error
  const reports = selected ? active.filter(member => member.manager_id === selected.id) : []
  const reportRoles = selected && data
    ? ROLES.filter(value => value !== 'conductor' && (data.settings.staffing[selected.role][value] ?? 0) > 0)
    : []
  const creationRole = reportRoles.includes(role) ? role : reportRoles[0]
  const review = (taskId: string, verdict: string) => {
    const text = decisions[taskId]
    void send('review', { task_id: taskId, verdict, text }).then(() => setDecisions(previous =>
      previous[taskId] === text ? { ...previous, [taskId]: '' } : previous)).catch(() => {})
  }
  const retire = async (member: OrganizationMember) => {
    if (!await confirm({
      title: t('organization.retireConfirm', { name: member.name }),
      body: t('organization.retireConsequences'),
      confirmLabel: t('organization.retire'),
    })) return
    await send('retire', { member_id: member.id })
  }

  function tree(member: OrganizationMember, depth = 0): React.ReactNode {
    const children = active.filter(child => child.manager_id === member.id)
    return <li key={member.id} className="min-w-0">
      <Clickable
        onClick={() => setSelectedId(member.id)}
        aria-pressed={selected?.id === member.id}
        className={`mb-3 flex items-center gap-3 rounded-xl border p-3 ${selected?.id === member.id ? 'border-[var(--accent)] bg-[var(--hover)]' : 'border-[var(--border)] bg-[var(--card)]'}`}
      >
        {member.role === 'conductor' || member.role === 'manager'
          ? <GitBranch className="lucide-inline shrink-0" />
          : <Users className="lucide-inline shrink-0" />}
        <div className="min-w-0 flex-1">
          <div className="break-words font-medium">{member.name}</div>
          <div className="text-[13px] text-[var(--muted)]">{label(member.role)}</div>
        </div>
        <span className="text-[12px] text-[var(--muted)]">{fmtNumber(children.length)}</span>
      </Clickable>
      {children.length > 0 && depth < 20 && <ul className="ml-3 border-l border-[var(--border)] pl-3 md:ml-5 md:pl-5">
        {children.map(child => tree(child, depth + 1))}
      </ul>}
    </li>
  }

  return <div className="flex min-h-0 min-w-0 flex-1 flex-col">
    {!memberName && !embedded && <PageHeader title={t('organization.title')} subtitle={t('organization.description')} />}
    <div className="min-w-0 space-y-4 px-4 pb-6 md:px-6">
      {/* No hand-off: forms below can contain unsaved staffing and task drafts. */}
      <ErrorNotice message={error instanceof Error ? error.message : ''} onDismiss={() => { mutate.reset(); setup.reset() }} />
      {!data ? <p className="text-[var(--muted)]">{t('organization.loading')}</p> : <>
        {memberName && <div className="space-y-2 text-sm text-[var(--muted)]">
          <p>{t('organization.teamScope')}</p>
          <p>{t('organization.memberSaveScope')}</p>
        </div>}
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-[var(--border)] p-3">
          <ShieldCheck className="lucide-inline" />
          <span className="flex-1 text-sm">{label(!data.settings.enabled ? 'paused' : data.runtime.ready ? 'running' : 'unavailable')}</span>
          <Btn disabled={busy || !active.length || (!data.settings.enabled && !data.runtime.ready)}
            onClick={() => { void send('configure', { ...data.settings, enabled: !data.settings.enabled }).catch(() => {}) }}>
            {data.settings.enabled ? <Pause className="lucide-inline" /> : <Play className="lucide-inline" />}
            {label(data.settings.enabled ? 'pause' : 'resume')}
          </Btn>
        </div>
        {!data.runtime.ready && <Card>
          <p className="font-medium">{t('organization.runtimeUnavailable')}</p>
          {/* No hand-off: member, staffing, task, message and review drafts below are unsaved. */}
          <ErrorNotice message={data.runtime.reason} className="mt-2" />
        </Card>}
        {memberName && !data.members.some(member => member.name === memberName) && <Card>
          <p className="text-sm text-[var(--muted)]">{t('organization.unmanagedMember')}</p>
        </Card>}
        <Tabs defaultValue="chart" layoutId={`organization-tabs-${tabId}`} className="space-y-4">
          <div className={TABS_RAIL_ROW_CLASS}>
            <TabsList aria-label={t('organization.title')}>
              {(['chart', 'guardrails', 'staffing', 'work'] as const).map(key =>
                <TabsTrigger key={key} value={key}>{label(key)}</TabsTrigger>)}
            </TabsList>
          </div>
        {!active.length && <Card className="space-y-3">
          <p>{t('organization.empty')}</p>
          <Btn disabled={busy || !data.runtime.ready} onClick={() => setup.mutate()}>
            <Plus className="lucide-inline" />{t('organization.createTeam')}
          </Btn>
        </Card>}
        <TabsContent value="chart">
        {active.length > 0 && <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.85fr)]">
          <Card className="min-w-0">
            <PanelSectionHeader label={t('organization.chart')} count={active.length} />
            <div className="mb-3 text-sm text-[var(--muted)]">{t('organization.human')}</div>
            <ul>{active.filter(member => !member.manager_id).map(member => tree(member))}</ul>
          </Card>
          {selected && <Card className="min-w-0 space-y-4">
            <PanelSectionHeader label={selected.name} />
            <p className="text-sm">{label(selected.role)} · {t('organization.reportsTo')}: {memberLabel(selected.manager_id)}</p>
            <p className="text-sm text-[var(--muted)]">{t('organization.privateMemory')}</p>
            {selected.manager_id && <label className="block space-y-1 text-sm">
              <span>{t('organization.reportsTo')}</span>
              <SimpleSelect value={selected.manager_id}
                options={active.filter(member => member.id !== selected.id && (member.role === 'conductor' || member.role === 'manager')).map(member => member.id)}
                optionLabels={active.filter(member => member.id !== selected.id && (member.role === 'conductor' || member.role === 'manager')).map(member => member.name)}
                onChange={managerId => { void send('move_member', { member_id: selected.id, manager_id: managerId }).catch(() => {}) }}
                disabled={busy} aria-label={t('organization.reportsTo')} />
            </label>}
            <div className="flex flex-wrap gap-2">
              <Link className="inline-flex items-center gap-2 text-sm text-[var(--accent)]" to={`/members?member=${encodeURIComponent(selected.name)}`}
                onClick={event => { if (!mayLeave()) event.preventDefault() }}>
                <MessageSquare className="lucide-inline" />{t('organization.chat')}
              </Link>
              <DropdownMenu>
                <DropdownMenuTrigger asChild><Btn disabled={busy} aria-label={t('organization.moreActions')}><MoreHorizontal className="lucide-inline" /></Btn></DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={() => { void send('retry', { member_id: selected.id }).catch(() => {}) }}>{t('organization.retry')}</DropdownMenuItem>
                  <DropdownMenuItem disabled={reports.length > 0} onSelect={() => { void retire(selected).catch(() => {}) }}>{t('organization.retire')}</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            {(selected.role === 'conductor' || selected.role === 'manager') && <form className="space-y-3"
              onSubmit={event => {
                event.preventDefault()
                void send('create_member', { role: creationRole, manager_id: selected.id, name }).then(() => {
                  setName(current => current === name ? '' : current)
                  setRole(current => current === role ? 'engineer' : current)
                }).catch(() => {})
              }}>
              <PanelSectionHeader label={t('organization.addMember')} />
              <label className="block space-y-1 text-sm">
                <span>{t('organization.name')}</span>
                <Input value={name} onChange={event => setName(event.target.value)} maxLength={128} />
              </label>
              <label className="block space-y-1 text-sm">
                <span>{t('organization.role')}</span>
                <SimpleSelect value={creationRole || ''} onChange={value => setRole(value as OrganizationRole)}
                  options={reportRoles}
                  optionLabels={reportRoles.map(label)}
                  aria-label={t('organization.role')} />
              </label>
              <Btn type="submit" disabled={busy || !creationRole}>{t('organization.create')}</Btn>
            </form>}
          </Card>}
        </div>}
        </TabsContent>
        <TabsContent value="guardrails"><Card className="space-y-4">
          <PanelSectionHeader label={t('organization.guardrails')} />
          <p className="text-sm text-[var(--muted)]">{t('organization.guardrailsDescription')}</p>
          {ROLES.map(value => <div key={value} className="border-t border-[var(--border)] pt-3">
            <h3 className="font-medium">{label(value)}</h3>
            <p className="mt-1 text-sm text-[var(--muted)]">{t(ROLE_POLICY_KEYS[value])}</p>
          </div>)}
        </Card></TabsContent>
        <TabsContent value="staffing"><Card>
          <form className="space-y-4" onSubmit={event => {
            event.preventDefault()
            void send('configure', settingsValues()).then(() => {
              setCapacity(current => current === capacity ? null : current)
              setStaffing(current => current === staffing ? null : current)
            }).catch(() => {})
          }}>
            <PanelSectionHeader label={t('organization.staffing')} />
            <p className="text-sm text-[var(--muted)]">{t('organization.staffingDescription')}</p>
            <label className="block space-y-1 text-sm">
              <span>{t('organization.capacity')}</span>
              <Input type="number" min={1} max={16} value={capacity ?? data.settings.concurrency}
                onChange={event => setCapacity(Number(event.target.value))} />
            </label>
            {(['conductor', 'manager'] as const).map(managerRole => <div key={managerRole} className="space-y-2">
              <PanelSectionHeader label={t('organization.perManager', { role: label(managerRole) })} />
              <div className="grid gap-3 sm:grid-cols-3">
                {(['manager', 'engineer', 'researcher'] as const).map(reportRole => <label key={reportRole} className="block space-y-1 text-sm">
                  <span>{label(reportRole)}</span>
                  <Input type="number" min={0} max={16}
                    value={(staffing ?? data.settings.staffing)[managerRole][reportRole] ?? 0}
                    onChange={event => setStaffing(previous => ({
                      ...(previous ?? data.settings.staffing),
                      [managerRole]: { ...(previous ?? data.settings.staffing)[managerRole], [reportRole]: Number(event.target.value) },
                    }))} />
                </label>)}
              </div>
            </div>)}
            <p className="text-sm text-[var(--muted)]">{t('organization.saveScope')}</p>
            <Btn type="submit" disabled={busy}>{t('organization.save')}</Btn>
          </form>
        </Card></TabsContent>
        <TabsContent value="work"><div className="grid min-w-0 gap-4 xl:grid-cols-2">
          <div className="space-y-4">
            {selected && <Card>
              <form className="space-y-3" onSubmit={event => {
                event.preventDefault()
                void send('assign', { recipient: selected.id, title, acceptance }).then(() => {
                  setTitle(current => current === title ? '' : current)
                  setAcceptance(current => current === acceptance ? '' : current)
                }).catch(() => {})
              }}>
                <PanelSectionHeader label={t('organization.assign')} />
                <SimpleSelect value={selected.id} options={active.map(member => member.id)}
                  optionLabels={active.map(member => member.name)} onChange={setSelectedId}
                  aria-label={t('organization.recipient')} />
                <label className="block space-y-1 text-sm"><span>{t('organization.taskTitle')}</span>
                  <Input className="block w-full" required value={title} maxLength={2000} onChange={event => setTitle(event.target.value)} /></label>
                <label className="block space-y-1 text-sm"><span>{t('organization.acceptance')}</span>
                  <textarea required aria-label={t('organization.acceptance')} className={FIELD} rows={3} maxLength={12000} value={acceptance} onChange={event => setAcceptance(event.target.value)} /></label>
                <Btn type="submit" disabled={busy || !selected}>{t('organization.assign')}</Btn>
              </form>
            </Card>}
            {selected && <Card>
              <form className="space-y-3" onSubmit={event => {
                event.preventDefault()
                void send('message', { recipient: selected.id, text: note })
                  .then(() => setNote(current => current === note ? '' : current)).catch(() => {})
              }}>
                <PanelSectionHeader label={t('organization.messageTo', { name: selected.name })} />
                <textarea required aria-label={t('organization.message')} className={FIELD} rows={3}
                  maxLength={12000} value={note} onChange={event => setNote(event.target.value)} />
                <Btn type="submit" disabled={busy}>{t('organization.send')}</Btn>
              </form>
            </Card>}
            {data.messages.slice(0, 20).map(message => <Card key={message.id}>
              <p className="text-[13px] text-[var(--muted)]">{memberLabel(message.sender)} → {memberLabel(message.recipient)}</p>
              <p className="mt-2 whitespace-pre-wrap break-words text-sm">{message.text}</p>
            </Card>)}
          </div>
          <div className="space-y-4" aria-live="polite">
            {!data.tasks.length && !displayedRuns.length && <Card>{t('organization.noWork')}</Card>}
            {data.tasks.map(task => <Card key={task.id} className="min-w-0 space-y-3">
              <PanelSectionHeader label={task.title} trailing={<span className="text-[12px]">{label(task.state)}</span>} />
              <p className="text-[13px] text-[var(--muted)]">{memberLabel(task.sender)} → {memberLabel(task.recipient)}</p>
              <p className="whitespace-pre-wrap break-words text-sm">{task.acceptance}</p>
              {task.report && <p className="whitespace-pre-wrap break-words border-l-2 border-[var(--accent)] pl-3 text-sm">{task.report}</p>}
              {task.sender === 'owner' && !['accepted', 'cancelled'].includes(task.state) && <div className="space-y-2">
                <label htmlFor={`organization-decision-${tabId}-${task.id}`} className="block text-sm">{t('organization.decision')}</label>
                <textarea id={`organization-decision-${tabId}-${task.id}`} aria-label={t('organization.decision')} required className={FIELD} rows={2} value={decisions[task.id] || ''}
                  onChange={event => setDecisions(previous => ({ ...previous, [task.id]: event.target.value }))} />
                <div className="flex flex-wrap gap-2">
                  <Btn disabled={busy || !decisions[task.id]?.trim() || task.state !== 'review'}
                    onClick={() => review(task.id, 'accept')}>{t('organization.accept')}</Btn>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild><Btn disabled={busy || !decisions[task.id]?.trim()} aria-label={t('organization.moreActions')}><MoreHorizontal className="lucide-inline" /></Btn></DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem disabled={task.state !== 'review'} onSelect={() => review(task.id, 'revise')}>{t('organization.revise')}</DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => review(task.id, 'cancel')}>{t('organization.cancel')}</DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </div>}
            </Card>)}
            {displayedRuns.map(run =>
              <Card key={run.id} className="space-y-2">
                <p className="text-sm">{memberLabel(run.member_id)} · {label(run.state)}</p>
                {/* No hand-off: adjacent task and message forms may contain unsaved drafts. */}
                <ErrorNotice message={run.error} />
                <Btn disabled={busy} onClick={() => { void send('retry', { member_id: run.member_id }).catch(() => {}) }}>{t('organization.retry')}</Btn>
              </Card>)}
          </div>
        </div></TabsContent>
        </Tabs>
      </>}
    </div>
    {confirmDialog}
  </div>
}
