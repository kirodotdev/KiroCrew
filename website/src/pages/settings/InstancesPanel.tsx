/**
 * InstancesPanel — Settings → Instances. Set up and manage remote KiroCrew
 * instances reachable over SSH tunnels (add / edit / connect / disconnect /
 * diagnose). This panel is the *control plane* only — it does not
 * embed remote dashboards. Once an instance is connected here, switch into it
 * from the tab strip in the top header (see InstanceTabBar / Stage 3).
 *
 * Self-contained on purpose: `connect` is idempotent server-side (re-connecting
 * an already-connected instance returns its live status + token), so the header
 * tab strip can obtain the iframe token independently without sharing in-memory
 * state with this panel.
 */
import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Server,
  Plus,
  Plug,
  Unplug,
  Trash2,
  RefreshCw,
  Stethoscope,
  AlertTriangle,
  X,
  Power,
} from 'lucide-react'
import { api, ApiError, type InstanceView, type InstanceTunnelStatus } from '../../api/client'
import { Card, Btn } from '../../components/ui'
import { useAppDispatch } from '../../store'
import { removeWarm } from '../../store/instancesSlice'

const STATE_DOT: Record<InstanceTunnelStatus['state'], string> = {
  connected: 'bg-success',
  connecting: 'bg-warning',
  error: 'bg-danger',
  stopped: 'bg-muted',
  disconnected: 'bg-muted',
}

/** Human-friendly duration ("3h 12m", "45m", "30s"). */
function humanizeSecs(secs: number): string {
  if (secs <= 0) return '0s'
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`
  if (m > 0) return `${m}m`
  return `${secs}s`
}

function StatusBadge({ status }: { status: InstanceTunnelStatus }) {
  const dot = STATE_DOT[status.state] ?? 'bg-muted'
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] text-muted">
      <span className={`inline-block w-2 h-2 rounded-full ${dot}`} aria-hidden />
      <span className="capitalize">{status.state}</span>
      {status.error ? <span className="text-danger truncate max-w-[240px]">— {status.error}</span> : null}
    </span>
  )
}

function AddInstanceForm({ onAdded, usedPorts }: { onAdded: () => void; usedPorts: number[] }) {
  const [name, setName] = useState('')
  const [sshHost, setSshHost] = useState('')
  const [remotePort, setRemotePort] = useState('7777')
  const [ttl, setTtl] = useState('20h')
  const [remoteBin, setRemoteBin] = useState('')

  const portNum = Number(remotePort) || 0
  const dupPort = portNum > 0 && usedPorts.includes(portNum)

  const addMutation = useMutation({
    mutationFn: () =>
      api.addInstance({
        name: name.trim(),
        ssh_host: sshHost.trim(),
        remote_port: Number(remotePort) || 7777,
        ttl: ttl.trim() || '20h',
        remote_bin: remoteBin.trim() || undefined,
      }),
    onSuccess: () => {
      setName('')
      setSshHost('')
      setRemotePort('7777')
      setTtl('20h')
      setRemoteBin('')
      onAdded()
    },
  })
  const err = addMutation.error
    ? addMutation.error instanceof ApiError
      ? addMutation.error.message
      : 'Failed to add instance'
    : ''

  const inputCls =
    'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none focus-ring'

  return (
    <Card>
      <div className="flex items-center gap-2 mb-3 text-text font-medium">
        <Plus className="lucide-inline" /> Add instance
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label htmlFor="add-instance-name" className="flex flex-col gap-1 text-[13px] text-muted">
          Name
          <input id="add-instance-name" aria-label="Name" className={inputCls} value={name} onChange={e => setName(e.target.value)} placeholder="Remote Host 1" />
        </label>
        <label htmlFor="add-instance-ssh-host" className="flex flex-col gap-1 text-[13px] text-muted">
          SSH host / alias
          <input id="add-instance-ssh-host" aria-label="SSH host / alias" className={inputCls} value={sshHost} onChange={e => setSshHost(e.target.value)} placeholder="host-1-alias" />
        </label>
        <label htmlFor="add-instance-remote-port" className="flex flex-col gap-1 text-[13px] text-muted">
          Remote port
          <input id="add-instance-remote-port" aria-label="Remote port" className={inputCls} value={remotePort} onChange={e => setRemotePort(e.target.value)} placeholder="7777" inputMode="numeric" />
          <span className="text-[12px] text-muted leading-snug">
            Must match the port the remote gateway serves on (its{' '}
            <code className="text-text">dashboard.url</code>). Each connected instance
            needs a <strong>distinct</strong> port — the local forward mirrors it.
          </span>
          {dupPort ? (
            <span className="text-[12px] text-danger leading-snug">
              Port {portNum} is already used by another instance — choose a different one
              (and configure that port on the remote host).
            </span>
          ) : null}
        </label>
        <label htmlFor="add-instance-ttl" className="flex flex-col gap-1 text-[13px] text-muted">
          Token TTL
          <input id="add-instance-ttl" aria-label="Token TTL" className={inputCls} value={ttl} onChange={e => setTtl(e.target.value)} placeholder="20h" />
        </label>
        <label htmlFor="add-instance-remote-bin" className="flex flex-col gap-1 text-[13px] text-muted sm:col-span-2">
          Remote kirocrew path <span className="text-muted-strong">(optional)</span>
          <input
            id="add-instance-remote-bin"
            aria-label="Remote kirocrew path"
            className={inputCls}
            value={remoteBin}
            onChange={e => setRemoteBin(e.target.value)}
            placeholder="/home/you/.local/bin/kirocrew  —  leave blank for standard installs"
          />
          <span className="text-[12px] text-muted leading-snug">
            Only needed if <code className="text-text">kirocrew</code> is installed somewhere
            non-standard on the remote. Leave blank for a normal pip install on PATH. To find
            it, run on the remote host: <code className="text-text">command -v kirocrew</code>{' '}
            (commonly <code className="text-text">~/.local/bin/kirocrew</code>).
            Use an absolute path (no <code className="text-text">~</code>).
          </span>
        </label>
      </div>
      {err ? <div className="mt-3 text-[13px] text-danger">{err}</div> : null}
      <div className="mt-3">
        <Btn
          primary
          onClick={() => addMutation.mutate()}
          disabled={addMutation.isPending || !name.trim() || !sshHost.trim() || dupPort}
        >
          {addMutation.isPending ? 'Adding…' : 'Add instance'}
        </Btn>
      </div>
      <p className="mt-2 text-[12px] text-muted">
        The gateway opens an SSH tunnel and mints a short-lived token on connect.
        The local forward port mirrors the remote port, so each connected instance
        must use a distinct remote port.
      </p>
    </Card>
  )
}

function InstanceRow({
  inst,
  busy,
  onConnect,
  onDisconnect,
  onRemove,
  onDiagnose,
}: {
  inst: InstanceView
  busy: string
  onConnect: (id: string) => void
  onDisconnect: (id: string) => void
  onRemove: (id: string) => void
  onDiagnose: (id: string) => void
}) {
  const connected = inst.status.state === 'connected'
  const ttl = inst.status.token_ttl_remaining
  const diag = inst.status.diagnosis
  return (
    <div className="flex items-center justify-between gap-3 py-2.5 border-b border-border last:border-b-0">
      <div className="min-w-0">
        <div className="text-text text-sm font-medium truncate">{inst.name}</div>
        <div className="text-[12px] text-muted truncate">
          {inst.ssh_host} · port {inst.remote_port} · ttl {inst.ttl}
          {typeof ttl === 'number' ? ` · token ${humanizeSecs(ttl)} left` : ''}
        </div>
        <div className="mt-1"><StatusBadge status={inst.status} /></div>
        {diag && !diag.ok ? (
          <div className="mt-1 text-[12px] text-warning"><AlertTriangle size={12} className="lucide-inline" /> {diag.reason}</div>
        ) : null}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Btn onClick={() => onDiagnose(inst.id)} disabled={!!busy} aria-label={`Diagnose ${inst.name}`}>
          <Stethoscope className="lucide-inline" /> {busy === `diagnose:${inst.id}` ? '…' : 'Diagnose'}
        </Btn>
        {connected ? (
          <Btn onClick={() => onDisconnect(inst.id)} disabled={!!busy}>
            <Unplug className="lucide-inline" /> Disconnect
          </Btn>
        ) : (
          <Btn primary onClick={() => onConnect(inst.id)} disabled={!!busy}>
            <Plug className="lucide-inline" /> {busy === `connect:${inst.id}` ? 'Connecting…' : 'Connect'}
          </Btn>
        )}
        <Btn danger onClick={() => onRemove(inst.id)} disabled={!!busy} aria-label={`Remove ${inst.name}`}>
          <Trash2 className="lucide-inline" />
        </Btn>
      </div>
    </div>
  )
}

export function InstancesPanel() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const [actionErr, setActionErr] = useState<string | null>(null)
  const [diagNote, setDiagNote] = useState<{ kind: 'ok' | 'info' | 'warn'; text: string } | null>(null)
  const [connectedNote, setConnectedNote] = useState<string | null>(null)
  // True after the user toggles the feature flag but before a gateway restart —
  // drives the "restart required" hint (the flag only takes effect at startup).
  const [restartPending, setRestartPending] = useState(false)
  const errMsg = useCallback(
    (e: unknown, fallback: string) =>
      e instanceof ApiError ? e.message : e instanceof Error ? e.message : fallback,
    [],
  )
  const clearNotices = useCallback(() => {
    setActionErr(null)
    setDiagNote(null)
    setConnectedNote(null)
  }, [])

  const instancesQuery = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances() })
  const disabled =
    instancesQuery.error instanceof ApiError &&
    instancesQuery.error.status === 403 &&
    /disabled/i.test(instancesQuery.error.message)
  const error =
    instancesQuery.error && !disabled
      ? instancesQuery.error instanceof ApiError
        ? instancesQuery.error.message
        : 'Failed to load instances'
      : ''
  const loading = instancesQuery.isLoading
  const instances = useMemo(() => instancesQuery.data?.instances ?? [], [instancesQuery.data])
  const warmCap = instancesQuery.data?.warm_set_cap || 5
  // Runtime usability: true only when the SSH manager is actually running.
  // enabled (data present, no 403) but !active => the flag was set after the
  // gateway started, so a restart is required to activate it.
  const active = instancesQuery.data?.active ?? false
  const reload = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['instances'] })
  }, [queryClient])

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onMutate: clearNotices,
    onSuccess: (st, id) => {
      if (st.state === 'connected') {
        const name = instances.find(i => i.id === id)?.name || id
        setConnectedNote(`Connected “${name}”. Switch to it from the tab strip in the top header.`)
      } else {
        setActionErr(st.error || 'Connection did not complete. Try Diagnose for details.')
      }
    },
    onError: (e, id) => setActionErr(`Connect to ${id} failed: ${errMsg(e, 'unknown error')}`),
    onSettled: () => reload(),
  })
  const disconnectMutation = useMutation({
    mutationFn: (id: string) => api.disconnectInstance(id),
    onMutate: clearNotices,
    // Explicit disconnect is the ONLY action that removes a tab: dropping the
    // warm iframe here, together with the backend clearing was_connected, makes
    // the header tab disappear (the tab strip keys on was_connected || warm).
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(`Disconnect of ${id} failed: ${errMsg(e, 'unknown error')}`),
    onSettled: () => reload(),
  })
  const removeMutation = useMutation({
    mutationFn: async (id: string) => {
      await api.disconnectInstance(id).catch(() => {})
      await api.removeInstance(id)
    },
    onMutate: clearNotices,
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(`Remove of ${id} failed: ${errMsg(e, 'unknown error')}`),
    onSettled: () => reload(),
  })
  const diagnoseMutation = useMutation({
    mutationFn: (id: string) => api.instanceStatus(id, true),
    onMutate: clearNotices,
    onSuccess: (st, id) => {
      const code = st.diagnosis?.code
      const reason = st.diagnosis?.reason || st.error
      if (!reason) return
      const kind = code === 'ok' ? 'ok' : code === 'not_connected' ? 'info' : 'warn'
      setDiagNote({ kind, text: `${id}: ${reason}` })
    },
    onError: (e, id) => setActionErr(`Diagnose of ${id} failed: ${errMsg(e, 'unknown error')}`),
    onSettled: () => reload(),
  })
  // Toggle the instances.enabled config flag from the UI (no CLI). The change
  // only takes effect after a gateway restart (manager + CSP init at startup),
  // so we flag restartPending and the panel surfaces a "restart required" hint.
  const setEnabledMutation = useMutation({
    mutationFn: (next: boolean) => api.patchConfig('instances.enabled', next),
    onMutate: clearNotices,
    onSuccess: () => {
      setRestartPending(true)
      reload()
    },
    onError: e => setActionErr(`Failed to update setting: ${errMsg(e, 'unknown error')}`),
  })

  const busy = connectMutation.isPending
    ? `connect:${connectMutation.variables}`
    : disconnectMutation.isPending
      ? `disconnect:${disconnectMutation.variables}`
      : removeMutation.isPending
        ? `remove:${removeMutation.variables}`
        : diagnoseMutation.isPending
          ? `diagnose:${diagnoseMutation.variables}`
          : ''

  const onConnect = useCallback((id: string) => connectMutation.mutate(id), [connectMutation])
  const onDisconnect = useCallback((id: string) => disconnectMutation.mutate(id), [disconnectMutation])
  const onRemove = useCallback((id: string) => removeMutation.mutate(id), [removeMutation])
  const onDiagnose = useCallback((id: string) => diagnoseMutation.mutate(id), [diagnoseMutation])

  if (disabled) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-text font-medium mb-1">
          <Server className="lucide-inline" /> Multi-instance management is off
        </div>
        <p className="text-[13px] text-muted mb-3">
          Enable it to let this gateway open SSH tunnels to your remote KiroCrews and switch
          between them from the top tab bar.
        </p>
        {restartPending && (
          <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warning/10 text-warning border border-warning/30">
            <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
            <span>
              Disabled in config. Restart the gateway (<code className="text-text">kirocrew restart</code>){' '}
              to fully tear down any tunnels still running from before.
            </span>
          </div>
        )}
        <Btn primary onClick={() => setEnabledMutation.mutate(true)} disabled={setEnabledMutation.isPending}>
          <Power className="lucide-inline" /> {setEnabledMutation.isPending ? 'Enabling…' : 'Enable multi-instance management'}
        </Btn>
        {actionErr && <div className="mt-2 text-[13px] text-danger">{actionErr}</div>}
        <p className="mt-2 text-[12px] text-muted">
          Equivalent CLI: <code className="text-text">kirocrew config set instances.enabled true</code> then{' '}
          <code className="text-text">kirocrew restart</code>.
        </p>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      {/* Enabled-state header: status dot + Disable toggle. The feature is on in
          config; `active` reflects whether the SSH manager is actually running. */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-[13px]">
          <span className={`inline-block w-2 h-2 rounded-full ${active ? 'bg-success' : 'bg-warning'}`} aria-hidden />
          <span className="text-muted">
            Multi-instance management is <span className="text-text font-medium">enabled</span>
            {active ? '' : ' — not active until restart'}
          </span>
        </div>
        <Btn onClick={() => setEnabledMutation.mutate(false)} disabled={setEnabledMutation.isPending} aria-label="Disable multi-instance management">
          <Power className="lucide-inline" /> {setEnabledMutation.isPending ? 'Disabling…' : 'Disable'}
        </Btn>
      </div>
      {!active && (
        <div role="status" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-warning/10 text-warning border border-warning/30">
          <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span>
            Enabled, but not active yet. Restart the gateway (<code className="text-text">kirocrew restart</code>){' '}
            to start the SSH tunnel manager and activate instance switching.
          </span>
        </div>
      )}
      {connectedNote && (
        <div role="status" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-success/10 text-success border border-success/30">
          <Plug size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{connectedNote}</span>
          <button type="button" aria-label="Dismiss" className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setConnectedNote(null)}><X size={12} /></button>
        </div>
      )}
      {actionErr && (
        <div role="alert" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-danger/10 text-danger border border-danger/30">
          <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{actionErr}</span>
          <button type="button" aria-label="Dismiss error" className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setActionErr(null)}><X size={12} /></button>
        </div>
      )}
      {diagNote && (
        <div
          role="status"
          className={
            'flex items-start gap-2 px-3 py-2 text-[13px] rounded-md border ' +
            (diagNote.kind === 'ok'
              ? 'bg-success/10 text-success border-success/30'
              : diagNote.kind === 'info'
                ? 'bg-accent/10 text-accent border-accent/30'
                : 'bg-warning/10 text-warning border-warning/30')
          }
        >
          <Stethoscope size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{diagNote.text}</span>
          <button type="button" aria-label="Dismiss diagnosis" className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setDiagNote(null)}><X size={12} /></button>
        </div>
      )}

      {loading ? (
        <Card>
          <div className="flex items-center gap-2 text-muted text-sm">
            <RefreshCw className="lucide-inline animate-spin" /> Loading…
          </div>
        </Card>
      ) : error ? (
        <Card>
          <div className="text-danger text-sm">{error}</div>
          <div className="mt-2">
            <Btn onClick={() => reload()}>
              <RefreshCw className="lucide-inline" /> Retry
            </Btn>
          </div>
        </Card>
      ) : (
        <>
          {instances.length > 0 ? (
            <Card>
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2 text-text font-medium">
                  <Server className="lucide-inline" /> Configured instances
                </div>
                <Btn onClick={() => reload()} aria-label="Refresh">
                  <RefreshCw className="lucide-inline" />
                </Btn>
              </div>
              <div>
                {instances.map(inst => (
                  <InstanceRow
                    key={inst.id}
                    inst={inst}
                    busy={busy}
                    onConnect={onConnect}
                    onDisconnect={onDisconnect}
                    onRemove={onRemove}
                    onDiagnose={onDiagnose}
                  />
                ))}
              </div>
              <p className="mt-2 text-[12px] text-muted">
                Up to {warmCap} instances stay warm (live tunnel) at once; the rest reconnect on
                demand. Tune with{' '}
                <code className="text-text">kirocrew config set instances.warm_set_cap N</code>.
              </p>
            </Card>
          ) : (
            <Card>
              <div className="text-[13px] text-muted">
                No instances configured yet. Add one below to manage a remote KiroCrew.
              </div>
            </Card>
          )}
          <AddInstanceForm onAdded={reload} usedPorts={instances.map(i => i.remote_port)} />
        </>
      )}
    </div>
  )
}
