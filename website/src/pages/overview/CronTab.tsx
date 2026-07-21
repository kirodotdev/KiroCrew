import { useState, useMemo } from 'react'
import Clickable from '../../components/Clickable'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Pencil, Hourglass, Play, MessageSquare, VolumeX } from 'lucide-react'
import { useAppSelector } from '../../store'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, SearchInput } from '../../components/ui'
import AgentSelector from '../../components/AgentSelector'
import { TIMEZONES } from '../../components/JobForm'
import InfoTip from '../../components/InfoTip'
import { esc } from '../../api/helpers'
import { useProvider } from '../../providers'
import type { CronJob } from '../../types'
import { useAgents } from '../../hooks/useAgents'
import { timeAgo } from '../../utils/timeAgo'
import { PY_TO_CRON, CRON_SEL, DAY_LABELS } from '../../utils/cronUtils'
import { useSortableTable } from '../../hooks/useSortableTable'
import SortableHeader from '../../components/SortableHeader'
import { useCronActions } from '../../hooks/useCronActions'

export default function CronTab({ refreshTrigger }: { refreshTrigger: number }) {
  const provider = useProvider()
  const noCrons = useAppSelector(s => s.dashboard.status?.no_crons)
  const fmtAgo = (ts?: number) => ts ? timeAgo(ts) : '—'
  const { data: jobs = [], refetch: load } = useQuery<CronJob[]>({
    queryKey: ['cron-jobs', refreshTrigger],
    queryFn: () => api.crons().then(r => r.jobs || []),
  })
  const [name, setName] = useState(''); const [msg, setMsg] = useState('')
  const [mode, setMode] = useState<'interval' | 'weekly'>('interval')
  const [intervalVal, setIntervalVal] = useState(1); const [intervalUnit, setIntervalUnit] = useState<'minutes' | 'hours' | 'days'>('hours')
  const [days, setDays] = useState<number[]>([]); const [time, setTime] = useState('09:00')
  const [tz, setTz] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [error, setError] = useState('')
  const [agent, setAgent] = useState('')
  const [channel, setChannel] = useState('')
  const [approvalMode, setApprovalMode] = useState('')
  const [silent, setSilent] = useState(false)
  const { agents, defaultAgent } = useAgents(refreshTrigger)
  const [cronFilter, setCronFilter] = useState('')
  const [editing, setEditing] = useState<CronJob | null>(null)
  const [editName, setEditName] = useState('')
  const [editMsg, setEditMsg] = useState('')
  const [editSchedule, setEditSchedule] = useState('')
  const [editTz, setEditTz] = useState('')
  const [editAgent, setEditAgent] = useState('')
  const [editChannel, setEditChannel] = useState('')
  const [editError, setEditError] = useState('')
  const { running, actionError, runNow, openInChat } = useCronActions(load)
  const filtered = useMemo(() => jobs.filter(j => !cronFilter || (j.name + ' ' + j.message + ' ' + (j.agent || '') + ' ' + (j.model || '') + ' ' + (j.channel || '')).toLowerCase().includes(cronFilter.toLowerCase())), [jobs, cronFilter])
  const cronComparators = useMemo(() => ({
    name: (a: CronJob, b: CronJob) => a.name.localeCompare(b.name),
    schedule: (a: CronJob, b: CronJob) => (a.schedule || '').localeCompare(b.schedule || ''),
    status: (a: CronJob, b: CronJob) => {
      const rank = (j: CronJob) =>
        !j.enabled ? 0 : j.last_status === 'error' ? 1 : j.last_status === 'ok' ? 2 : 3;
      return rank(a) - rank(b);
    },
    lastRun: (a: CronJob, b: CronJob) => (a.last_run_ts || 0) - (b.last_run_ts || 0),
  }), [])
  const { sorted: sortedJobs, sort: cronSort, toggle: toggleCronSort } = useSortableTable(filtered, 'cron-overview', cronComparators, { key: 'name', dir: 'asc' })
  const toggleDay = (d: number) => setDays(prev => prev.includes(d) ? prev.filter(x => x !== d) : [...prev, d].sort())
  const openEdit = (j: CronJob) => {
    setEditing(j); setEditName(j.name); setEditMsg(j.message)
    setEditSchedule(j.schedule || ''); setEditTz(j.timezone || 'UTC'); setEditAgent(j.agent || ''); setEditChannel(j.channel || ''); setEditError('')
  }
  const saveEdit = async () => {
    if (!editing) return
    setEditError('')
    const body: Record<string, string | number> = {}
    if (editName !== editing.name) body.name = editName
    if (editMsg !== editing.message) body.message = editMsg
    if (editAgent !== (editing.agent || '')) body.agent_id = editAgent
    if (editChannel !== (editing.channel || '')) body.channel = editChannel
    if (editSchedule !== (editing.schedule || '')) {
      const parts = editSchedule.trim().split(/\s+/)
      if (parts.length === 5) { body.cron = editSchedule.trim() }
      else { const n = parseInt(editSchedule); if (!isNaN(n) && n >= 60) body.every = n; else { setEditError('Enter a 5-field cron expression or interval in seconds (≥60)'); return } }
    }
    if (editTz !== (editing.timezone || 'UTC')) {
      body.timezone = editTz
    }
    if (Object.keys(body).length === 0) { setEditing(null); return }
    try {
      const res = await api.updateCron(editing.id, body)
      if (res.error) { setEditError(res.error); return }
    } catch { setEditError('Failed to update job — check server connection'); return }
    setEditing(null); load()
  }
  const add = async () => {
    setError('')
    if (!name || !msg) { setError('Name and message are required'); return }
    const body: Record<string, string | number | boolean> = { name, message: msg, agent }
    if (channel) body.channel = channel
    if (approvalMode) body.approval_mode = approvalMode
    if (silent) body.silent = true
    if (mode === 'interval') {
      const secs = intervalVal * (intervalUnit === 'minutes' ? 60 : intervalUnit === 'hours' ? 3600 : 86400)
      body.every = secs
    } else {
      if (days.length === 0) { setError('Select at least one day'); return }
      const [h, m] = time.split(':').map(Number)
      const cronDow = days.map(d => PY_TO_CRON[d - 1]).join(',')
      body.cron = `${m} ${h} * * ${cronDow}`
      body.timezone = tz
    }
    const res = await api.createCron(body)
    if (res.error) { setError(res.error); return }
    setName(''); setMsg(''); setDays([]); setIntervalVal(1); setError(''); setChannel(''); setApprovalMode(''); setSilent(false); load()
  }
  return (<>
    {noCrons && <div className="bg-yellow-900/30 border border-yellow-700/50 text-yellow-200 px-4 py-2 rounded-lg mb-3 text-sm"><AlertTriangle className="lucide-inline" /> Cron execution disabled (<code className="text-yellow-300">--no-crons</code>) — jobs are managed by another instance</div>}
    <Card><CardTitle>Add Job <InfoTip text={`Schedule recurring or one-time tasks. Jobs run in their own ${provider.labels.sessionProcess} session. Use cron expressions or interval seconds.`} /></CardTitle>
      <div className="flex flex-col gap-3">
        <fieldset className="contents" aria-label="Job details">
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder="Job name" value={name} onChange={e => setName(e.target.value)} />
          <Input placeholder="Message / task" style={{ flex: 2 }} value={msg} onChange={e => setMsg(e.target.value)} />
          <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent} onChange={(name) => setAgent(name)} />
        </div>
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder="Channel ID (optional)" style={{ flex: '0 0 170px' }} value={channel} onChange={e => setChannel(e.target.value)} />
          <label htmlFor="cron-silent" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="cron-silent" type="checkbox" aria-label="Silent" checked={silent} onChange={e => setSilent(e.target.checked)} /> Silent</label>
          <select className={CRON_SEL} value={approvalMode} onChange={e => setApprovalMode(e.target.value)}>
            <option value="">Approval: default</option><option value="auto">auto</option>
          </select>
        </div>
        </fieldset>
        <div className="flex gap-2 items-center flex-wrap">
          <select className={CRON_SEL} value={mode} onChange={e => setMode(e.target.value as 'interval' | 'weekly')}>
            <option value="interval">Every interval</option>
            <option value="weekly">Weekly schedule</option>
          </select>
          {mode === 'interval' ? (<>
            <Input type="number" min={1} style={{ flex: '0 0 70px' }} value={intervalVal} onChange={e => setIntervalVal(Math.max(1, parseInt(e.target.value) || 1))} />
            <select className={CRON_SEL} value={intervalUnit} onChange={e => setIntervalUnit(e.target.value as 'minutes' | 'hours' | 'days')}>
              <option value="minutes">min</option><option value="hours">hr</option><option value="days">day</option>
            </select>
          </>) : (<>
            <div className="flex gap-1">{DAY_LABELS.map((d, i) => (
              <button key={d} onClick={() => toggleDay(i + 1)} className={`px-2.5 py-1.5 rounded-md text-[13px] font-medium border cursor-pointer transition-all ${days.includes(i + 1) ? 'bg-accent text-accent-fg border-accent' : 'bg-bg-elevated text-muted border-border hover:border-border-strong'}`}>{d}</button>
            ))}</div>
            <span className="text-muted text-[13px]">at</span>
            <Input type="time" style={{ flex: '0 0 100px' }} value={time} onChange={e => setTime(e.target.value)} />
            <select className={CRON_SEL} style={{ flex: '0 0 200px' }} value={tz} onChange={e => setTz(e.target.value)}>
              {TIMEZONES.map(z => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
            </select>
          </>)}
          <SendBtn onClick={add}>Add</SendBtn>
        </div>
        {error && <div className="text-danger text-[13px]">{error}</div>}
      </div></Card>
    <Card><CardTitle>Jobs</CardTitle>
      <div className="mb-3"><SearchInput placeholder="Filter jobs…" value={cronFilter} onChange={e => setCronFilter(e.target.value)} /></div>
      <div className="overflow-x-auto"><table className="w-full border-collapse table-striped"><thead><tr><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[72px]">ID</th><SortableHeader label="Name" sortKey="name" sort={cronSort} onToggle={toggleCronSort} className="w-[100px]" /><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[80px]">Agent</th><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[80px]">Model</th><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[90px]">Channel</th><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[80px]">Approval</th><SortableHeader label="Schedule" sortKey="schedule" sort={cronSort} onToggle={toggleCronSort} className="w-[110px]" /><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[400px]">Message</th><SortableHeader label="Status" sortKey="status" sort={cronSort} onToggle={toggleCronSort} className="w-[70px]" /><SortableHeader label="Last Run" sortKey="lastRun" sort={cronSort} onToggle={toggleCronSort} className="w-[80px]" /><th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[180px]">Actions</th></tr></thead>
        <tbody>{jobs.length === 0 ? <tr><td colSpan={11} className="text-muted italic px-2.5 py-3.5 text-sm">No cron jobs</td></tr> : sortedJobs.length === 0 ? <tr><td colSpan={11} className="text-muted italic px-2.5 py-3.5 text-sm">No matching jobs</td></tr> : sortedJobs.map(j => (
          <tr key={j.id} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm"><code>{j.id}</code></td><td className="px-2.5 py-2 border-b border-border text-sm">{esc(j.name)}</td><td className="px-2.5 py-2 border-b border-border text-sm">{j.agent ? <span className="px-1.5 py-[2px] rounded-full text-[12px] font-bold bg-aim-subtle text-aim border border-aim/30">{j.agent}</span> : <span className="text-muted text-[13px]">default</span>}</td><td className="px-2.5 py-2 border-b border-border text-sm">{j.model ? <span className="text-[13px] truncate block max-w-[120px]" title={j.model}>{j.model}</span> : <span className="text-muted text-[13px] italic">inherited</span>}</td><td className="px-2.5 py-2 border-b border-border text-sm">{j.channel ? <code>{j.channel}</code> : <span className="text-muted text-[13px]">DM</span>}</td><td className="px-2.5 py-2 border-b border-border text-sm">{j.approval_mode ? <Badge variant="ok">{j.approval_mode}</Badge> : <span className="text-muted text-[13px]">default</span>}{j.silent ? <> <VolumeX className="lucide-inline" /></> : ''}</td><td className="px-2.5 py-2 border-b border-border text-sm"><code>{esc(j.schedule)}</code>{j.timezone && <span className="block text-[11px] text-muted">{j.timezone.replace(/_/g, ' ')}</span>}</td><td className="px-2.5 py-2 border-b border-border text-sm break-words" title={j.message}>{esc(j.message)}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm">{j.enabled ? (j.last_status === 'ok' ? <Badge variant="ok">OK</Badge> : j.last_status === 'error' ? <Badge variant="err">Error</Badge> : <Badge variant="ok">Ready</Badge>) : <Badge variant="warn">Paused</Badge>}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{fmtAgo(j.last_run_ts)}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm whitespace-nowrap">
              <span title="Edit"><Btn aria-label="Edit" onClick={() => openEdit(j)}><Pencil className="lucide-inline" /></Btn></span>{' '}
              <span title={j.enabled ? "Run now" : "Resume job to run"} style={{ color: 'var(--ok)' }}><Btn aria-label={running.has(j.id) ? "Running" : "Run now"} onClick={() => runNow(j.id)} disabled={!j.enabled || running.has(j.id)}>{running.has(j.id) ? <Hourglass className="lucide-inline" /> : <><Play className="lucide-inline" /> Run</>}</Btn></span>{' '}
              <span title={j.has_slot ? "Continue session" : j.has_result ? "View last result" : "No result yet"}><Btn aria-label={j.has_slot ? "Continue session" : j.has_result ? "View last result" : "No result yet"} onClick={() => openInChat(j.id)} disabled={!j.has_result && !j.has_slot}><MessageSquare className="lucide-inline" /></Btn></span>{' '}
              <Btn onClick={async () => { await api.toggleCron(j.id, !j.enabled); load() }}>{j.enabled ? 'Pause' : 'Resume'}</Btn>{' '}
              <Btn danger onClick={async () => { await api.deleteCron(j.id); load() }}>Delete</Btn>
              {actionError?.id === j.id && <span className="text-danger text-[12px] ml-1">{actionError.msg}</span>}
            </td></tr>
        ))}</tbody></table></div></Card>
    {editing && (
      <Clickable className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100]" onClick={() => setEditing(null)}>
        {/* Handlers only stop propagation so a click/keypress inside the dialog
            doesn't bubble to the backdrop's close handler — the dialog itself is
            not an interactive control. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <div role="dialog" aria-modal="true" aria-label="Edit cron job" className="bg-bg-elevated rounded-xl border border-border p-6 w-[500px] max-w-[90vw] shadow-xl" onClick={e => e.stopPropagation()} onKeyDown={e => e.stopPropagation()}>
          <h3 className="text-lg font-semibold text-text mb-4">Edit Job: {editing.id}</h3>
          {/* label-has-for is deprecated and requires physical nesting; the custom
              <Input> forwardRef can't be seen as a nested control by the linter, so
              we pair each caption <label htmlFor> with an id-matched control for a
              real programmatic association (label-has-associated-control passes). */}
          {/* eslint-disable jsx-a11y/label-has-for */}
          <div className="flex flex-col gap-3">
            <label htmlFor="cron-edit-name" className="text-sm text-muted">Name</label>
            <Input id="cron-edit-name" aria-label="Name" value={editName} onChange={e => setEditName(e.target.value)} />
            <label htmlFor="cron-edit-message" className="text-sm text-muted">Message</label>
            <textarea id="cron-edit-message" aria-label="Message" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none resize-y min-h-[80px] focus-ring" value={editMsg} onChange={e => setEditMsg(e.target.value)} />
            <label htmlFor="cron-edit-schedule" className="text-sm text-muted">Schedule (cron expr or seconds)</label>
            <Input id="cron-edit-schedule" aria-label="Schedule" value={editSchedule} onChange={e => setEditSchedule(e.target.value)} placeholder="0 9 * * 1-5 or 3600" />
            <label htmlFor="cron-edit-tz" className="text-sm text-muted">Timezone</label>
            <select id="cron-edit-tz" aria-label="Timezone" className={CRON_SEL} value={editTz} onChange={e => setEditTz(e.target.value)}>
              {(editing?.timezone && !TIMEZONES.includes(editing.timezone) ? [editing.timezone, ...TIMEZONES] : TIMEZONES).map(z => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
            </select>
            <span className="text-sm text-muted">Agent</span>
            <AgentSelector agents={agents} defaultAgent={defaultAgent} value={editAgent} onChange={(name) => setEditAgent(name)} />
            <label htmlFor="cron-edit-channel" className="text-sm text-muted">Channel ID</label>
            <Input id="cron-edit-channel" aria-label="Channel ID" value={editChannel} onChange={e => setEditChannel(e.target.value)} placeholder="Optional" />
            {/* eslint-enable jsx-a11y/label-has-for */}
            {editError && <div className="text-danger text-[13px]">{editError}</div>}
            <div className="flex gap-2 justify-end mt-2">
              <Btn onClick={() => setEditing(null)}>Cancel</Btn>
              <SendBtn onClick={saveEdit}>Save</SendBtn>
            </div>
          </div>
        </div>
      </Clickable>
    )}
  </>)
}
