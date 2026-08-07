/**
 * InstancesPanel — Settings → Instances. Set up and manage remote KiroCrew
 * instances reachable over SSH tunnels (add / edit / connect / disconnect /
 * diagnose). This panel is the *control plane* only — it does not
 * embed remote dashboards. Once an instance is connected here, switch into it
 * from the tab strip in the top header (see InstanceTabBar).
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
import SimpleSelect from '../../components/SimpleSelect'
import { useAppDispatch } from '../../store'
import { removeWarm } from '../../store/instancesSlice'

import { i18nT } from '../../i18n/t'
import { fmtDuration, fmtUnit } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
const STATE_DOT: Record<InstanceTunnelStatus['state'], string> = {
  connected: 'bg-success',
  connecting: 'bg-warning',
  error: 'bg-danger',
  stopped: 'bg-muted',
  disconnected: 'bg-muted',
}

/** Human-friendly duration ("3h 12m", "45m", "30s"). */
function humanizeSecs(secs: number): string {
  if (secs <= 0) return fmtUnit(0, 'second', { maximumFractionDigits: 0 })
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h > 0) return fmtDuration([[h, 'hour'], [m, 'minute']], { dropZero: true })
  if (m > 0) return fmtUnit(m, 'minute', { maximumFractionDigits: 0 })
  return fmtUnit(secs, 'second', { maximumFractionDigits: 0 })
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
  const [method, setMethod] = useState<'ssh' | 'ssm'>('ssh')
  const [sshHost, setSshHost] = useState('')
  const [ssmTarget, setSsmTarget] = useState('')
  const [awsProfile, setAwsProfile] = useState('')
  const [awsRegion, setAwsRegion] = useState('')
  const [ssmRunAs, setSsmRunAs] = useState('')
  const [remotePort, setRemotePort] = useState('7777')
  const [ttl, setTtl] = useState('20h')
  const [remoteBin, setRemoteBin] = useState('')

  const portNum = Number(remotePort) || 0
  const dupPort = portNum > 0 && usedPorts.includes(portNum)
  const isSsm = method === 'ssm'
  // The transport-specific required field: ssh_host for SSH, ssm_target for SSM.
  const targetFilled = isSsm ? !!ssmTarget.trim() : !!sshHost.trim()

  const addMutation = useMutation({
    mutationFn: () =>
      api.addInstance({
        name: name.trim(),
        connection_method: method,
        ...(isSsm
          ? {
              ssm_target: ssmTarget.trim(),
              aws_profile: awsProfile.trim() || undefined,
              aws_region: awsRegion.trim() || undefined,
              ssm_run_as: ssmRunAs.trim() || undefined,
            }
          : { ssh_host: sshHost.trim() }),
        remote_port: Number(remotePort) || 7777,
        ttl: ttl.trim() || '20h',
        remote_bin: remoteBin.trim() || undefined,
      }),
    onSuccess: () => {
      setName('')
      setSshHost('')
      setSsmTarget('')
      setAwsProfile('')
      setAwsRegion('')
      setRemotePort('7777')
      setTtl('20h')
      setRemoteBin('')
      onAdded()
    },
  })
  const err = addMutation.error
    ? addMutation.error instanceof ApiError
      ? addMutation.error.message
      : i18nT('pages.settings.instancesPanel.failed_to_add_remote_crew')
    : ''

  const inputCls =
    'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none focus-ring'

  return (
    <Card>
      <div className="flex items-center gap-2 mb-3 text-text font-medium">
        <Plus className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.add_instance')}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <label htmlFor="add-instance-name" className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.name')}
          <input id="add-instance-name" aria-label={i18nT('pages.settings.instancesPanel.name')} className={inputCls} value={name} onChange={e => setName(e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.remote_host_1')} />
        </label>
        {/* Not a <label>: SimpleSelect renders a button, so `htmlFor` would point
            at no form control. The caption text stays put and the accessible name
            moves to the trigger's aria-label (same key). */}
        <div className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.connection_method')}
          <SimpleSelect
            options={['ssh', 'ssm']}
            optionLabels={[i18nT('pages.settings.instancesPanel.ssh_tunnel'), i18nT('pages.settings.instancesPanel.aws_ssm_session_manager')]}
            value={method}
            onChange={v => setMethod(v as 'ssh' | 'ssm')}
            aria-label={i18nT('pages.settings.instancesPanel.connection_method')}
          />
          <span className="text-[12px] text-muted leading-snug">
            {isSsm
              ? i18nT('pages.settings.instancesPanel.tunnels_via_aws_ssm_start_session_no_inbound_ssh')
              : i18nT('pages.settings.instancesPanel.opens_ssh_n_l_to_the_host_requires_non_interacti')}
          </span>
        </div>
        {isSsm ? (
          <>
            <label htmlFor="add-instance-ssm-target" className="flex flex-col gap-1 text-[13px] text-muted">
              {i18nT('pages.settings.instancesPanel.ssm_target_instance_id')}
              <input id="add-instance-ssm-target" aria-label={i18nT('pages.settings.instancesPanel.ssm_target_instance_id')} className={inputCls} value={ssmTarget} onChange={e => setSsmTarget(e.target.value)} placeholder="i-0123456789abcdef0" />
              <span className="text-[12px] text-muted leading-snug">
                {i18nT('pages.settings.instancesPanel.ec2_instance_id_i_or_ssm_managed_instance_id_mi')}
              </span>
            </label>
            <label htmlFor="add-instance-aws-profile" className="flex flex-col gap-1 text-[13px] text-muted">
              {i18nT('pages.settings.instancesPanel.aws_profile')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
              <input id="add-instance-aws-profile" aria-label={i18nT('pages.settings.instancesPanel.aws_profile')} className={inputCls} value={awsProfile} onChange={e => setAwsProfile(e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.default_credential_chain')} />
            </label>
            <label htmlFor="add-instance-aws-region" className="flex flex-col gap-1 text-[13px] text-muted">
              {i18nT('pages.settings.instancesPanel.aws_region')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
              <input id="add-instance-aws-region" aria-label={i18nT('pages.settings.instancesPanel.aws_region')} className={inputCls} value={awsRegion} onChange={e => setAwsRegion(e.target.value)} placeholder="us-east-1" />
            </label>
            <label htmlFor="add-instance-ssm-run-as" className="flex flex-col gap-1 text-[13px] text-muted">
              {i18nT('pages.settings.instancesPanel.remote_user')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
              <input id="add-instance-ssm-run-as" aria-label={i18nT('pages.settings.instancesPanel.remote_user')} className={inputCls} value={ssmRunAs} onChange={e => setSsmRunAs(e.target.value)} placeholder="ec2-user" />
              <span className="text-[12px] text-muted leading-snug">
                {i18nT('pages.settings.instancesPanel.the_user_the_remote_gateway_runs_as_sudo_u_for_s')}
              </span>
            </label>
          </>
        ) : (
          <label htmlFor="add-instance-ssh-host" className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.ssh_host_alias')}
            <input id="add-instance-ssh-host" aria-label={i18nT('pages.settings.instancesPanel.ssh_host_alias')} className={inputCls} value={sshHost} onChange={e => setSshHost(e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.host_1_alias')} />
          </label>
        )}
        <label htmlFor="add-instance-remote-port" className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.remote_port')}
          <input id="add-instance-remote-port" aria-label={i18nT('pages.settings.instancesPanel.remote_port')} className={inputCls} value={remotePort} onChange={e => setRemotePort(e.target.value)} placeholder="7777" inputMode="numeric" />
          <span className="text-[12px] text-muted leading-snug">
            {i18nT('pages.settings.instancesPanel.must_match_the_port_the_remote_gateway_serves_on')}
          </span>
          {dupPort ? (
            <span className="text-[12px] text-danger leading-snug">
              {i18nT('pages.settings.instancesPanel.port')} {portNum} {i18nT('pages.settings.instancesPanel.is_already_used_by_another_instance_choose_a_dif')}
            </span>
          ) : null}
        </label>
        <label htmlFor="add-instance-ttl" className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.token_ttl')}
          <input id="add-instance-ttl" aria-label={i18nT('pages.settings.instancesPanel.token_ttl')} className={inputCls} value={ttl} onChange={e => setTtl(e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.20h')} />
        </label>
        <label htmlFor="add-instance-remote-bin" className="flex flex-col gap-1 text-[13px] text-muted sm:col-span-2">
          {i18nT('pages.settings.instancesPanel.remote_kirocrew_path')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
          <input
            id="add-instance-remote-bin"
            aria-label={i18nT('pages.settings.instancesPanel.remote_kirocrew_path')}
            className={inputCls}
            value={remoteBin}
            onChange={e => setRemoteBin(e.target.value)}
            placeholder={i18nT('pages.settings.instancesPanel.home_you_local_bin_kirocrew_leave_blank_for_stan')}
          />
          <span className="text-[12px] text-muted leading-snug">
            {i18nT('pages.settings.instancesPanel.only_needed_if')} <code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew')}</code> {i18nT('pages.settings.instancesPanel.is_installed_somewhere_non_standard_on_the_remot')} <code className="text-text">{i18nT('pages.settings.instancesPanel.command_v_kirocrew')}</code>{' '}
            {i18nT('pages.settings.instancesPanel.commonly')} <code className="text-text">{i18nT('pages.settings.instancesPanel.local_bin_kirocrew')}</code>{i18nT('pages.settings.instancesPanel.use_an_absolute_path_no')} <code className="text-text">~</code>).
          </span>
        </label>
      </div>
      <ErrorNotice message={err} className="mt-3" />
      <div className="mt-3">
        <Btn
          primary
          onClick={() => addMutation.mutate()}
          disabled={addMutation.isPending || !name.trim() || !targetFilled || dupPort}
        >
          {addMutation.isPending ? i18nT('pages.settings.instancesPanel.adding') : i18nT('pages.settings.instancesPanel.add_remote_crew')}
        </Btn>
      </div>
      <p className="mt-2 text-[12px] text-muted">
        {i18nT('pages.settings.instancesPanel.the_gateway_opens_an_ssh_tunnel_and_mints_a_shor')}
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
          <span className="uppercase tracking-wide text-muted-strong">
            {inst.connection_method === 'ssm' ? 'SSM' : 'SSH'}
          </span>{' '}
          {inst.connection_method === 'ssm' ? inst.ssm_target : inst.ssh_host}
          {inst.connection_method === 'ssm' && inst.aws_region ? ` (${inst.aws_region})` : ''}{' '}
          {i18nT('pages.settings.instancesPanel.port_2')} {inst.remote_port} {i18nT('pages.settings.instancesPanel.ttl')} {inst.ttl}
          {typeof ttl === 'number' ? ' ' + i18nT('pages.settings.instancesPanel.token_left', { time: humanizeSecs(ttl) }) : ''}
        </div>
        <div className="mt-1"><StatusBadge status={inst.status} /></div>
        {diag && !diag.ok ? (
          <div className="mt-1 text-[12px] text-warning"><AlertTriangle size={12} className="lucide-inline" /> {diag.reason}</div>
        ) : null}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Btn onClick={() => onDiagnose(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.diagnose_2', { name: inst.name })}>
          <Stethoscope className="lucide-inline" /> {busy === `diagnose:${inst.id}` ? '…' : i18nT('pages.settings.instancesPanel.diagnose')}
        </Btn>
        {connected ? (
          <Btn onClick={() => onDisconnect(inst.id)} disabled={!!busy}>
            <Unplug className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.disconnect')}
          </Btn>
        ) : (
          <Btn primary onClick={() => onConnect(inst.id)} disabled={!!busy}>
            <Plug className="lucide-inline" /> {busy === `connect:${inst.id}` ? i18nT('pages.settings.instancesPanel.connecting') : i18nT('pages.settings.instancesPanel.connect')}
          </Btn>
        )}
        <Btn danger onClick={() => onRemove(inst.id)} disabled={!!busy} aria-label={i18nT('pages.settings.instancesPanel.remove', { name: inst.name })}>
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
        : i18nT('pages.settings.instancesPanel.failed_to_load_remote_crews')
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
        setConnectedNote(i18nT('pages.settings.instancesPanel.connected_switch_from_tab_strip', { name }))
      } else {
        setActionErr(st.error || i18nT('pages.settings.instancesPanel.connection_did_not_complete_try_diagnose_for_det'))
      }
    },
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.connect_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: () => reload(),
  })
  const disconnectMutation = useMutation({
    mutationFn: (id: string) => api.disconnectInstance(id),
    onMutate: clearNotices,
    // Explicit disconnect is the ONLY action that removes a tab: dropping the
    // warm iframe here, together with the backend clearing was_connected, makes
    // the header tab disappear (the tab strip keys on was_connected || warm).
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.disconnect_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: () => reload(),
  })
  const removeMutation = useMutation({
    mutationFn: async (id: string) => {
      await api.disconnectInstance(id).catch(() => {})
      await api.removeInstance(id)
    },
    onMutate: clearNotices,
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.remove_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
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
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.diagnose_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
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
    onError: e => setActionErr(i18nT('pages.settings.instancesPanel.update_setting_failed', { error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
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
          <Server className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.multi_instance_management_is_off')}
        </div>
        <p className="text-[13px] text-muted mb-3">
          {i18nT('pages.settings.instancesPanel.enable_it_to_let_this_gateway_open_ssh_tunnels_t')}
        </p>
        {restartPending && (
          <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warning/10 text-warning border border-warning/30">
            <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
            <span>
              {i18nT('pages.settings.instancesPanel.disabled_in_config_restart_the_gateway')}<code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>){' '}
              {i18nT('pages.settings.instancesPanel.to_fully_tear_down_any_tunnels_still_running_fro')}
            </span>
          </div>
        )}
        <Btn primary onClick={() => setEnabledMutation.mutate(true)} disabled={setEnabledMutation.isPending}>
          <Power className="lucide-inline" /> {setEnabledMutation.isPending ? i18nT('pages.settings.instancesPanel.enabling') : i18nT('pages.settings.instancesPanel.enable_remote_crew_management')}
        </Btn>
        <ErrorNotice message={actionErr} className="mt-2" />
        <p className="mt-2 text-[12px] text-muted">
          {i18nT('pages.settings.instancesPanel.equivalent_cli')} <code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_config_set_instances_enabled_true')}</code> {i18nT('pages.settings.instancesPanel.then')}{' '}
          <code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>.
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
            {i18nT('pages.settings.instancesPanel.multi_instance_management_is')} <span className="text-text font-medium">{i18nT('pages.settings.instancesPanel.enabled')}</span>
            {active ? '' : ' — not active until restart'}
          </span>
        </div>
        <Btn onClick={() => setEnabledMutation.mutate(false)} disabled={setEnabledMutation.isPending} aria-label={i18nT('pages.settings.instancesPanel.disable_multi_instance_management')}>
          <Power className="lucide-inline" /> {setEnabledMutation.isPending ? i18nT('pages.settings.instancesPanel.disabling') : i18nT('pages.settings.instancesPanel.disable')}
        </Btn>
      </div>
      {!active && (
        <div role="status" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-warning/10 text-warning border border-warning/30">
          <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span>
            {i18nT('pages.settings.instancesPanel.enabled_but_not_active_yet_restart_the_gateway')}<code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>){' '}
            {i18nT('pages.settings.instancesPanel.to_start_the_ssh_tunnel_manager_and_activate_ins')}
          </span>
        </div>
      )}
      {connectedNote && (
        <div role="status" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-success/10 text-success border border-success/30">
          <Plug size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{connectedNote}</span>
          <button type="button" aria-label={i18nT('pages.settings.instancesPanel.dismiss')} className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setConnectedNote(null)}><X size={12} /></button>
        </div>
      )}
      {actionErr && (
        <div role="alert" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-danger/10 text-danger border border-danger/30">
          <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{actionErr}</span>
          <button type="button" aria-label={i18nT('pages.settings.instancesPanel.dismiss_error')} className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setActionErr(null)}><X size={12} /></button>
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
          <button type="button" aria-label={i18nT('pages.settings.instancesPanel.dismiss_diagnosis')} className="shrink-0 opacity-70 hover:opacity-100" onClick={() => setDiagNote(null)}><X size={12} /></button>
        </div>
      )}

      {loading ? (
        <Card>
          <div className="flex items-center gap-2 text-muted text-sm">
            <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.instancesPanel.loading')}
          </div>
        </Card>
      ) : error ? (
        <Card>
          <div className="text-danger text-sm">{error}</div>
          <div className="mt-2">
            <Btn onClick={() => reload()}>
              <RefreshCw className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.retry')}
            </Btn>
          </div>
        </Card>
      ) : (
        <>
          {instances.length > 0 ? (
            <Card>
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2 text-text font-medium">
                  <Server className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.configured_instances')}
                </div>
                <Btn onClick={() => reload()} aria-label={i18nT('pages.settings.instancesPanel.refresh')}>
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
                {i18nT('pages.settings.instancesPanel.up_to')} {warmCap} {i18nT('pages.settings.instancesPanel.instances_stay_warm_live_tunnel_at_once_the_rest')}{' '}
                <code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_config_set_instances_warm_set_cap_n')}</code>.
              </p>
            </Card>
          ) : (
            <Card>
              <div className="text-[13px] text-muted">
                {i18nT('pages.settings.instancesPanel.no_instances_configured_yet_add_one_below_to_man')}
              </div>
            </Card>
          )}
          <AddInstanceForm onAdded={reload} usedPorts={instances.map(i => i.remote_port)} />
        </>
      )}
    </div>
  )
}
