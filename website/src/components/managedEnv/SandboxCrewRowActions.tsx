/**
 * Stop, Resume and Delete on a Remote Crew row the managed-environment lane created.
 *
 * Drawn only on rows stamped `kiro_managed_env`, and only while the `crew_client` preview
 * is on: off, nothing here draws or calls a route. The environment's state comes from the
 * lane's List; an environment this Crew did not create gets no action (the server refuses
 * it too). The actions sit in the row's own detail block, one state button and Delete, so
 * the core's action row does not grow. Delete asks first. It sends the delete and removes
 * no row: once the List reads the environment as gone, the person takes the crew off the
 * list with the core Remove.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, Power, ServerOff } from 'lucide-react'
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { useConfirm } from '../ConfirmDialog'
import type { CrewRowActionProps } from '../remoteCrewRowActions'
import { managedEnvApi } from '../../api/managedEnvApi'
import type { ManagedEnvAction, ManagedEnvItem } from '../../api/managedEnvApi'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW_CLIENT, readPreviewFlag } from '../../utils/previewFlags'
import { i18nT } from '../../i18n/t'

export const SANDBOX_PROVISIONER_ID = 'kiro_managed_env'
export const MANAGED_ENV_LIST_KEY = ['managed-env-list'] as const
const LIST_EVERY_MS = 15_000

export type RowState = 'creating' | 'running' | 'stopped' | 'deleting' | 'gone' | 'error'

const ACTION_FAILED_KEY = {
  stop: 'managedEnv.row.stop_failed',
  resume: 'managedEnv.row.resume_failed',
  delete: 'managedEnv.row.delete_failed',
} as const satisfies Record<ManagedEnvAction, string>

const STATE_LABEL_KEY = {
  creating: 'managedEnv.row.state_creating',
  running: 'managedEnv.row.state_running',
  stopped: 'managedEnv.row.state_stopped',
  deleting: 'managedEnv.row.state_deleting',
  gone: 'managedEnv.row.gone',
  error: 'managedEnv.row.state_error',
} as const satisfies Record<RowState, string>

export function rowState(status: string): RowState {
  switch (status) {
    case 'PENDING':
    case 'CREATING':
      return 'creating'
    case 'RUNNING':
      return 'running'
    case 'STOPPING':
    case 'STOPPED':
      return 'stopped'
    case 'DELETING':
      return 'deleting'
    // GONE: a deleted own id the List no longer shows (`routes.list_entries`).
    case 'DELETED':
    case 'GONE':
      return 'gone'
    default:
      return 'error'
  }
}

/** The row's "Managed by" text, or undefined (the core's default line) while the preview
 *  is off: off, the row offers none of the actions the text names. */
export function sandboxManagedBy(): string | undefined {
  return readPreviewFlag(PREVIEW_CREW_CLIENT) ? i18nT('managedEnv.row.managed_by') : undefined
}

/** The lane's List, shared by every row. While the preview is off it is not read, and an
 *  answer cached before the switch went off gives no item, so no action is offered. */
function useManagedEnvItem(sshHost: string | undefined): {
  on: boolean
  item: ManagedEnvItem | undefined
  loaded: boolean
  error: unknown
  /** When the current error arrived, so a dismissal covers that failure only. */
  errorAt: number
} {
  const on = usePreviewFlag(PREVIEW_CREW_CLIENT)
  const list = useQuery({
    queryKey: MANAGED_ENV_LIST_KEY,
    queryFn: managedEnvApi.list,
    enabled: on,
    refetchInterval: LIST_EVERY_MS,
    retry: false,
  })
  const item = on ? list.data?.sandboxes.find((s) => s.own && s.ssh_host === sshHost) : undefined
  return { on, item, loaded: on && list.isSuccess, error: on ? list.error : null, errorAt: list.errorUpdatedAt }
}

export function SandboxCrewRowDetail({ inst, disabled }: CrewRowActionProps) {
  const { on, item, loaded, error, errorAt } = useManagedEnvItem(inst.ssh_host)
  // The failure the person dismissed: a later one shows again.
  const [dismissedAt, setDismissedAt] = useState(0)
  const qc = useQueryClient()
  const act = useMutation({
    // The id travels with the request: a Delete confirmed for one environment acts on
    // that one, even if the row has since been pointed at another.
    mutationFn: (req: { sandboxId: string; action: ManagedEnvAction }) =>
      managedEnvApi.act(req.sandboxId, req.action),
    onSettled: () => void qc.invalidateQueries({ queryKey: MANAGED_ENV_LIST_KEY }),
  })
  const { confirm, confirmDialog } = useConfirm()
  if (!on) return null

  const run = (sandboxId: string, action: ManagedEnvAction) => {
    if (action !== 'delete') {
      act.mutate({ sandboxId, action })
      return
    }
    void confirm({
      title: i18nT('managedEnv.row.delete_title', { name: inst.name }),
      body: i18nT('managedEnv.row.delete_body'),
      confirmLabel: i18nT('managedEnv.row.delete_confirm'),
    }).then((yes) => {
      // The switch can go off while the question is open.
      if (yes && readPreviewFlag(PREVIEW_CREW_CLIENT)) act.mutate({ sandboxId, action })
    })
  }

  let body = null
  let actions = null
  if (error && errorAt > dismissedAt) {
    body = (
      <ErrorNotice
        askAgent
        title={i18nT('managedEnv.row.list_failed')}
        message={(error as Error).message || i18nT('managedEnv.row.list_failed')}
        onDismiss={() => setDismissedAt(errorAt)}
      />
    )
  } else if (loaded) {
    // A row the List does not show at all reads gone too: nothing here can act on it.
    const state = item ? rowState(item.status) : 'gone'
    body =
      state === 'error' ? (
        // A failed environment is an outcome that failed, so it gets the error surface.
        <div data-state={state}>
          <ErrorNotice
            askAgent
            title={i18nT(STATE_LABEL_KEY[state])}
            message={i18nT('managedEnv.row.state_error_hint', { status: item?.status ?? '' })}
          />
        </div>
      ) : (
        <p className="text-xs text-[var(--text-secondary)]" data-state={state}>
          {i18nT(STATE_LABEL_KEY[state])}
        </p>
      )
    if (item && state !== 'gone') {
      const busy = disabled || act.isPending || state === 'deleting'
      const sid = item.sandbox_id
      // At most two buttons: the one state change that applies, then Delete.
      actions = (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {state === 'running' && (
            <Btn disabled={busy} onClick={() => run(sid, 'stop')}>
              <Power size={14} aria-hidden="true" /> {i18nT('managedEnv.row.stop')}
            </Btn>
          )}
          {state === 'stopped' && (
            <Btn disabled={busy || item.status === 'STOPPING'} onClick={() => run(sid, 'resume')}>
              <Play size={14} aria-hidden="true" /> {i18nT('managedEnv.row.resume')}
            </Btn>
          )}
          <Btn danger disabled={busy} onClick={() => run(sid, 'delete')}>
            <ServerOff size={14} aria-hidden="true" /> {i18nT('managedEnv.row.delete')}
          </Btn>
        </div>
      )
    }
  }
  return (
    <div className="mt-2">
      {body}
      {actions}
      {act.isError && (
        <ErrorNotice
          askAgent
          title={i18nT(ACTION_FAILED_KEY[act.variables?.action ?? 'stop'], { name: inst.name })}
          message={(act.error as Error).message}
          onDismiss={() => act.reset()}
        />
      )}
      {confirmDialog}
    </div>
  )
}
