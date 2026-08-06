import { useState, useMemo } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { AlertTriangle, Anchor, Link2, Lock } from 'lucide-react'
import { api } from '../api/client'
import { useProvider } from '../providers'
import { Card, CardTitle, PageHeader, StatCard, Btn, SendBtn, Input, Badge, SearchInput, EmptyState } from '../components/ui'
import InfoTip from '../components/InfoTip'
import SimpleSelect from '../components/SimpleSelect'
import { esc } from '../api/helpers'
import { timeAgo as _timeAgo } from '../utils/timeAgo'
import { useSortableTable } from '../hooks/useSortableTable'
import SortableHeader from '../components/SortableHeader'

import { i18nT } from '../i18n/t'
interface Hook {
  id: string; name: string; event: string; matcher: string
  command: string; timeout: number; enabled: boolean
  last_run: number; last_status: string; run_count: number
}

/** Result payload from POST /api/hooks/:id/test. */
interface HookTestResult {
  exit_code?: number
  duration_ms?: number
  error?: string
  stdout?: string
  stderr?: string
}

const EVENTS = ['AgentSpawn', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop']

const EVENT_STYLE: Record<string, string> = {
  AgentSpawn: 'bg-accent/15 text-accent border-accent/30',
  UserPromptSubmit: 'bg-ok-subtle text-ok border-ok/30',
  PreToolUse: 'bg-aim-subtle text-aim border-aim/30',
  PostToolUse: 'bg-aim-subtle text-aim border-aim/30',
  Stop: 'bg-warn-subtle text-warn border-warn/30',
}

const EVENT_BADGE: Record<string, 'ok' | 'err' | 'warn' | 'aim'> = {
  AgentSpawn: 'ok', UserPromptSubmit: 'ok',
  PreToolUse: 'aim', PostToolUse: 'aim', Stop: 'warn',
}

const EVENT_ORDER = Object.fromEntries(EVENTS.map((e, i) => [e, i]))

const normalizeEvent = (e: string) => e.charAt(0).toUpperCase() + e.slice(1)

function timeAgo(ts: number): string {
  if (!ts) return 'never'
  return _timeAgo(ts)
}

function HookForm({ hook, onSave, onCancel }: {
  hook?: Hook; onSave: (data: Partial<Hook>) => void; onCancel: () => void
}) {
  const [name, setName] = useState(hook?.name || '')
  const [event, setEvent] = useState(hook?.event || 'UserPromptSubmit')
  const [matcher, setMatcher] = useState(hook?.matcher || '')
  const [command, setCommand] = useState(hook?.command || '')
  const [timeout, setTimeout_] = useState(hook?.timeout || 30)
  const isToolHook = event === 'PreToolUse' || event === 'PostToolUse'

  return (
    <Card>
      <CardTitle>{hook ? i18nT('pages.hooksPage.edit_hook') : i18nT('pages.hooksPage.new_hook_2')} <InfoTip text={i18nT('pages.hooksPage.script_hooks_fire_shell_commands_on_chat_lifecyc')} /></CardTitle>
      <div className="flex flex-col gap-3">
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder={i18nT('pages.hooksPage.hook_name')} value={name} onChange={e => setName(e.target.value)} />
          <SimpleSelect
            options={EVENTS}
            value={event}
            onChange={setEvent}
            // A hook stored with an event this picker no longer offers (legacy
            // or hand-edited config) matches no row. A native <select> silently
            // displayed the FIRST option while state held the stale value; show
            // the stored value instead.
            triggerFallback={event}
            aria-label={i18nT('pages.hooksPage.event')}
          />
        </div>
        <div>
          <Input className="w-full font-mono" placeholder={i18nT('pages.hooksPage.echo_hook_fired')} value={command} onChange={e => setCommand(e.target.value)} />
        </div>
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder={isToolHook ? i18nT('pages.hooksPage.matcher_tool_filter_e_g_fs_write_git') : i18nT('pages.hooksPage.matcher_optional_e_g_deploy')} value={matcher} onChange={e => setMatcher(e.target.value)} />
          <div className="flex items-center gap-1.5 text-[13px] text-muted shrink-0">
            <span>{i18nT('pages.hooksPage.timeout')}</span>
            <Input type="number" min={1} max={300} className="w-16" value={timeout} onChange={e => setTimeout_(parseInt(e.target.value, 10) || 30)} />
            <span>{i18nT('pages.hooksPage.s')}</span>
          </div>
          <SendBtn onClick={() => onSave({ name, event, matcher, command, timeout })}>{i18nT('pages.hooksPage.save')}</SendBtn>
          <Btn onClick={onCancel} className="h-9 px-4 text-sm font-semibold rounded-lg">{i18nT('pages.hooksPage.cancel')}</Btn>
        </div>
      </div>
    </Card>
  )
}

export default function HooksPage({ embedded }: { embedded?: boolean } = {}) {
  const provider = useProvider()
  const { data: hooks = [], isLoading: loading, error: hooksErr, refetch: refresh } = useQuery<Hook[]>({
    queryKey: ['hooks'],
    queryFn: () => api.hooks().then((r: { hooks?: Hook[] }) => r.hooks || []),
  })
  const error = hooksErr ? i18nT('pages.hooksPage.failed_to_load_hooks', { error: hooksErr instanceof Error ? hooksErr.message : String(hooksErr) }) : null
  const { data: providerHooks = {}, error: providerHookErr } = useQuery({
    queryKey: ['provider-hooks', provider.id],
    queryFn: () => provider.fetchProviderHooks(),
    enabled: provider.capabilities.hooks,
  })
  const providerHookError = providerHookErr ? `Failed to load ${provider.labels.hooksSection.toLowerCase()}` : null
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ id: string; data: HookTestResult } | null>(null)
  const [filter, setFilter] = useState('')

  const mutOpts = { onSuccess: () => refresh(), onError: (e: Error) => e }
  const createMut = useMutation({ mutationFn: (data: Partial<Hook>) => api.createHook(data), ...mutOpts, onSuccess: () => { setCreating(false); refresh() } })
  const updateMut = useMutation({ mutationFn: ({ id, data }: { id: string; data: Partial<Hook> }) => api.updateHook(id, data), ...mutOpts, onSuccess: () => { setEditing(null); refresh() } })
  const deleteMut = useMutation({ mutationFn: (id: string) => api.deleteHook(id), ...mutOpts })
  const toggleMut = useMutation({ mutationFn: (id: string) => api.toggleHook(id), ...mutOpts })
  const testMut = useMutation({ mutationFn: (id: string) => api.testHook(id), onSuccess: (r: { result: HookTestResult }, id: string) => { setTestResult({ id, data: r.result }); refresh() } })

  const mutError = createMut.error?.message || updateMut.error?.message || deleteMut.error?.message || toggleMut.error?.message || testMut.error?.message || null
  const handleCreate = (data: Partial<Hook>) => createMut.mutate(data)
  const handleUpdate = (id: string, data: Partial<Hook>) => updateMut.mutate({ id, data })
  const handleDelete = (id: string) => deleteMut.mutate(id)
  const handleToggle = (id: string) => toggleMut.mutate(id)
  const handleTest = (id: string) => { setTestResult(null); testMut.mutate(id) }

  const enabled = hooks.filter(h => h.enabled).length
  const totalRuns = hooks.reduce((s, h) => s + h.run_count, 0)
  const lastErr = hooks.filter(h => h.last_status === 'error').length
  const filtered = useMemo(
    () => hooks.filter(h => !filter || (h.name + ' ' + h.event + ' ' + h.command + ' ' + h.matcher)
      .toLowerCase().includes(filter.toLowerCase())),
    [hooks, filter],
  )
  const hookComparators = useMemo(() => ({
    name: (a: Hook, b: Hook) => a.name.localeCompare(b.name),
    event: (a: Hook, b: Hook) => a.event.localeCompare(b.event),
    runs: (a: Hook, b: Hook) => a.run_count - b.run_count,
    status: (a: Hook, b: Hook) => (a.last_status || '').localeCompare(b.last_status || ''),
    lastRun: (a: Hook, b: Hook) => (a.last_run || 0) - (b.last_run || 0),
  }), [])
  const { sorted: sortedHooks, sort: hookSort, toggle: toggleHookSort } = useSortableTable(filtered, 'hooks', hookComparators, { key: 'name', dir: 'asc' })

  if (loading) return <div className="p-6 text-muted">{i18nT('pages.hooksPage.loading')}</div>

  const content = (
    <>
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
        {[
          { label: i18nT('pages.hooksPage.total'), value: hooks.length, accent: true },
          { label: i18nT('pages.hooksPage.enabled'), value: enabled },
          { label: i18nT('pages.hooksPage.total_runs'), value: totalRuns },
          { label: i18nT('pages.hooksPage.errors'), value: lastErr },
        ].map((s, i) => (
          <StatCard key={s.label} label={s.label} value={s.value} delay={i * 60} accent={s.accent} />
        ))}
        </div>

        {(error || mutError) && (
          <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
            <span className="text-danger text-lg shrink-0"><AlertTriangle className="lucide-inline" /></span>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-danger font-medium">{i18nT('pages.hooksPage.error')}</div>
              <div className="text-[13px] text-danger/90 mt-0.5">{error || mutError}</div>
            </div>
            <Btn onClick={() => { createMut.reset(); updateMut.reset(); deleteMut.reset(); toggleMut.reset(); testMut.reset() }} className="text-danger/60 hover:text-danger shrink-0">×</Btn>
          </div>
        )}

        {creating ? (
          <HookForm onSave={handleCreate} onCancel={() => setCreating(false)} />
        ) : (
          <div className="flex items-center gap-2 mb-4">
            <SendBtn onClick={() => { setCreating(true); setEditing(null) }}>{i18nT('pages.hooksPage.new_hook')}</SendBtn>
          </div>
        )}

        {editing && (() => {
          const h = hooks.find(x => x.id === editing)
          return h ? <HookForm hook={h} onSave={data => handleUpdate(h.id, data)} onCancel={() => setEditing(null)} /> : null
        })()}

        <Card>
          <CardTitle>{i18nT('pages.hooksPage.hooks')} <InfoTip text={i18nT('pages.hooksPage.hooks_run_shell_commands_on_chat_events_agentspa')} /></CardTitle>
          <div className="mb-3"><SearchInput placeholder={i18nT('pages.hooksPage.filter_hooks')} value={filter} onChange={e => setFilter(e.target.value)} /></div>
          {hooks.length === 0 ? (
            <EmptyState icon={<Anchor className="lucide-inline" />} title={i18nT('pages.hooksPage.no_hooks_yet')} subtitle={i18nT('pages.hooksPage.create_a_hook_to_run_scripts_on_chat_events')} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse table-striped">
                <thead>
                  <tr>
                    <th aria-label={i18nT('pages.hooksPage.enabled')} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[52px]"></th>
                    <SortableHeader label={i18nT('pages.hooksPage.name')} sortKey="name" sort={hookSort} onToggle={toggleHookSort} className="w-[120px]" />
                    <SortableHeader label={i18nT('pages.hooksPage.event')} sortKey="event" sort={hookSort} onToggle={toggleHookSort} className="w-[130px]" />
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium min-w-[200px]">{i18nT('pages.hooksPage.command')}</th>
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[120px]">{i18nT('pages.hooksPage.matcher')}</th>
                    <SortableHeader label={i18nT('pages.hooksPage.runs')} sortKey="runs" sort={hookSort} onToggle={toggleHookSort} className="w-[60px]" />
                    <SortableHeader label={i18nT('pages.hooksPage.status')} sortKey="status" sort={hookSort} onToggle={toggleHookSort} className="w-[80px]" />
                    <SortableHeader label={i18nT('pages.hooksPage.last_run')} sortKey="lastRun" sort={hookSort} onToggle={toggleHookSort} className="w-[90px]" />
                    <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium w-[160px]">{i18nT('pages.hooksPage.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.length === 0 ? (
                    <tr><td colSpan={9} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.hooksPage.no_matching_hooks')}</td></tr>
                  ) : sortedHooks.map(h => (
                    <tr key={h.id} className={`hover:bg-bg-hover transition-colors ${h.enabled ? '' : 'opacity-50'}`}>
                      <td className="px-2.5 py-2 border-b border-border">
                        <button
                          className={`w-9 h-5 rounded-full relative transition-colors cursor-pointer ${h.enabled ? 'bg-accent' : 'bg-border'}`}
                          onClick={() => handleToggle(h.id)}
                          aria-label={h.enabled ? i18nT('pages.hooksPage.disable_hook') : i18nT('pages.hooksPage.enable_hook')}
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${h.enabled ? 'left-[18px]' : 'left-0.5'}`} />
                        </button>
                      </td>
                      <td className="px-2.5 py-2 border-b border-border text-sm font-medium text-text">{esc(h.name)}</td>
                      <td className="px-2.5 py-2 border-b border-border text-sm"><span className={`px-1.5 py-[2px] rounded-full text-[11px] font-bold border font-mono ${EVENT_STYLE[h.event] || 'bg-bg-elevated text-muted border-border'}`}>{h.event}</span></td>
                      <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-text/80 truncate max-w-[300px]" title={h.command}>{esc(h.command)}</td>
                      <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{h.matcher ? esc(h.matcher) : <span className="italic">—</span>}</td>
                      <td className="px-2.5 py-2 border-b border-border text-sm font-mono">{h.run_count}</td>
                      <td className="px-2.5 py-2 border-b border-border text-sm">
                        {!h.last_status ? <span className="text-muted italic">—</span>
                          : h.last_status === 'ok' ? <Badge variant="ok">{i18nT('pages.hooksPage.ok')}</Badge>
                          : h.last_status === 'error' ? <Badge variant="err">{i18nT('pages.hooksPage.error')}</Badge>
                          : <Badge variant="warn">{h.last_status}</Badge>}
                      </td>
                      <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{timeAgo(h.last_run)}</td>
                      <td aria-label={i18nT('pages.hooksPage.actions')} className="px-2.5 py-2 border-b border-border text-sm">
                        <div className="flex gap-1.5">
                          <Btn onClick={() => handleTest(h.id)} className="bg-accent/10 text-accent border-accent/30 hover:bg-accent/20">{i18nT('pages.hooksPage.test')}</Btn>
                          <Btn onClick={() => { setEditing(h.id); setCreating(false) }}>{i18nT('pages.hooksPage.edit')}</Btn>
                          <Btn danger onClick={() => { if (window.confirm(i18nT('pages.hooksPage.delete_hook', { name: h.name }))) handleDelete(h.id) }}>{i18nT('pages.hooksPage.delete')}</Btn>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {testResult && (() => {
            const h = hooks.find(x => x.id === testResult.id)
            return (
              <div className="mt-3 bg-bg-elevated border border-border rounded-lg p-4 animate-scale-in">
                <div className="flex items-center gap-3 mb-2">
                  <span className="text-sm font-medium text-text">{i18nT('pages.hooksPage.test_result')}{h ? `: ${h.name}` : ''}</span>
                  <Badge variant={testResult.data.exit_code === 0 ? 'ok' : 'err'}>{testResult.data.exit_code === 0 ? 'OK' : `exit ${testResult.data.exit_code}`}</Badge>
                  <span className="text-[12px] text-muted font-mono">{testResult.data.duration_ms}{i18nT('pages.hooksPage.ms')}</span>
                  <Btn onClick={() => setTestResult(null)} className="ml-auto">×</Btn>
                </div>
                {testResult.data.error && <div className="text-[13px] text-danger mb-1">{testResult.data.error}</div>}
                {testResult.data.stdout && <pre className="whitespace-pre-wrap text-[12px] font-mono text-text/80 bg-bg border border-border rounded-md p-3 max-h-[200px] overflow-auto">{testResult.data.stdout}</pre>}
                {testResult.data.stderr && <pre className="whitespace-pre-wrap text-[12px] font-mono text-warn bg-bg border border-border rounded-md p-3 max-h-[100px] overflow-auto mt-2">{testResult.data.stderr}</pre>}
              </div>
            )
          })()}
        </Card>
        {provider.capabilities.hooks && (
        <Card>
          <CardTitle>{provider.labels.hooksSection} <InfoTip text={i18nT('pages.hooksPage.read_only_view_of_provider_hooks', { path: provider.labels.configFile || i18nT('pages.hooksPage.config') })} /></CardTitle>
          {providerHookError ? (
            <EmptyState icon={<AlertTriangle className="lucide-inline text-warning" />} title={i18nT('pages.hooksPage.failed_to_load', { section: provider.labels.hooksSection.toLowerCase() })} subtitle={i18nT('pages.hooksPage.check_your_connection_or_configuration_and_try_a')} />
          ) : Object.values(providerHooks).some(entries => entries.length > 0) ? (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse table-striped">
                <thead>
                  <tr>
                    {[{ h: '#', w: 'w-[40px]' }, { h: i18nT('pages.hooksPage.event'), w: 'w-[150px]' }, { h: i18nT('pages.hooksPage.source'), w: 'w-[90px]' }, { h: i18nT('pages.hooksPage.matcher'), w: 'w-[120px]' }, { h: i18nT('pages.hooksPage.command'), w: 'min-w-[300px]' }].map(c => (
                      <th key={c.h} className={`text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium ${c.w}`}>{c.h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(() => {
                    let order = 0
                    return Object.entries(providerHooks).sort(([a], [b]) => (EVENT_ORDER[normalizeEvent(a)] ?? 999) - (EVENT_ORDER[normalizeEvent(b)] ?? 999)).map(([event, entries]) =>
                      entries.map((entry, i) => {
                        order++
                        return (
                          <tr key={`${event}-${i}`} className={`hover:bg-bg-hover transition-colors ${entry.source === 'bundled' ? 'bg-bg-elevated/50' : ''}`}>
                            <td className="px-2.5 py-2 border-b border-border text-[12px] text-muted font-mono">{order}</td>
                            <td className="px-2.5 py-2 border-b border-border text-sm"><Badge variant={EVENT_BADGE[normalizeEvent(event)] || 'warn'}>{normalizeEvent(event)}</Badge></td>
                            <td className="px-2.5 py-2 border-b border-border text-sm">{entry.source === 'bundled' ? <span className="inline-flex items-center gap-1"><Lock className="w-3 h-3 text-muted" /><Badge variant="ok">{i18nT('pages.hooksPage.bundled')}</Badge></span> : <Badge variant="warn">{i18nT('pages.hooksPage.user')}</Badge>}</td>
                            <td className="px-2.5 py-2 border-b border-border text-sm text-muted">{entry.matcher ? entry.matcher : <span className="italic">—</span>}</td>
                            <td className="px-2.5 py-2 border-b border-border text-sm font-mono text-text/80" title={entry.command}><div className="truncate max-w-[400px]">{entry.command}</div></td>
                          </tr>
                        )
                      })
                    )
                  })()}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState icon={<Link2 className="lucide-inline" />} title={i18nT('pages.hooksPage.none_configured', { section: provider.labels.hooksSection.toLowerCase() })} subtitle={provider.labels.configFile ? i18nT('pages.hooksPage.configure_via', { path: provider.labels.configFile }) : ''} />
          )}
        </Card>
        )}
      </>
  )

  if (embedded) return content

  return (
    <>
      <PageHeader title={i18nT('pages.hooksPage.hooks')} subtitle={i18nT('pages.hooksPage.shell_commands_that_run_automatically_on_agent_e')} />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {content}
      </div>
    </>
  )
}
