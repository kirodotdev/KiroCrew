import { useState } from 'react'
import { api } from '../api/client'
import { Input, SendBtn } from './ui'
import { SettingsToggle } from './settings'
import AgentSelector, { type KiroCrewAgent } from './AgentSelector'
import type { CronJob } from '../types'
import { SaveCreateLabel, CRON_SEL, expandDow } from '../utils/cronUtils'

export const TIMEZONES = ['America/Los_Angeles','America/Phoenix','America/Denver','America/Chicago','America/New_York','America/Sao_Paulo','Europe/London','Europe/Berlin','Europe/Paris','Asia/Kolkata','Asia/Shanghai','Asia/Tokyo','Australia/Sydney','Pacific/Auckland','UTC']
const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const GRID_TO_CRON_DOW = [0, 1, 2, 3, 4, 5, 6, 0] // grid 1-7 → cron dow
const CRON_DOW_TO_GRID: Record<number, number> = { 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 0: 7, 7: 7 }



/** Parse a CronJob into initial form state */
function parseJobDefaults(job?: CronJob) {
  if (!job) return { name: '', message: '', agent: '', channel: '', approvalMode: '', silent: false, strictSchedule: false, hideInChat: false, schedMode: 'interval' as const, intVal: 1, intUnit: 'hours' as const, weekDays: [] as number[], weekTime: '09:00', cronExpr: '' }
  const isInterval = !!(job.every_secs || (job.schedule || '').match(/^every\s+\d+/))
  const secs = job.every_secs || (() => { const m = (job.schedule || '').match(/^every\s+(\d+)\s*([sh])/); if (!m) return 3600; return m[2] === 'h' ? parseInt(m[1]) * 3600 : parseInt(m[1]) })()
  const intUnit = secs >= 86400 ? 'days' as const : secs >= 3600 ? 'hours' as const : 'minutes' as const
  const intVal = Math.max(1, Math.round(intUnit === 'days' ? secs / 86400 : intUnit === 'hours' ? secs / 3600 : secs / 60))
  const cronRaw = job.cron_expr || ''
  const cronParts = cronRaw.split(/\s+/)
  const isWeekly = !isInterval && cronParts.length === 5 && cronParts[4] !== '*' && cronParts[2] === '*' && cronParts[3] === '*'
  const schedMode = isInterval ? 'interval' as const : isWeekly ? 'weekly' as const : 'cron' as const
  // Read cron time and days directly (stored in job timezone, not UTC)
  let weekDays: number[] = []
  let weekTime = '09:00'
  if (isWeekly) {
    const h = parseInt(cronParts[1]), m = parseInt(cronParts[0])
    weekDays = expandDow(cronParts[4]).map(d => CRON_DOW_TO_GRID[d] || 1)
    weekTime = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`
  }
  return { name: job.name, message: job.message, agent: job.agent || '', projectPath: job.project_path || '', channel: job.channel || '', approvalMode: job.approval_mode || '', silent: job.silent || false, strictSchedule: job.strict_schedule || false, hideInChat: job.hide_in_chat || false, schedMode, intVal, intUnit, weekDays, weekTime, cronExpr: cronRaw }
}

/** Build the API body from form state. Returns null if validation fails (sets error). */
function buildBody(
  f: ReturnType<typeof parseJobDefaults>,
  tz: string,
  setError: (e: string) => void,
): Record<string, string | number | boolean> | null {
  if (!f.name || !f.message) { setError('Name and message are required'); return null }
  const body: Record<string, string | number | boolean> = { name: f.name, message: f.message, agent: f.agent }
  if (f.projectPath) body.project_path = f.projectPath
  if (f.channel) body.channel = f.channel
  if (f.approvalMode) body.approval_mode = f.approvalMode
  body.silent = f.silent
  body.strict_schedule = f.strictSchedule
  body.hide_in_chat = f.hideInChat
  if (f.schedMode === 'interval') {
    body.every = f.intVal * (f.intUnit === 'minutes' ? 60 : f.intUnit === 'hours' ? 3600 : 86400)
  } else if (f.schedMode === 'weekly') {
    if (f.weekDays.length === 0) { setError('Select at least one day'); return null }
    const [h, m] = f.weekTime.split(':').map(Number)
    body.cron = `${m} ${h} * * ${f.weekDays.map(d => GRID_TO_CRON_DOW[d]).join(',')}`
    body.timezone = tz
  } else {
    const expr = f.cronExpr.trim()
    if (expr.split(/\s+/).length !== 5) { setError('Enter a valid 5-field cron expression'); return null }
    body.cron = expr
    body.timezone = tz
  }
  return body
}

interface Props {
  job?: CronJob // if provided, edit mode
  agents: KiroCrewAgent[]
  defaultAgent: string
  onSaved: () => void
  /** Vertical layout for side panel, horizontal for inline create */
  layout?: 'vertical' | 'horizontal'
  /** If true, the component won't render its own submit button (parent renders it) */
  externalSubmit?: boolean
  /** Ref callback — parent can call this to trigger submit */
  submitRef?: React.MutableRefObject<(() => void) | null>
  /** Called when saving state changes */
  onSavingChange?: (saving: boolean) => void
}

export default function JobForm({ job, agents, defaultAgent, onSaved, layout = 'horizontal', externalSubmit, submitRef, onSavingChange }: Props) {
  const defaults = parseJobDefaults(job)
  const [name, setName] = useState(defaults.name)
  const [msg, setMsg] = useState(defaults.message)
  const [agent, setAgent] = useState(defaults.agent)
  const [projectPath, setProjectPath] = useState(defaults.projectPath || '')
  const [channel, setChannel] = useState(defaults.channel)
  const [approvalMode, setApprovalMode] = useState(defaults.approvalMode)
  const [silent, setSilent] = useState(defaults.silent)
  const [strictSchedule, setStrictSchedule] = useState(defaults.strictSchedule)
  const [hideInChat, setHideInChat] = useState(defaults.hideInChat)
  const [schedMode, setSchedMode] = useState(defaults.schedMode)
  const [intVal, setIntVal] = useState(defaults.intVal)
  const [intUnit, setIntUnit] = useState(defaults.intUnit)
  const [weekDays, setWeekDays] = useState(defaults.weekDays)
  const [weekTime, setWeekTime] = useState(defaults.weekTime)
  const [tz, setTz] = useState(() => job ? (job.timezone || 'UTC') : Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [cronExpr, setCronExpr] = useState(defaults.cronExpr)
  const [error, setError] = useState('')
  const [saving, setSavingState] = useState(false)
  const setSaving = (v: boolean) => { setSavingState(v); onSavingChange?.(v) }

  const submit = async () => {
    setError(''); setSaving(true)
    const f = { name, message: msg, agent, projectPath, channel, approvalMode, silent, strictSchedule, hideInChat, schedMode, intVal, intUnit, weekDays, weekTime, cronExpr }
    const body = buildBody(f, tz, setError)
    if (!body) { setSaving(false); return }
    try {
      const res = job
        ? await api.updateCron(job.id, body)
        : await api.createCron(body).catch((e: Error) => ({ error: e.message }))
      if (res.error) { setError(res.error); setSaving(false); return }
      if (!job) { setName(''); setMsg(''); setWeekDays([]); setIntVal(1); setChannel(''); setApprovalMode(''); setSilent(false); setStrictSchedule(false); setHideInChat(false); setProjectPath('') }
      onSaved()
    } catch { setError('Failed to save'); setSaving(false) }
  }

  const toggleDay = (d: number) => setWeekDays(prev => prev.includes(d) ? prev.filter(x => x !== d) : [...prev, d].sort())

  // Expose submit to parent via ref
  if (submitRef) submitRef.current = submit

  const vertical = layout === 'vertical'

  return (
    <div className="flex flex-col gap-3">
      {vertical ? (<>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">Name</span>
          <span className="text-[11px] text-muted/70">A short label for this job</span>
          <Input id="jobform-name" aria-label="Name" value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          {job?.script ? (<>
            <span className="text-[12px] text-muted font-medium">Script</span>
            <code className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[12px] font-mono break-all">{job.script}</code>
          </>) : job?.command ? (<>
            <span className="text-[12px] text-muted font-medium">Command</span>
            <code className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[12px] font-mono break-all">{job.command}</code>
          </>) : (
          <div className="flex flex-col gap-1">
            <span className="text-[12px] text-muted font-medium">Message</span>
            <span className="text-[11px] text-muted/70">The prompt or task sent to the agent when this job fires</span>
            <textarea id="jobform-message" aria-label="Message" className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none resize-y min-h-[60px] focus-ring" value={msg} onChange={e => setMsg(e.target.value)} />
          </div>)}
        </div>
      </>) : (
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder="Job name" value={name} onChange={e => setName(e.target.value)} />
          <Input placeholder="Message / task" style={{ flex: 2 }} value={msg} onChange={e => setMsg(e.target.value)} />
          <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent} activeProjectPath={projectPath} onChange={(name, pp) => { setAgent(name); setProjectPath(pp || '') }} />
          <Input placeholder="Channel ID (optional)" style={{ flex: '0 0 170px' }} value={channel} onChange={e => setChannel(e.target.value)} />
          <select className={CRON_SEL} value={approvalMode} onChange={e => setApprovalMode(e.target.value)}>
            <option value="">Approval: default</option><option value="auto">auto</option>
          </select>
          <label htmlFor="jobform-silent" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-silent" aria-label="Silent" type="checkbox" checked={silent} onChange={e => setSilent(e.target.checked)} /> Silent</label>
          <label htmlFor="jobform-strict-schedule" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-strict-schedule" aria-label="Strict schedule" type="checkbox" checked={strictSchedule} onChange={e => setStrictSchedule(e.target.checked)} /> Strict schedule</label>
          <label htmlFor="jobform-hide-in-chat" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-hide-in-chat" aria-label="Hide in chat" type="checkbox" checked={hideInChat} onChange={e => setHideInChat(e.target.checked)} /> Hide in chat</label>
        </div>
      )}

      {/* Schedule */}
      {vertical && <div className="flex flex-col gap-0.5"><span className="text-[12px] text-muted font-medium">Schedule</span><span className="text-[11px] text-muted/70">How often this job runs</span></div>}
      <div className={`flex gap-2 items-center flex-wrap ${vertical ? '' : ''}`}>
        <select className={CRON_SEL} value={schedMode} onChange={e => setSchedMode(e.target.value as 'interval' | 'weekly' | 'cron')}>
          <option value="interval">Every interval</option>
          <option value="weekly">Weekly schedule</option>
          <option value="cron">Cron expression</option>
        </select>
        {schedMode === 'interval' ? (<>
          <Input type="number" min={1} style={{ flex: '0 0 70px' }} value={intVal} onChange={e => setIntVal(Math.max(1, parseInt(e.target.value) || 1))} />
          <select className={CRON_SEL} value={intUnit} onChange={e => setIntUnit(e.target.value as 'minutes' | 'hours' | 'days')}>
            <option value="minutes">minutes</option><option value="hours">hours</option><option value="days">days</option>
          </select>
        </>) : schedMode === 'weekly' ? (<>
          <div className="flex gap-1 flex-wrap">{DAY_NAMES.map((d, i) => (
            <button key={d} type="button" onClick={() => toggleDay(i + 1)} className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all ${weekDays.includes(i + 1) ? 'bg-accent text-accent-fg border-accent' : 'bg-bg-elevated text-muted border-border hover:border-border-strong'}`}>{d}</button>
          ))}</div>
          <span className="text-muted text-[13px]">at</span>
          <Input type="time" style={{ flex: '0 0 100px' }} value={weekTime} onChange={e => setWeekTime(e.target.value)} />
          <select className={`${CRON_SEL} ${vertical ? 'text-[12px]' : ''}`} style={vertical ? {} : { flex: '0 0 200px' }} value={tz} onChange={e => setTz(e.target.value)}>
            {Array.from(new Set([tz, ...TIMEZONES])).map(z => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
          </select>
        </>) : (<>
          <Input value={cronExpr} onChange={e => setCronExpr(e.target.value)} placeholder="0 9 * * 1-5" />
          <select className={`${CRON_SEL} ${vertical ? 'text-[12px]' : ''}`} style={vertical ? {} : { flex: '0 0 200px' }} value={tz} onChange={e => setTz(e.target.value)}>
            {Array.from(new Set([tz, ...TIMEZONES])).map(z => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
          </select>
        </>)}
        {!vertical && !externalSubmit && <SendBtn onClick={submit} disabled={saving}>{saving ? 'Saving...' : (job ? 'Save' : 'Add')}</SendBtn>}
      </div>

      {/* Vertical-only: agent, channel, actions */}
      {vertical && (<>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">Agent</span>
          <span className="text-[11px] text-muted/70">Which agent handles this job. Leave default for the primary agent.</span>
          <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent} activeProjectPath={projectPath} onChange={(name, pp) => { setAgent(name); setProjectPath(pp || '') }} />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">Channel ID</span>
          <span className="text-[11px] text-muted/70">Slack channel to post results to. Leave empty for DM.</span>
          <Input id="jobform-channel" aria-label="Channel ID" value={channel} onChange={e => setChannel(e.target.value)} placeholder="Optional" />
        </div>
        <label htmlFor="jobform-approval" className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">Approval</span>
          <span className="text-[11px] text-muted/70">How tool calls are approved during execution</span>
          <select id="jobform-approval" className={CRON_SEL} value={approvalMode} onChange={e => setApprovalMode(e.target.value)}>
            <option value="">Default</option><option value="auto">Auto-approve</option>
          </select>
        </label>
        <SettingsToggle
          label="Silent mode"
          description="Suppress automatic message delivery. The agent controls when to notify."
          checked={silent}
          onChange={setSilent}
        />
        <SettingsToggle
          label="Strict schedule"
          description="Fire exactly on schedule with no jitter. By default, jobs are spread randomly to reduce traffic spikes."
          checked={strictSchedule}
          onChange={setStrictSchedule}
        />
        <SettingsToggle
          label="Hide in chat"
          description="Keep this job's runs out of the active session list. Turn on for fire-and-forget jobs (digests, cleanups) — results still reach Slack/notifications and the History tab."
          checked={hideInChat}
          onChange={setHideInChat}
        />
        {vertical && !externalSubmit && (
          <SendBtn onClick={submit} disabled={saving}>
            <SaveCreateLabel isEdit={!!job} saving={saving} />
          </SendBtn>
        )}
      </>)}

      {error && <div className="text-danger text-[13px]">{error}</div>}
    </div>
  )
}

export { buildBody, parseJobDefaults }
