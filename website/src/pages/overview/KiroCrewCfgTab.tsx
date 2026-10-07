import { useState, useEffect, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Bot, FolderOpen, Brain, Settings, Lock, Flame, Gauge, Plus } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Badge, Btn, EmptyState, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import SimpleSelect from '../../components/SimpleSelect'
// The crew editor's create dialog, shared the way the Crewmates dialog shares it.
import { WorkspaceModal } from '../KiroCrewAgentsPage'
import { useProvider } from '../../providers'
import { useSidePanelLeaveGuard } from '../../components/SidePanelLayout'
import { DEFAULT_CREWMATE_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { useAppDispatch } from '../../store'
import { triggerRefresh } from '../../store/dashboardSlice'

import type { KiroCrewAgent } from '../../components/AgentSelector'

import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
import { usePersistedString } from '../../hooks/usePersistedString'

/** `PUT /api/config/default-agent` answering that the name is not a configured
 *  alias: a 400 whose body carries `code: "default_agent_not_alias"`. Duck-typed
 *  on `status`/`body` like `isNotFoundError`, so a mocked client that rejects
 *  with `Object.assign(new Error(), { status, body })` counts too. */
export const isUnknownAliasRefusal = (e: unknown): boolean => {
  if (typeof e !== 'object' || e === null) return false
  const r = e as { status?: unknown; body?: unknown }
  if (r.status !== 400 || typeof r.body !== 'string') return false
  try {
    return (JSON.parse(r.body) as { code?: unknown }).code === 'default_agent_not_alias'
  } catch {
    return false
  }
}

type KiroCrewAgentCfg = Omit<KiroCrewAgent, 'name'>
interface WorkspaceCfg { dir: string }
interface MemoryStoreCfg { description: string; embedding_provider: string }
interface KiroCrewCfg {
  agents: Record<string, KiroCrewAgentCfg>
  default_agent: string
  workspaces: Record<string, WorkspaceCfg>
  default_workspace: string
  memory_stores: Record<string, MemoryStoreCfg>
  default_memory_store: string
  agent: { default_agent: string; provider: string; model: string; approval_mode: string; sandbox: string; subagent_max_turns?: number; max_subagents?: number; subagent_auto_max?: number; spawn_min_memory_gb?: number; resource_pressure_gb?: number; resource_critical_gb?: number; tool_search?: boolean; max_channels: number; max_channel_agents: number }
  session: { timeout_secs: number; pool_size: number; pool_agent: string; pool_ttl_secs: number }
  memory: { embedding_provider: string }
  auto_update: boolean
}

function Tag({ children, active }: { children: React.ReactNode; active?: boolean }) {
  return <span className={`px-1.5 py-[1px] rounded text-[12px] font-mono ${active ? 'bg-accent/15 text-accent border border-accent/30' : 'bg-bg-elevated text-muted border border-border'}`}>{children}</span>
}

function UsedByTags({ names }: { names: string[] }) {
  return <div className="flex gap-1 flex-wrap">{names.length > 0 ? names.map(n => <Tag key={n} active>{n}</Tag>) : <span className="text-muted text-[13px]">—</span>}</div>
}

/** One workspace row: change its directory in place, or delete it after its
 *  exact name is typed. The backend refuses to delete the default workspace
 *  or one an agent uses, so those rows offer no Delete. */
function WorkspaceRow({ name, dir, isDefault, usedBy, onChanged }: { name: string; dir: string; isDefault: boolean; usedBy: string[]; onChanged: () => void }) {
  // Only the directory edit persists, so it survives a Developer tab switch. An
  // armed delete and its typed confirm never outlive this mount.
  const [storedEdit, setStoredEdit] = usePersistedString(`mc-cfg-workspace-form:${name}`, 'view')
  const [editDraft, setEditDraft] = usePersistedString(`mc-cfg-workspace-draft:${name}`, '')
  const [deleting, setDeleting] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const mode = deleting ? 'delete' : storedEdit === 'edit' ? 'edit' : 'view'
  const draft = mode === 'delete' ? confirmText : editDraft
  const setDraft = mode === 'delete' ? setConfirmText : setEditDraft
  const setMode = (m: 'view' | 'edit' | 'delete') => { setDeleting(m === 'delete'); setStoredEdit(m === 'edit' ? 'edit' : 'view') }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const open = (next: 'edit' | 'delete') => { setMode(next); if (next === 'edit') setEditDraft(dir); else setConfirmText(''); setError('') }
  const run = async (call: () => Promise<unknown>) => {
    setBusy(true)
    setError('')
    try {
      await call()
      setMode('view')
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      // A refusal is decided on fresh state (e.g. a new agent now uses this
      // workspace), so refresh the table to match the message.
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  const editing = mode === 'edit'
  const close = () => { if (!busy) { setMode('view'); setError('') } }
  return (
    <>
      <tr data-testid={`workspace-row-${name}`}>
        <td className="px-2.5 py-2 text-sm text-text font-medium">
          {name} {isDefault && <Badge variant="ok">{i18nT('pages.overview.kiroCrewCfgTab.default')}</Badge>}
        </td>
        <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{dir}</td>
        <td className="px-2.5 py-2"><UsedByTags names={usedBy} /></td>
        <td className="px-2.5 py-2 text-right whitespace-nowrap">
          {/* The row's buttons stay in place while a form is open below them, so
              the clicked control never vanishes; only the form's own buttons act. */}
          <Btn disabled={mode !== 'view'} aria-expanded={mode === 'edit'} onClick={() => open('edit')}>{i18nT('pages.overview.kiroCrewCfgTab.change_directory')}</Btn>
          {!isDefault && usedBy.length === 0 && <Btn danger disabled={mode !== 'view'} aria-expanded={mode === 'delete'} className="ml-2" onClick={() => open('delete')}>{i18nT('settings.secrets.delete')}</Btn>}
        </td>
      </tr>
      {(mode !== 'view' || error) && (
        <tr data-testid={`workspace-form-${name}`}>
          <td colSpan={4} className="px-2.5 pb-2">
            {mode !== 'view' && <div className="flex flex-col gap-2">
              {editing
                ? <div className="text-[12px] text-muted">{i18nT('pages.overview.kiroCrewCfgTab.change_directory_hint')}</div>
                : <>
                  <div className="text-[12px] text-danger">{i18nT('pages.overview.kiroCrewCfgTab.delete_workspace_warning')}</div>
                  <div className="text-[12px] text-text">{i18nT('pages.overview.kiroCrewCfgTab.type_workspace_name_to_confirm', { name })}</div>
                </>}
              <div className="flex gap-2">
                <Input
                  aria-label={editing ? i18nT('pages.overview.kiroCrewCfgTab.directory') : i18nT('pages.overview.kiroCrewCfgTab.type_workspace_name_to_confirm', { name })}
                  placeholder={editing ? undefined : name}
                  value={draft}
                  disabled={busy}
                  onChange={e => setDraft(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Escape') close() }}
                  autoFocus
                />
                <Btn disabled={busy} onClick={close}>{i18nT('settings.secrets.cancel')}</Btn>
                {editing
                  ? <Btn disabled={busy || !draft.trim() || draft.trim() === dir} onClick={() => run(() => api.updateWorkspace(name, { dir: draft.trim() }))}>{i18nT('settings.secrets.save')}</Btn>
                  : <Btn danger disabled={busy || draft !== name} onClick={() => run(() => api.deleteWorkspace(name))}>{i18nT('settings.secrets.delete')}</Btn>}
              </div>
            </div>}
            {/* No hand-off: the typed directory or confirm name (`draft`) is unsaved,
                and the form stays open with it so a retry needs no retyping. */}
            <ErrorNotice message={error} variant="inline" />
          </td>
        </tr>
      )}
    </>
  )
}

/** The RAM Limits rows, mapped to their label keys so a refusal names its field. */
const MEMORY_LIMIT_LABEL_KEYS: Record<string, string> = {
  'agent.spawn_min_memory_gb': 'pages.overview.kiroCrewCfgTab.spawn_min_memory',
  'agent.resource_pressure_gb': 'pages.overview.kiroCrewCfgTab.memory_pressure_threshold',
  'agent.resource_critical_gb': 'pages.overview.kiroCrewCfgTab.memory_critical_threshold',
}
const MEMORY_LIMIT_PATHS = new Set(Object.keys(MEMORY_LIMIT_LABEL_KEYS))
const rowCls = "flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm"
const inputCls = "h-7 min-w-[120px] bg-bg-elevated border border-border rounded-md px-2 py-0.5 text-[13px] font-mono text-text focus-visible:border-accent focus:outline-hidden"
const readonlyCls = "flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm bg-bg-elevated/30 rounded px-1 -mx-1"

function useDirtyTrack<T>(value: T) {
  const [ok, setOk] = useState(false)
  const dirty = useRef(false)
  useEffect(() => { if (dirty.current) { setOk(true); dirty.current = false; const t = setTimeout(() => setOk(false), 2000); return () => clearTimeout(t) } }, [value])
  const markDirty = () => { dirty.current = true }
  return { ok, markDirty }
}

function CfgRow({ label, hint, help, ok, children }: { label: string; hint?: string; help?: string; ok: boolean; children: React.ReactNode }) {
  const row = (
    <div className={help ? rowCls.replace(' border-b border-border', '') : rowCls}>
      <span className="text-muted inline-flex items-center gap-1">{label} {hint && <InfoTip text={hint} />}</span>
      <div className="flex items-center gap-1.5">
        {children}
        {ok && <span className="text-ok text-[11px]"><Check className="lucide-inline" /></span>}
      </div>
    </div>
  )
  if (!help) return row
  // Visible helper text instead of a tooltip, for rows whose effect a reader has
  // to know before they dare change the value.
  return (
    <div className="border-b border-border pb-1.5">
      {row}
      <p className="text-muted text-[12px] leading-snug">{help}</p>
    </div>
  )
}

function CfgSelect({ label, path, value, options, hint, labels, onSave }: { label: string; path: string; value: string; options: string[]; hint?: string; labels?: Record<string, string>; onSave: (p: string, v: string) => void }) {
  const [local, setLocal] = useState(value)
  const { ok, markDirty } = useDirtyTrack(value)
  useEffect(() => { setLocal(value) }, [value])
  return (
    <CfgRow label={label} hint={hint} ok={ok}>
      {/* The trigger is a <button>, so the row's visible label is threaded in
          as the accessible name (matches CfgNumber's aria-label={label}).
          `className` restores this table's compact geometry: the shared trigger
          defaults to `px-3 py-2 text-sm`, which is ~10px taller than the `h-7
          text-[13px]` control it replaced and would grow every row. */}
      <SimpleSelect
        aria-label={label}
        className="h-7 px-2 py-0.5 text-[13px] font-mono"
        style={{ minWidth: 120 }}
        options={options}
        optionLabels={options.map(o => labels?.[o] ?? o)}
        value={local}
        onChange={v => { markDirty(); setLocal(v); onSave(path, v) }}
      />
    </CfgRow>
  )
}

function CfgNumber({ label, path, value, suffix, min, max, step, hint, help, onSave }: { label: string; path: string; value: number; suffix?: string; min?: number; max?: number; step?: number; hint?: string; help?: string; onSave: (p: string, v: number) => void }) {
  const ime = useImeGuard()
  const [local, setLocal] = useState(String(value))
  const { ok, markDirty } = useDirtyTrack(value)
  const [err, setErr] = useState('')
  useEffect(() => { setLocal(String(value)); setErr('') }, [value])
  const commit = () => {
    // A fractional `step` marks a decimal field (GB thresholds); every other row
    // is an integer and keeps parseInt, so typing "1.5" there still saves 1.
    const n = step !== undefined && !Number.isInteger(step) ? parseFloat(local) : parseInt(local)
    if (!Number.isFinite(n)) { setErr('invalid'); return }
    if (min !== undefined && n < min) { setErr(`min ${min}`); return }
    if (max !== undefined && n > max) { setErr(`max ${max}`); return }
    if (n !== value) { markDirty(); setErr(''); onSave(path, n) }
  }
  return (
    <CfgRow label={label} hint={hint} help={help} ok={ok && !err}>
      <input type="number" aria-label={label} min={min} max={max} step={step} placeholder={min !== undefined && max !== undefined ? `${min}–${max}` : undefined}
        className={`${inputCls} text-right ${err ? 'border-danger' : ''}`}
        value={local}
        onChange={e => { setLocal(e.target.value); setErr('') }}
        {...ime.bindEnter({ onEnter: commit, onBlur: commit })}
      />
      {suffix && <span className="text-muted text-[12px]">{suffix}</span>}
      {err && <span className="text-danger text-[11px]">{err}</span>}
    </CfgRow>
  )
}

function CfgToggle({ label, path, value, hint, onSave }: { label: string; path: string; value: boolean; hint?: string; onSave: (p: string, v: boolean) => void }) {
  const [local, setLocal] = useState(value)
  const { ok, markDirty } = useDirtyTrack(value)
  useEffect(() => { setLocal(value) }, [value])
  return (
    <CfgRow label={label} hint={hint} ok={ok}>
      <button className={`h-7 min-w-[120px] px-2 py-0.5 rounded text-[13px] font-mono ${local ? 'bg-ok/15 text-ok border border-ok/30' : 'bg-bg-elevated text-muted border border-border'}`} onClick={() => { markDirty(); const v = !local; setLocal(v); onSave(path, v) }}>
        {local ? 'on' : 'off'}
      </button>
    </CfgRow>
  )
}

export default function KiroCrewCfgTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const { data: cfg = null, error: queryErr } = useQuery<KiroCrewCfg>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const err = queryErr ? (queryErr instanceof Error ? queryErr.message : String(queryErr)) : ''
  const [saveErr, setSaveErr] = useState('')
  // Which row's save failed, so the RAM Limits card owns its own refusals and
  // the two older cards keep showing everyone else's.
  const [saveErrPath, setSaveErrPath] = useState('')
  // Remount counters PER PATH: a refused save re-keys only its own row back to
  // the stored value, so a draft being typed in a neighbouring row survives.
  const [revs, setRevs] = useState<Record<string, number>>({})
  const rowKey = (path: string) => `${path}-${revs[path] ?? 0}`
  const [creatingWs, setCreatingWs] = useState(false)
  // Bumped on every open: a create still in flight from an earlier opening
  // (Cancel does not abort it) must not close the dialog reopened since.
  const [createGen, setCreateGen] = useState(0)
  const latestCreateGen = useRef(0)
  // The create dialog's typed fields live in main's WorkspaceForm; guard them
  // against a route change the way the crew editor's host does.
  const [createDirty, setCreateDirty] = useState(false)
  useSidePanelLeaveGuard(() => !createDirty || window.confirm(i18nT('pages.overview.promptsTab.discard_unsaved_changes')), createDirty)

  const reqId = useRef(0)

  const patchMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) => api.patchConfig(path, value),
    onSuccess: (updated) => { queryClient.setQueryData(['kirocrewConfig'], updated) },
    onError: (e: Error, { path }) => {
      setSaveErr(e.message)
      setSaveErrPath(path)
      setTimeout(() => setSaveErr(''), 4000)
      queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      setRevs(r => ({ ...r, [path]: (r[path] ?? 0) + 1 }))
    },
  })

  const save = (path: string, value: unknown) => {
    ++reqId.current
    patchMut.mutate({ path, value })
  }

  /** Which crewmate a new session starts as. Its own write through
   *  `PUT /api/config/default-agent` (owner-gated, refuses an unknown name)
   *  rather than a raw config PATCH, and its own notice: the row commits on
   *  change, so there is no draft the hand-off could lose. */
  const [defaultErr, setDefaultErr] = useState('')
  // Its own remount counter, not the per-row `revs`: bumping every row's here would
  // remount every control on the page when the default-crewmate request
  // settles, and a CfgNumber keeps its typed-but-uncommitted draft in local
  // state — so a value being typed into Pool size or Session timeout while the
  // request was in flight would silently revert to the stored one.
  const [defaultRev, setDefaultRev] = useState(0)
  // Writes run in SELECTION order. The trigger is never pending-gated, so a
  // second pick can land while the first PUT is in flight; two concurrent PUTs
  // may reach the server's config lock in either order, and the one the user
  // made FIRST could then persist LAST. Chaining each request behind the
  // previous one (settled either way) means the last pick is the last write.
  const defaultChain = useRef<Promise<unknown>>(Promise.resolve())
  const defaultMut = useMutation({
    mutationFn: (name: string) => {
      const next = defaultChain.current.then(() => api.setDefaultAgent(name))
      defaultChain.current = next.catch(() => undefined)
      return next
    },
    onSuccess: () => {
      setDefaultErr('')
      // The default is read by more than this table: the chat composer's
      // catalog (`useAgents`, keyed on the store's refreshTrigger) decides which
      // crewmate a NEW session binds to, and the shared ['default-agent'] /
      // ['kirocrew-agents'] queries feed the roster and the sidebar marker.
      // Refresh all of them here, as the roster's old picker did, so a session
      // opened right after the change starts as the crewmate just chosen rather
      // than the one the catalog still remembers.
      queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
      queryClient.invalidateQueries({ queryKey: ['default-agent'] })
      dispatch(triggerRefresh())
    },
    onError: (e: Error, name: string) => {
      // Only the endpoint's own "not a configured alias" refusal (400 with code
      // `default_agent_not_alias`) means the crewmate is gone; that one gets the
      // stable friendly sentence, since its phrasing has changed over time. Every
      // other failure -- owner denial, an unreadable config, a dropped connection
      // -- keeps its own reason, because "reload and pick again" is wrong advice
      // for those and the real cause would otherwise be discarded.
      const reason = isUnknownAliasRefusal(e)
        ? i18nT('pages.overview.kiroCrewCfgTab.default_crewmate_unknown_reason', { name })
        : e.message
      setDefaultErr(reason
        ? i18nT('pages.overview.kiroCrewCfgTab.default_crewmate_refused_because', { name, reason })
        : i18nT('pages.overview.kiroCrewCfgTab.default_crewmate_failed'))
      // On a failure the select must show what the config SAYS, not what was
      // picked: a refused pick would otherwise sit selected next to a table
      // still badging the old default. The bumped key remounts CfgSelect on the
      // stored value. Only THIS select remounts — and only on failure: on
      // success the invalidated config refetch carries the accepted value into
      // CfgSelect through its `value` effect, while a remount here would flash
      // the OLD default back for one refetch and swallow the ✓ tick.
      setDefaultRev(n => n + 1)
    },
  })

  if (err) return <Card><ErrorNotice message={err} askAgent /></Card>
  if (!cfg) return <Card><div className="skeleton h-40 rounded" /></Card>

  const agents = Object.entries(cfg.agents)
  const workspaces = Object.entries(cfg.workspaces)
  const stores = Object.entries(cfg.memory_stores)
  // A workspace write changes the config this tab renders and the list the
  // chat workspace picker reads.
  const workspacesChanged = () => {
    queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    queryClient.invalidateQueries({ queryKey: ['workspaces'] })
  }

  return (
    <>
      {/* Agents */}
      <Card>
        <CardTitle><Bot className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.kirocrew_agents')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.named_agent_definitions')} /></CardTitle>
        {agents.length === 0 ? (
          <EmptyState icon={<Bot className="lucide-inline" />} title={i18nT('pages.overview.kiroCrewCfgTab.no_agents_defined')} subtitle={i18nT('pages.overview.kiroCrewCfgTab.using_legacy_mode_agent_default_agent_as_agent_t')} />
        ) : (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.name')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.kiroCrewAgentsPage.built_from')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.workspace')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.memory_store')}</th>
              </tr>
            </thead>
            <tbody>
              {agents.map(([name, a]) => (
                <tr key={name}>
                  <td className="px-2.5 py-2 text-sm text-text font-medium">
                    {name} {name === cfg.default_agent && <Badge variant="aim">{i18nT('pages.overview.kiroCrewCfgTab.default')}</Badge>}
                  </td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.kiro_agent || '—'}</td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.workspace}</td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.memory_store}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {/* The Settings-side writer of the default crewmate. The Crewmates tab
            only marks it with a badge; the chat composer's ★ writes the same
            setting but only for the agent a session is bound to, and the picker
            withholds covered crewmates unless dashboard.crewmates_in_agent_picker is on — so without
            this row a user with several crewmates could not pick which one new
            sessions start as.
            Rendered whenever any crewmate exists — the roster badge deep-links
            here even with one, so the anchor must be on the page for the ring
            to land (a one-option select is a true statement, not a trap). */}
        {agents.length > 0 && (
          <div
            className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1"
            data-setting-key={DEFAULT_CREWMATE_HIGHLIGHT_ANCHOR}
            data-testid="cfg-default-crewmate-row"
          >
            {/* Keyed on the failure counter ONLY. Putting the stored value in
                the key would remount on every SUCCESSFUL change too (the
                refetched config carries the new value), which resets
                useDirtyTrack and swallows the ✓ tick; the `value` effect inside
                CfgSelect already carries an accepted value in without a remount. */}
            <CfgSelect
              key={`defaultagent-${defaultRev}`}
              label={i18nT('pages.overview.kiroCrewCfgTab.default_crewmate')}
              path="agent.default_agent"
              value={cfg.default_agent}
              options={agents.map(([name]) => name)}
              hint={i18nT('pages.overview.kiroCrewCfgTab.default_crewmate_hint')}
              onSave={(_path, name) => { setDefaultErr(''); defaultMut.mutate(name) }}
            />
            {/* No hand-off: it navigates to the chat and unmounts this page, and
                Subagent Settings below keeps its drafts in local state until its
                own Save — a hand-off here would throw them away. */}
            <ErrorNotice message={defaultErr} variant="inline" testId="cfg-default-crewmate-error" />
          </div>
        )}
      </Card>

      {/* Workspaces */}
      <Card>
        <div className="flex items-center justify-between gap-2 mb-3.5">
          <CardTitle className="mb-0"><FolderOpen className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.workspaces')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.named_workspace_directories_each_agent_binds_to')} /></CardTitle>
          <Btn onClick={() => { latestCreateGen.current = createGen + 1; setCreateGen(createGen + 1); setCreatingWs(true) }}><Plus className="lucide-inline" />{i18nT('pages.overview.kiroCrewCfgTab.new_workspace')}</Btn>
        </div>
        {/* The actions column is wide; scroll the table, not the pane, on a narrow screen. */}
        <div className="overflow-x-auto">
        <table className="w-full border-collapse table-striped">
          <thead>
            <tr>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.name')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.directory')}</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.used_by')}</th>
              <th className="border-b border-border"><span className="sr-only">{i18nT('pages.overview.memoryTab.actions')}</span></th>
            </tr>
          </thead>
          <tbody>
            {workspaces.map(([name, ws]) => {
              const usedBy = agents.filter(([, a]) => a.workspace === name).map(([n]) => n)
              return <WorkspaceRow key={name} name={name} dir={ws.dir} isDefault={name === cfg.default_workspace} usedBy={usedBy} onChanged={workspacesChanged} />
            })}
          </tbody>
        </table>
        </div>
        <WorkspaceModal
          open={creatingWs}
          workspaceOptions={workspaces.map(([n]) => n)}
          onClose={() => setCreatingWs(false)}
          onDirtyChange={setCreateDirty}
          onCreated={() => { if (createGen === latestCreateGen.current) setCreatingWs(false); workspacesChanged() }}
        />
      </Card>

      {/* Memory Stores */}
      <Card>
        <CardTitle><Brain className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.memory_stores')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.named_memory_stores_with_optional_per_store_embe')} /></CardTitle>
        {stores.length === 0 ? (
          <EmptyState icon={<Brain className="lucide-inline" />} title={i18nT('pages.overview.kiroCrewCfgTab.no_memory_stores')} subtitle={i18nT('pages.overview.kiroCrewCfgTab.using_global_memory_settings')} />
        ) : (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.name')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.description')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.embedding')}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.kiroCrewCfgTab.used_by')}</th>
              </tr>
            </thead>
            <tbody>
              {stores.map(([name, ms]) => {
                const usedBy = agents.filter(([, a]) => a.memory_store === name).map(([n]) => n)
                return (
                  <tr key={name}>
                    <td className="px-2.5 py-2 text-sm text-text font-medium">
                      {name} {name === cfg.default_memory_store && <Badge variant="ok">{i18nT('pages.overview.kiroCrewCfgTab.default')}</Badge>}
                    </td>
                    <td className="px-2.5 py-2 text-[13px] text-muted">{ms.description || '—'}</td>
                    <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{ms.embedding_provider || <span className="italic">{i18nT('pages.overview.kiroCrewCfgTab.inherited_provider', { provider: cfg.memory.embedding_provider })}</span>}</td>
                    <td className="px-2.5 py-2"><UsedByTags names={usedBy} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      {/* Subagent Settings */}
      <SubagentSettings cfg={cfg} onSaved={() => queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })} />

      {/* Memory limits: the free-memory floor that gates subagent starts and the
          two levels at which the agent is told memory is low. Each row saves on
          its own and applies without a restart. */}
      <Card>
        <CardTitle><Gauge className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.resource_thresholds')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.resource_thresholds_description')} /></CardTitle>
        {/* No hand-off: it navigates to the chat and unmounts this page, and
            Subagent Settings above keeps its drafts in local state until its own
            Save -- a hand-off here would throw them away. */}
        {saveErr && MEMORY_LIMIT_PATHS.has(saveErrPath) && (
          <ErrorNotice message={`${i18nT(MEMORY_LIMIT_LABEL_KEYS[saveErrPath])}: ${saveErr}`} variant="inline" />
        )}
        <p className="text-muted text-[12px] mb-2">{i18nT('pages.overview.kiroCrewCfgTab.resource_thresholds_zero_off')}</p>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <CfgNumber key={rowKey('agent.spawn_min_memory_gb')} label={i18nT('pages.overview.kiroCrewCfgTab.spawn_min_memory')} path="agent.spawn_min_memory_gb" value={cfg.agent.spawn_min_memory_gb ?? 2} suffix="GB" min={0} max={1024} step={0.5} help={i18nT('pages.overview.kiroCrewCfgTab.spawn_min_memory_hint')} onSave={save} />
          <CfgNumber key={rowKey('agent.resource_pressure_gb')} label={i18nT('pages.overview.kiroCrewCfgTab.memory_pressure_threshold')} path="agent.resource_pressure_gb" value={cfg.agent.resource_pressure_gb ?? 4} suffix="GB" min={0} max={1024} step={0.5} help={i18nT('pages.overview.kiroCrewCfgTab.memory_pressure_threshold_hint')} onSave={save} />
          <CfgNumber key={rowKey('agent.resource_critical_gb')} label={i18nT('pages.overview.kiroCrewCfgTab.memory_critical_threshold')} path="agent.resource_critical_gb" value={cfg.agent.resource_critical_gb ?? 2} suffix="GB" min={0} max={1024} step={0.5} help={i18nT('pages.overview.kiroCrewCfgTab.memory_critical_threshold_hint')} onSave={save} />
        </div>
      </Card>

      {/* Warm Pool */}
      {provider.capabilities.warmPool && (
      <Card>
        <CardTitle><Flame className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.warm_pool')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.warm_pool_description')} /></CardTitle>
        {/* No hand-off: it navigates to the chat and unmounts this page, and
            Subagent Settings keeps its drafts in local state until its own Save. */}
        {saveErr && !MEMORY_LIMIT_PATHS.has(saveErrPath) && <ErrorNotice message={saveErr} variant="inline" />}
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <CfgNumber key={rowKey('session.pool_size')} label={i18nT('pages.overview.kiroCrewCfgTab.pool_size')} path="session.pool_size" value={cfg.session.pool_size ?? 0} min={0} max={10} hint={i18nT('pages.overview.kiroCrewCfgTab.number_of_pre_spawned_processes_0_disables')} onSave={save} />
          <CfgSelect key={rowKey('session.pool_agent')} label={i18nT('pages.overview.kiroCrewCfgTab.pool_agent')} path="session.pool_agent" value={cfg.session.pool_agent ?? ''} options={['', ...Object.keys(cfg.agents)]} labels={{'': `(${cfg.default_agent || i18nT('pages.overview.kiroCrewCfgTab.default_agent')})`}} hint={i18nT('pages.overview.kiroCrewCfgTab.agent_for_pool_processes_empty_uses_default_agent')} onSave={save} />
          <CfgNumber key={rowKey('session.pool_ttl_secs')} label={i18nT('pages.overview.kiroCrewCfgTab.pool_ttl')} path="session.pool_ttl_secs" value={cfg.session.pool_ttl_secs} suffix="s" min={0} max={7200} hint={i18nT('pages.overview.kiroCrewCfgTab.max_age_for_pooled_processes_0_disables_expiry')} onSave={save} />
        </div>
      </Card>
      )}

      {/* Quick Info */}
      <Card>
        <CardTitle><Settings className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.config_summary')}</CardTitle>
        {/* No hand-off: it navigates to the chat and unmounts this page, and
            Subagent Settings keeps its drafts in local state until its own Save. */}
        {saveErr && !MEMORY_LIMIT_PATHS.has(saveErrPath) && <ErrorNotice message={saveErr} variant="inline" />}
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <div className={readonlyCls}><span className="text-muted"><Lock className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.provider')}</span><span className="text-text font-mono text-[13px]">{cfg.agent.provider}</span></div>
          <CfgSelect key={rowKey('agent.approval_mode')} label={i18nT('pages.overview.kiroCrewCfgTab.approval_mode')} path="agent.approval_mode" value={cfg.agent.approval_mode} options={['auto', 'interactive']} hint={i18nT('pages.overview.kiroCrewCfgTab.immediate_auto_approves_all_tools_interactive_as')} onSave={save} />
          <CfgNumber key={rowKey('session.timeout_secs')} label={i18nT('pages.overview.kiroCrewCfgTab.session_timeout')} path="session.timeout_secs" value={cfg.session.timeout_secs} suffix="s" min={60} max={86400} hint={i18nT('pages.overview.kiroCrewCfgTab.takes_effect_on_next_session_range_60_86400s')} onSave={save} />
          <CfgSelect key={rowKey('agent.sandbox')} label={i18nT('pages.overview.kiroCrewCfgTab.sandbox')} path="agent.sandbox" value={cfg.agent.sandbox} options={['auto', 'strict', 'off']} hint={i18nT('pages.overview.kiroCrewCfgTab.applies_to_sessions_started_after_the_change')} onSave={save} />
          <div className={readonlyCls}><span className="text-muted"><Lock className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.embedding_provider')}</span><span className="text-text font-mono text-[13px]">{cfg.memory.embedding_provider}</span></div>
          <CfgToggle key={rowKey('auto_update')} label={i18nT('pages.overview.kiroCrewCfgTab.auto_update')} path="auto_update" value={cfg.auto_update} hint={i18nT('pages.overview.kiroCrewCfgTab.next_update_check_cycle')} onSave={save} />
          <CfgToggle key={rowKey('agent.tool_search')} label={i18nT('pages.overview.kiroCrewCfgTab.mcp_tool_search')} path="agent.tool_search" value={cfg.agent.tool_search ?? true} hint={i18nT('pages.overview.kiroCrewCfgTab.enable_dynamic_mcp_tool_discovery_via_kiro_cli_t')} onSave={save} />
          <div className={readonlyCls}><span className="text-muted">{i18nT('pages.overview.kiroCrewCfgTab.max_channels')}</span><span className="text-text font-mono text-[13px]">{cfg.agent.max_channels}</span></div>
          <div className={readonlyCls}><span className="text-muted">{i18nT('pages.overview.kiroCrewCfgTab.max_channel_agents')}</span><span className="text-text font-mono text-[13px]">{cfg.agent.max_channel_agents}</span></div>
        </div>
      </Card>
    </>
  )
}

function SubagentSettings({ cfg, onSaved }: { cfg: KiroCrewCfg; onSaved: () => void }) {
  const [maxTurns, setMaxTurns] = useState(cfg.agent.subagent_max_turns ?? 100)
  const [maxSubs, setMaxSubs] = useState(cfg.agent.max_subagents ?? 3)
  const [autoMax, setAutoMax] = useState(cfg.agent.subagent_auto_max ?? 16)
  const hardCap = autoMax
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<ReactNode>('')
  const [msgOk, setMsgOk] = useState(false)

  // One effect per field, keyed on that field's stored value: a save from any
  // other row on this page replaces `cfg`, and resyncing on `cfg` itself would
  // throw away a draft typed here but not yet saved.
  const storedTurns = cfg.agent.subagent_max_turns ?? 100
  const storedSubs = cfg.agent.max_subagents ?? 3
  const storedAutoMax = cfg.agent.subagent_auto_max ?? 16
  useEffect(() => { setMaxTurns(storedTurns) }, [storedTurns])
  useEffect(() => { setMaxSubs(storedSubs) }, [storedSubs])
  useEffect(() => { setAutoMax(storedAutoMax) }, [storedAutoMax])

  const dirty = maxTurns !== (cfg.agent.subagent_max_turns ?? 100) || maxSubs !== (cfg.agent.max_subagents ?? 3) || autoMax !== (cfg.agent.subagent_auto_max ?? 16)

  const save = async () => {
    setSaving(true); setMsg('')
    try {
      const res = await api.saveKirocrewConfig({ subagent_max_turns: maxTurns, max_subagents: maxSubs, subagent_auto_max: autoMax })
      if (res.error) { setMsg(res.error); setMsgOk(false) } else { setMsg(<><Check className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.saved')}</>); setMsgOk(true); onSaved() }
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); setMsgOk(false) }
    finally { setSaving(false) }
  }

  return (
    <Card>
      <CardTitle><Bot className="lucide-inline" /> {i18nT('pages.overview.kiroCrewCfgTab.subagent_settings')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.controls_how_many_subagents_can_run_concurrently')} /></CardTitle>
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 max-[600px]:grid-cols-1">
        <label htmlFor="subagent-max-turns" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.max_turns_per_subagent')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.tool_call_budget_per_subagent_1_1000_default_100')} /></span>
          <input id="subagent-max-turns" aria-label={i18nT('pages.overview.kiroCrewCfgTab.max_turns_per_subagent')} type="number" min={1} max={1000} value={maxTurns} onChange={e => setMaxTurns(parseInt(e.target.value) || 1)}
            className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
        </label>
        <label htmlFor="subagent-max-concurrent" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.max_concurrent_subagents')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.maximum_subagents_running_at_once', { cap: hardCap })} /></span>
          <span className="inline-flex items-center gap-2">
            {maxSubs === 0 && <span className="text-[11px] text-muted">{i18nT('pages.overview.kiroCrewCfgTab.auto')}</span>}
            <input id="subagent-max-concurrent" aria-label={i18nT('pages.overview.kiroCrewCfgTab.max_concurrent_subagents')} type="number" min={0} max={hardCap} value={maxSubs} onChange={e => { const v = parseInt(e.target.value); setMaxSubs(Number.isNaN(v) ? 0 : Math.max(0, v)) }}
              className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
          </span>
        </label>
        {maxSubs === 0 && (
          <label htmlFor="subagent-auto-size-max" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
            <span className="text-muted inline-flex items-center gap-1">{i18nT('pages.overview.kiroCrewCfgTab.auto_size_max')} <InfoTip text={i18nT('pages.overview.kiroCrewCfgTab.ceiling_on_the_auto_sized_concurrent_subagent_co')} /></span>
            <input id="subagent-auto-size-max" aria-label={i18nT('pages.overview.kiroCrewCfgTab.auto_size_max')} type="number" min={1} max={64} value={autoMax} onChange={e => { const v = parseInt(e.target.value); setAutoMax(Number.isNaN(v) ? 1 : Math.min(64, Math.max(1, v))) }}
              className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
          </label>
        )}
      </div>
      <div className="flex items-center gap-3 mt-3">
        <button onClick={save} disabled={!dirty || saving}
          className="px-3 py-1.5 rounded text-sm font-medium bg-accent text-accent-fg hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed">
          {saving ? i18nT('pages.overview.kiroCrewCfgTab.saving') : i18nT('pages.overview.kiroCrewCfgTab.save')}
        </button>
        {msg && <span className={`text-[13px] ${msgOk ? 'text-ok' : 'text-danger'}`}>{msg}</span>}
      </div>
    </Card>
  )
}
