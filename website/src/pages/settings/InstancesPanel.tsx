/**
 * InstancesPanel — legacy control plane for remote Kiro Crew instances
 * reachable over SSH tunnels (add / edit / connect / disconnect / diagnose).
 * No longer routed as a settings tab: Settings → Remote Instances renders
 * RemoteCrewPanel, which reuses AddInstanceForm and StatusBadge from this
 * file. This panel is the *control plane* only — it does not
 * embed remote dashboards. Once an instance is connected here, switch into it
 * from the tab strip in the top header (see InstanceTabBar).
 *
 * Self-contained on purpose: `connect` is idempotent server-side (re-connecting
 * an already-connected instance returns its live status + token), so the header
 * tab strip can obtain the iframe token independently without sharing in-memory
 * state with this panel.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
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
import { useAppDispatch, useAppSelector } from '../../store'
import { removeWarm, setCrewAddForm } from '../../store/instancesSlice'

import { i18nT } from '../../i18n/t'
import { fmtDuration, fmtUnit } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { Trans } from 'react-i18next'

import { SettingRef } from '../../components/settingRef/SettingRef'
import {
  InstanceFormFields,
  useInstanceFormState,
  isBlankInstanceForm,
  EMPTY_INSTANCE_FORM,
} from './InstanceFormFields'
const STATE_DOT: Record<InstanceTunnelStatus['state'], string> = {
  connected: 'bg-ok',
  connecting: 'bg-warn',
  error: 'bg-danger',
  stopped: 'bg-muted',
  disconnected: 'bg-muted',
}

/** Human-friendly duration ("3h 12m", "45m", "30s"). */
export function humanizeSecs(secs: number): string {
  if (secs <= 0) return fmtUnit(0, 'second', { maximumFractionDigits: 0 })
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  if (h > 0) return fmtDuration([[h, 'hour'], [m, 'minute']], { dropZero: true })
  if (m > 0) return fmtUnit(m, 'minute', { maximumFractionDigits: 0 })
  return fmtUnit(secs, 'second', { maximumFractionDigits: 0 })
}

export function StatusBadge({ status }: { status: InstanceTunnelStatus }) {
  const dot = STATE_DOT[status.state] ?? 'bg-muted'
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] text-muted">
      <span className={`inline-block w-2 h-2 rounded-full ${dot}`} aria-hidden />
      <span className="capitalize">{status.state}</span>
      {status.error ? <span className="text-danger truncate max-w-[240px]">— {status.error}</span> : null}
    </span>
  )
}

export function AddInstanceForm({ onAdded }: { onAdded: () => void }) {
  // Resume what the user typed before a hand-off navigated them away. Read once
  // per mount: a later read would fight the live form state.
  const dispatch = useAppDispatch()
  const stored = useAppSelector(s => s.instances.crewForms?.add ?? null)
  const [restored] = useState(() => stored)
  const form = useInstanceFormState(EMPTY_INSTANCE_FORM, restored)

  // Hold the values on every change, not only when this card's own banner hands
  // off. The navigation unmounts the WHOLE panel, so the crew rows above carry
  // their own "Ask the agent" links that destroy this form just as thoroughly — as
  // does a sidebar click or the browser's back button. Making this a property of
  // the form rather than of one button covers every exit instead of the one wired.
  //
  // Undebounced deliberately: there is no write to batch. This is a store dispatch
  // rather than storage, so running per keystroke costs a reducer call — while any
  // debounce window is a window where a sibling row's button loses the fields.
  useEffect(() => {
    // Erasing the form erases the held values, or navigating away and back would
    // restore text the user deliberately cleared.
    dispatch(setCrewAddForm(isBlankInstanceForm(form.values) ? null : { ...form.values }))
  }, [form.values, dispatch])

  const addMutation = useMutation({
    mutationFn: () => api.addInstance(form.body()),
    onSuccess: () => {
      // The values described a crew that now exists; keeping them would pre-fill
      // the next add with the one just created.
      dispatch(setCrewAddForm(null))
      form.reset(EMPTY_INSTANCE_FORM)
      onAdded()
    },
  })
  const err = addMutation.error
    ? addMutation.error instanceof ApiError
      ? addMutation.error.message
      : i18nT('pages.settings.instancesPanel.failed_to_add_remote_crew')
    : ''

  return (
    <Card>
      <div className="flex items-center gap-2 mb-3 text-text font-medium">
        <Plus className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.add_instance')}
      </div>
      <InstanceFormFields idPrefix="add-instance" form={form} />
      {/* `askAgent` is safe here BECAUSE the typed values live above the route: the
          hand-off navigates away and unmounts this form, and a first-time user has
          just typed up to nine fields by hand. Nothing has to be persisted at click
          time, so there is no failure to veto — coming back re-seeds from the store. */}
      <ErrorNotice
        message={err}
        className="mt-3"
        askAgent
      />
      <div className="mt-3">
        <Btn primary onClick={() => addMutation.mutate()} disabled={addMutation.isPending || !form.valid}>
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
          <div className="mt-1 text-[12px] text-warn"><AlertTriangle size={12} className="lucide-inline" /> {diag.reason}</div>
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
          <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warn/10 text-warn border border-warn/30">
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
        <ErrorNotice message={actionErr} askAgent className="mt-2" />
        <p className="mt-2 text-[12px] text-muted">
          <Trans
            i18nKey="pages.settings.instancesPanel.enable_via_setting"
            components={{
              settingRef: <SettingRef configKey="instances.enabled" />,
              restartCmd: <code className="text-text">kirocrew restart</code>,
            }}
          />
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
          <span className={`inline-block w-2 h-2 rounded-full ${active ? 'bg-ok' : 'bg-warn'}`} aria-hidden />
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
        <div role="status" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-warn/10 text-warn border border-warn/30">
          <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span>
            {i18nT('pages.settings.instancesPanel.enabled_but_not_active_yet_restart_the_gateway')}<code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>){' '}
            {i18nT('pages.settings.instancesPanel.to_start_the_ssh_tunnel_manager_and_activate_ins')}
          </span>
        </div>
      )}
      {connectedNote && (
        <div role="status" className="flex items-start gap-2 px-3 py-2 text-[13px] rounded-md bg-ok/10 text-ok border border-ok/30">
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
              ? 'bg-ok/10 text-ok border-ok/30'
              : diagNote.kind === 'info'
                ? 'bg-accent/10 text-accent border-accent/30'
                : 'bg-warn/10 text-warn border-warn/30')
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
                <SettingRef configKey="instances.warm_set_cap" />.
              </p>
            </Card>
          ) : (
            <Card>
              <div className="text-[13px] text-muted">
                {i18nT('pages.settings.instancesPanel.no_instances_configured_yet_add_one_below_to_man')}
              </div>
            </Card>
          )}
          <AddInstanceForm onAdded={reload} />
        </>
      )}
    </div>
  )
}
