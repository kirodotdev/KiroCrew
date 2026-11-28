/**
 * The "Managed environment" launch form on Remote Crew's setup tab.
 *
 * The panel draws it only when the server lists the `kiro_managed_env` provisioner, which
 * it does only while the `crew_client` feature preview is on. The form asks for the crew
 * name and starts the core's launch; the server's own preflight checks every setting, the
 * sign-in and the service, and the panel shows the progress card and any error. Launch is
 * held until this computer's ssh config has the lane's block, which one button beside
 * Launch adds. Below it, the person's environments another client made (Kiro web, the IDE,
 * another Crew) each take Add, which adds that environment as the crew named above.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Link2, Plus, Rocket } from 'lucide-react'
import { Btn, Input } from '../ui'
import ErrorNotice from '../ErrorNotice'
import type { RemoteProvisionerFormProps } from '../remoteProvisionerRenderers'
import { api } from '../../api/client'
import { managedEnvApi } from '../../api/managedEnvApi'
import { MANAGED_ENV_LIST_KEY, STATE_LABEL_KEY, rowState } from './SandboxCrewRowActions'
import { i18nT } from '../../i18n/t'

const SSH_CONFIG_KEY = ['managed-env-ssh-config'] as const

/** The ssh block's state (read only) and the write that adds it. */
function useSshSettings() {
  const qc = useQueryClient()
  // staleTime 0: the file can change outside the dashboard, so every mount and focus reads
  // it again rather than trusting an old "present" (the engine checks it live anyway).
  const state = useQuery({ queryKey: SSH_CONFIG_KEY, queryFn: managedEnvApi.sshConfig, retry: false, staleTime: 0 })
  const add = useMutation({
    mutationFn: managedEnvApi.addSshConfig,
    onSuccess: (data) => qc.setQueryData(SSH_CONFIG_KEY, data),
  })
  return { state, add }
}

/** What the ssh step does, the lines it adds, and a failed write. Its button sits beside
 *  Launch, which it unlocks. */
function SshSettings({ state, add }: ReturnType<typeof useSshSettings>) {
  const failure = add.error ?? state.error
  const box = useRef<HTMLDivElement>(null)
  // The form sits in a scrolling dialog; a failure below the fold would go unseen.
  useEffect(() => {
    if (failure) box.current?.scrollIntoView?.({ block: 'nearest' })
  }, [failure])
  return (
    <div className="space-y-2" ref={box}>
      {state.data?.present ? (
        <p className="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]" data-testid="managed-env-ssh-ready">
          <Check size={14} aria-hidden="true" className="text-ok" />
          {i18nT('managedEnv.form.ssh_ready', { path: state.data.path })}
        </p>
      ) : (
        <>
          <p className="text-xs text-[var(--text-secondary)]">
            {i18nT('managedEnv.form.ssh_config_note', { path: state.data?.path ?? '~/.ssh/config' })}
          </p>
          {state.data?.block && (
            <details className="text-xs text-[var(--text-secondary)]" data-testid="managed-env-ssh-preview">
              <summary className="cursor-pointer">{i18nT('managedEnv.form.ssh_preview')}</summary>
              <p className="mt-1">{i18nT('managedEnv.form.ssh_preview_why', { hosts: 'crew-sbx-*' })}</p>
              <pre className="mt-1 whitespace-pre-wrap break-all rounded-md bg-bg-elevated p-2 font-mono text-[11px] text-text">
                {state.data.block}
              </pre>
            </details>
          )}
        </>
      )}
      {/* No hand-off: this sits in the setup flow, whose crew name is still unsaved —
          navigating away discards it. */}
      {failure && (
        <ErrorNotice
          title={i18nT('managedEnv.form.ssh_failed')}
          message={`${i18nT('managedEnv.form.ssh_failed_hint', { path: state.data?.path ?? '~/.ssh/config' })} ${(failure as Error).message}`}
          onDismiss={() => {
            add.reset()
            void state.refetch()
          }}
        />
      )}
    </div>
  )
}

/** Listed states the server refuses to add: gone or on the way out. */
const NOT_ADDABLE = new Set(['DELETED', 'DELETING', 'FAILED'])

/** The person's environments another client made, each with Add. The server checks the id
 *  against a fresh List for this sign-in, then writes the crew row; Crew never deletes it.
 *  An added id whose crew row is gone (a failed row write, or the core Remove) is offered
 *  again, so Add can write the row. */
function OtherEnvironments({ name, nameOk, sshReady, disabled, onAdded }: {
  name: string
  nameOk: boolean
  sshReady: boolean
  disabled: boolean
  /** The add for `name` succeeded. */
  onAdded: (name: string) => void
}) {
  const qc = useQueryClient()
  // staleTime 0: environments are made elsewhere, so every mount and focus reads again.
  const list = useQuery({ queryKey: MANAGED_ENV_LIST_KEY, queryFn: managedEnvApi.list, retry: false, staleTime: 0 })
  // The panel's own crew list, read from its cache (this form fetches nothing for it).
  const crews = useQuery({ queryKey: ['instances'], queryFn: () => api.listInstances(), enabled: false })
  const add = useMutation({
    mutationFn: (req: { sandboxId: string; name: string }) => managedEnvApi.adopt(req.sandboxId, req.name),
    onSuccess: (_out, req) => onAdded(req.name),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: MANAGED_ENV_LIST_KEY })
      void qc.invalidateQueries({ queryKey: ['instances'] })
    },
  })
  const hosts = crews.data ? new Set(crews.data.instances.map((c) => c.ssh_host)) : undefined
  const rowless = (host?: string) => !!hosts && !!host && !hosts.has(host)
  const rows = (list.data?.sandboxes ?? []).filter(
    (s) => !s.own && (!s.adopted || rowless(s.ssh_host)) && !NOT_ADDABLE.has(s.status),
  )
  // A failed read shows even over cached rows: they may be stale.
  const listFailed = list.error ? (
    <ErrorNotice
      askAgent
      title={i18nT('managedEnv.row.list_failed')}
      message={(list.error as Error).message || i18nT('managedEnv.row.list_failed')}
      onDismiss={() => void list.refetch()}
    />
  ) : null
  if (rows.length === 0 && !add.isError) return listFailed
  const ready = nameOk && sshReady
  return (
    <section className="space-y-2 border-t border-border pt-3" aria-labelledby="managed-env-others" data-testid="managed-env-others">
      <h4 id="managed-env-others" className="text-sm font-medium">{i18nT('managedEnv.form.others_title')}</h4>
      <p className="text-xs text-[var(--text-secondary)]">{i18nT('managedEnv.form.others_note')}</p>
      {listFailed}
      <ul className="space-y-1">
        {rows.map((s) => (
          <li key={s.sandbox_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <span className="min-w-0 break-all">
              {s.display_name || s.sandbox_id}{' '}
              <span className="text-xs text-[var(--text-secondary)]">{i18nT(STATE_LABEL_KEY[rowState(s.status)])}</span>
            </span>
            <Btn
              type="button"
              busy={add.isPending && add.variables?.sandboxId === s.sandbox_id}
              disabled={!ready || disabled || add.isPending}
              aria-describedby={ready ? undefined : 'managed-env-add-why'}
              onClick={() => add.mutate({ sandboxId: s.sandbox_id, name })}
            >
              <Plus size={14} aria-hidden="true" /> {i18nT('managedEnv.form.add')}
            </Btn>
          </li>
        ))}
      </ul>
      {!ready && (
        <p id="managed-env-add-why" className="text-xs text-[var(--text-secondary)]">
          {!sshReady ? i18nT('managedEnv.form.add_needs_ssh') : i18nT('managedEnv.form.add_needs_name')}
        </p>
      )}
      {add.isError && (
        // The typed crew name outlives this form (nameDraft), so a hand-off loses nothing.
        <ErrorNotice
          askAgent
          title={i18nT('managedEnv.form.add_failed')}
          message={(add.error as Error).message}
          onDismiss={() => add.reset()}
        />
      )}
    </section>
  )
}

export const SANDBOX_PROVISIONER_KIND = 'kiro_managed_env'

/** The crew-name rule the server's engine applies. */
const CREW_NAME = /^[a-z][a-z0-9-]{0,30}[a-z0-9]$/

export function crewNameOk(name: string): boolean {
  return CREW_NAME.test(name) && !name.includes('--')
}

// The typed crew name outlives the form, so an error hand-off, Back, a lane switch or
// closing the dialog does not lose it. In memory only, like the built-in form's draft
// (`setCrewLaunchDraft`): it dies with the tab, and a launch the gateway took clears it.
let nameDraft = ''
// The panel's job when the name was last typed. Only a job that appears after that is
// this form's own launch; a job retained from before (a failed one) never clears it.
let jobAtEdit: string | undefined

/** Tests start each case from an empty draft. */
export function resetSandboxNameDraft(): void {
  nameDraft = ''
  jobAtEdit = undefined
}

export default function SandboxLaunchForm({ provisioner, launch, launching, disabled, activeJob }: RemoteProvisionerFormProps) {
  const [name, setNameState] = useState(nameDraft)
  const setName = (next: string) => {
    nameDraft = next
    jobAtEdit = activeJob?.id
    setNameState(next)
  }
  // A new job for this lane and this name means the launch was accepted: the draft is used.
  const taken =
    !!activeJob &&
    activeJob.id !== jobAtEdit &&
    activeJob.provider_id === provisioner.id &&
    activeJob.profile === name &&
    name !== ''
  useEffect(() => {
    if (taken) {
      nameDraft = ''
      jobAtEdit = undefined
      setNameState('')
    }
  }, [taken])
  const ok = crewNameOk(name)
  const ssh = useSshSettings()
  // A launch without the ssh block is refused before anything is made.
  const sshReady = ssh.state.data?.present === true
  const showHint = name !== '' && !ok
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (ok && sshReady && !launching && !disabled) launch({ profile: name, size_key: '' })
      }}
    >
      <p className="text-sm text-[var(--text-secondary)]">{i18nT('managedEnv.form.intro')}</p>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">{i18nT('managedEnv.form.name_label')}</span>
        <Input
          value={name}
          onChange={(e) => setName(e.target.value.trim().toLowerCase())}
          placeholder={i18nT('managedEnv.form.name_placeholder')}
          aria-invalid={showHint}
          aria-describedby="managed-env-name-hint"
          className="aria-invalid:border-danger aria-invalid:[--ring:var(--danger)]"
          disabled={disabled || launching}
        />
      </label>
      <p id="managed-env-name-hint" className={`text-xs ${showHint ? 'text-danger' : 'text-[var(--text-secondary)]'}`}>
        {showHint ? i18nT('managedEnv.form.name_rule') : i18nT('managedEnv.form.name_help')}
      </p>
      <SshSettings {...ssh} />
      <div className="flex flex-wrap items-center gap-3">
        {!sshReady && (
          <Btn
            type="button"
            busy={ssh.add.isPending}
            disabled={disabled || launching || ssh.add.isPending || !ssh.state.isSuccess}
            onClick={() => ssh.add.mutate()}
          >
            <Link2 size={14} aria-hidden="true" /> {i18nT('managedEnv.form.ssh_add')}
          </Btn>
        )}
        <Btn type="submit" primary busy={launching} disabled={!ok || !sshReady || disabled || launching} aria-describedby={!ok || !sshReady ? 'managed-env-launch-why' : undefined}>
          <Rocket size={14} aria-hidden="true" /> {i18nT('managedEnv.form.launch')}
        </Btn>
        {(!ok || !sshReady) && (
          <span id="managed-env-launch-why" className="text-xs text-[var(--text-secondary)]">
            {/* The ssh step first: its button is right here, beside Launch. */}
            {!sshReady
              ? i18nT('managedEnv.form.launch_needs_ssh')
              : name === ''
                ? i18nT('managedEnv.form.launch_needs_name')
                : i18nT('managedEnv.form.launch_fix_name')}
          </span>
        )}
      </div>
      <OtherEnvironments
        name={name}
        nameOk={ok}
        sshReady={sshReady}
        disabled={disabled || launching}
        // Clear only the name that was added: one typed while it was in flight stays.
        onAdded={(added) => {
          if (nameDraft === added) setName('')
        }}
      />
    </form>
  )
}
