/**
 * InstanceFormFields — the remote-crew field set, shared by the "add a crew"
 * form and the per-crew "edit" form.
 *
 * Both forms write the same record through the same validation, so the fields,
 * their hints, and the transport-conditional layout live here once. `idPrefix`
 * keeps DOM ids unique when an edit form is open on a row while the add form is
 * mounted further down the same page.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Pencil } from 'lucide-react'
import { api, ApiError, type AddInstanceBody, type InstanceView } from '../../api/client'
import SimpleSelect from '../../components/SimpleSelect'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

/** Form-shaped mirror of an instance record: every field is a string the user typed. */
/**
 * Unsaved edit state that outlives the form component: the typed values plus the
 * record they were typed against. The two travel together because a draft is only
 * meaningful relative to its baseline — rebasing it onto a newer poll is how an
 * untouched field becomes an unintended write.
 */
export interface InstanceDraft {
  values: InstanceFormValues
  baseline: InstanceView
}

export interface InstanceFormValues {
  name: string
  method: 'ssh' | 'ssm'
  sshHost: string
  ssmTarget: string
  awsProfile: string
  awsRegion: string
  ssmRunAs: string
  remotePort: string
  ttl: string
  remoteBin: string
}

// Defaults for a brand-new crew. The port and TTL mirror the backend's own
// defaults so an untouched form round-trips to the same record the API would
// have created on its own.
export const DEFAULT_REMOTE_PORT = '7777'
export const DEFAULT_TTL = '20h'
// The backend's own default for the remote account an SSM session runs as. A
// cleared field falls back to it rather than to the empty string, which the
// registry rejects for an SSM crew.
export const DEFAULT_SSM_RUN_AS = 'ec2-user'

// A tunnel forwards ONE TCP port and mints a token with a bounded lifetime, so
// neither field has a sane fallback: coercing an unparseable value would persist
// a port the user never chose, or a TTL the token minter cannot read.
const PORT_MIN = 1
const PORT_MAX = 65535
/**
 * `<positive int><h|m>` with at most four digits — the exact shape the token
 * minters accept (`_TTL_RE` in `instances/token_mint.py` and
 * `instances/ssm_token_mint.py`). A looser rule here would let a value like
 * `99999h` save and then fail at mint time, i.e. at the next connect rather
 * than at the edit that caused it.
 */
const TTL_RE = /^[1-9][0-9]{0,3}[hm]$/

export const EMPTY_INSTANCE_FORM: InstanceFormValues = {
  name: '',
  method: 'ssh',
  sshHost: '',
  ssmTarget: '',
  awsProfile: '',
  awsRegion: '',
  ssmRunAs: '',
  remotePort: DEFAULT_REMOTE_PORT,
  ttl: DEFAULT_TTL,
  remoteBin: '',
}

/** Seed the form from an existing crew, so editing starts from what is stored. */
export function instanceFormFromView(inst: InstanceView): InstanceFormValues {
  return {
    name: inst.name,
    method: inst.connection_method === 'ssm' ? 'ssm' : 'ssh',
    sshHost: inst.ssh_host || '',
    ssmTarget: inst.ssm_target || '',
    awsProfile: inst.aws_profile || '',
    awsRegion: inst.aws_region || '',
    ssmRunAs: inst.ssm_run_as || '',
    remotePort: String(inst.remote_port || DEFAULT_REMOTE_PORT),
    ttl: inst.ttl || DEFAULT_TTL,
    remoteBin: inst.remote_bin || '',
  }
}

/**
 * Form state plus everything derived from it that both forms need to gate their
 * submit button. `usedPorts` must exclude the crew being edited — its own port
 * is not a conflict with itself.
 */
export function useInstanceFormState(
  initial: InstanceFormValues,
  usedPorts: number[],
  /**
   * Values to START from, when the caller is restoring an edit the user already
   * typed. `initial` stays the record snapshot, so `dirty` keeps measuring
   * against what is PERSISTED — a restored draft is still unsaved work, not a
   * clean form.
   */
  seed?: InstanceFormValues | null,
) {
  const [values, setValues] = useState<InstanceFormValues>(seed ?? initial)
  const set = useCallback(
    <K extends keyof InstanceFormValues>(key: K, value: InstanceFormValues[K]) => {
      setValues(prev => ({ ...prev, [key]: value }))
    },
    [],
  )
  // Whether the user has typed anything yet — the panel refuses to swap an open
  // form out from under unsaved work.
  const dirty = JSON.stringify(values) !== JSON.stringify(initial)
  const isSsm = values.method === 'ssm'
  // Parsed strictly: `Number('')` is 0 and `Number('80abc')` is NaN, but
  // `parseInt` would accept "80abc" as 80 and quietly forward the wrong port.
  const portRaw = values.remotePort.trim()
  const portNum = /^[0-9]+$/.test(portRaw) ? Number(portRaw) : NaN
  const portValid = Number.isInteger(portNum) && portNum >= PORT_MIN && portNum <= PORT_MAX
  const ttlValid = TTL_RE.test(values.ttl.trim())
  const dupPort = portValid && usedPorts.includes(portNum)
  // The transport-specific required field: ssh_host for SSH, ssm_target for SSM.
  const targetFilled = isSsm ? !!values.ssmTarget.trim() : !!values.sshHost.trim()
  const valid = !!values.name.trim() && targetFilled && !dupPort && portValid && ttlValid
  /**
   * The request payload. Fields belonging to the transport that is NOT selected
   * are omitted rather than blanked: the backend validates them only for their
   * own transport, so leaving them intact means switching SSH → SSM → SSH does
   * not silently erase what the user typed earlier.
   *
   * `explicitClears` is what an EDIT needs. A create can omit an empty optional
   * and let the backend apply its default, but an update is a partial one: an
   * omitted key means "leave as-is", so emptying a field the user wants gone
   * would silently keep the old value — the crew would go on connecting through
   * an AWS profile that no longer appears anywhere in the form. Sending the
   * empty value makes the clear real, and makes it visible to the
   * transport-change check that decides whether to reopen the tunnel.
   *
   * `baseline` narrows the request to what the user actually CHANGED. A form is
   * seeded once and can be minutes old, so sending every field would make the
   * later of two saves revert the earlier one's corrections — a partial update
   * that carries the whole record is a full overwrite in disguise. A field
   * emptied by the user still differs from its baseline, so an explicit clear
   * survives the narrowing.
   */
  const body = useCallback(
    ({
      explicitClears = false,
      omitIdentity = false,
    }: {
      explicitClears?: boolean
      omitIdentity?: boolean
    } = {}): AddInstanceBody => {
      const v = values
      const ssm = v.method === 'ssm'
      const opt = (raw: string, cleared?: string) =>
        raw.trim() || (explicitClears ? cleared ?? '' : undefined)
      const full: AddInstanceBody = {
        name: v.name.trim(),
        // Omitted for a locked crew so a partial update cannot rewrite the
        // identity its cloud correlation depends on.
        ...(omitIdentity ? {} : { connection_method: v.method }),
        ...(ssm
          ? {
              // The profile and region are lifecycle COORDINATES, not
              // preferences: stop/start/delete address the machine by
              // {profile, region, instanceId}, so an edit here would point those
              // calls at a different AWS account and leave the real instance
              // running, unmanaged and billing.
              ...(omitIdentity
                ? {}
                : {
                    ssm_target: v.ssmTarget.trim(),
                    aws_profile: opt(v.awsProfile),
                    aws_region: opt(v.awsRegion),
                  }),
              // An SSM crew must always name a remote user, so a cleared field
              // returns to the default rather than to the empty string.
              ssm_run_as: opt(v.ssmRunAs, DEFAULT_SSM_RUN_AS),
            }
          : { ssh_host: v.sshHost.trim() }),
        // Both are gated by `valid`, so no fallback coercion here: a submit can
        // only carry a port and TTL the form already accepted.
        remote_port: Number(v.remotePort.trim()),
        ttl: v.ttl.trim(),
        remote_bin: opt(v.remoteBin),
      }
      return full
    },
    [values],
  )

  /**
   * The narrowed payload an EDIT sends: only what differs from the record the
   * form was seeded with. A form is seeded once and can be minutes old, so
   * sending every field would make the later of two concurrent saves revert the
   * earlier one's corrections — a partial update carrying the whole record is a
   * full overwrite in disguise. A field the user emptied still differs from its
   * baseline, so an explicit clear survives the narrowing.
   */
  const patch = useCallback(
    (baseline: InstanceView, opts: { omitIdentity?: boolean } = {}): Partial<AddInstanceBody> => {
      const full = body({
        explicitClears: true,
        omitIdentity: opts.omitIdentity,
      }) as unknown as Record<string, unknown>
      const before = baseline as unknown as Record<string, unknown>
      const changed: Record<string, unknown> = {}
      for (const [key, value] of Object.entries(full)) {
        if (value !== undefined && value !== before[key]) changed[key] = value
      }
      return changed as Partial<AddInstanceBody>
    },
    [body],
  )
  return {
    values,
    set,
    dirty,
    patch,
    reset: setValues,
    isSsm,
    portNum,
    dupPort,
    portValid,
    ttlValid,
    targetFilled,
    valid,
    body,
  }
}

export type InstanceFormState = ReturnType<typeof useInstanceFormState>

/**
 * Edit an already-configured crew in place. Editing preserves the crew's
 * identity; the only alternative is delete-and-re-add, which discards the record
 * along with the typo.
 *
 * Saving a transport change closes the tunnel and does NOT reopen it. Any
 * automatic reconnect races the user's own Disconnect — whatever moment the
 * intent is sampled, a disconnect can land after it and be undone by a
 * reconnection nobody asked for. The teardown preserves connect intent, so the
 * crew keeps its switcher entry and its row offers Connect; one explicit click
 * is cheaper than a save that fights the user.
 */
export function EditInstanceForm({
  inst,
  usedPorts,
  onSaved,
  onCancel,
  draft,
  externallyChanged,
  onDraftChange,
  onRebase,
  lockTransport = false,
}: {
  inst: InstanceView
  /** Ports taken by OTHER crews — this crew's own port is not a conflict. */
  usedPorts: number[]
  /**
   * Receives the SAVED record. Its status says whether the tunnel survived the
   * edit, which the caller needs: a warm pane holds the OLD local port and token,
   * so if the save tore the tunnel down that pane is already dead.
   */
  onSaved: (updated: InstanceView) => void
  onCancel: () => void
  /**
   * Unsaved work to restore, when this form is being remounted after the panel
   * swapped it out (a tab switch unmounts the whole crew list). Carries the
   * BASELINE as well as the values: the baseline is the record the draft was
   * typed against, and re-deriving it from the current `inst` on remount would
   * rebase the stale draft onto a newer poll — turning a field the user never
   * touched into a difference, and sending it.
   */
  draft?: InstanceDraft | null
  /**
   * Fields whose PERSISTED value no longer matches the baseline this draft was
   * typed against — the crew was edited (or removed and recreated under the same
   * id) outside this form. The two cases are indistinguishable from here, and they
   * want opposite outcomes: a concurrent edit should keep the user's typing, a
   * replacement must not receive it. So the form refuses to guess and refuses to
   * save blind — it names what moved and makes the user look.
   */
  externallyChanged?: string[]
  /**
   * Reports unsaved work up so it OUTLIVES this component. Called with the typed
   * values and their baseline while the form is dirty, and with `null` once it
   * matches that baseline — deliberately NOT called on unmount, because unmount
   * is exactly the moment the draft has to survive.
   */
  onDraftChange?: (draft: InstanceDraft | null) => void
  /**
   * Adopt the CURRENT record as this draft's baseline, keeping the typed values.
   * The subsequent save is then a partial update against the record that actually
   * exists, so it cannot carry a stale field back.
   */
  onRebase?: () => void
  /**
   * Freeze the fields that ADDRESS the machine: connection method, SSM target,
   * AWS profile and region.
   * A cloud crew is matched to its EC2 stack THROUGH that identity, so editing
   * it away would leave the dashboard unable to stop or delete a machine that
   * keeps billing — and Remove would then unregister it silently.
   */
  lockTransport?: boolean
}) {
  // Everything the form measures itself against comes from ONE snapshot: the
  // record it was opened on, restored with the draft when it remounts. Seeding
  // `initial` from the live `inst` instead would make `dirty` and the request body
  // disagree about what "unchanged" means.
  const baselineRef = useRef(draft?.baseline ?? inst)
  // The baseline is normally fixed for the form's lifetime, but a REBASE replaces it
  // deliberately: the user has been shown that the record moved and chose to apply
  // their edits to the record as it now is. Following it here is what makes both
  // `dirty` and the request body measure against that same choice.
  if (draft?.baseline !== undefined && draft.baseline !== baselineRef.current) {
    baselineRef.current = draft.baseline
  }
  const form = useInstanceFormState(
    instanceFormFromView(baselineRef.current),
    usedPorts,
    draft?.values,
  )
  // The record as it was when this form opened, held immutably. `inst` is a prop
  // fed by the 60-second instances poll, so diffing against it would compare the
  // user's stale field values with someone else's newer ones: a change made from
  // the CLI mid-edit would then read as a difference and be overwritten by a
  // field the user never touched. The form's own values are seeded from this
  // same snapshot, so the two cannot drift apart.
  // Held in a ref so the effect below depends only on the FORM's state. A caller
  // passing an inline arrow (the panel does, one per row) would otherwise change
  // the callback's identity on every render and re-fire the report each time.
  const reportRef = useRef(onDraftChange)
  reportRef.current = onDraftChange
  useEffect(() => {
    reportRef.current?.(
      form.dirty ? { values: form.values, baseline: baselineRef.current } : null,
    )
  }, [form.dirty, form.values])
  const saveMutation = useMutation({
    mutationFn: () =>
      api.updateInstance(
        inst.id,
        form.patch(baselineRef.current, { omitIdentity: lockTransport }),
      ),
    onSuccess: updated => onSaved(updated),
  })
  // Only meaningful once there is something to lose: a clean form has no typed
  // values to protect, and re-seeding it silently is correct.
  const stale = !!externallyChanged?.length && form.dirty
  const err = saveMutation.error
    ? saveMutation.error instanceof ApiError
      ? saveMutation.error.message
      : i18nT('pages.settings.remoteCrewPanel.failed_to_save_crew')
    : ''
  return (
    <div
      className="mt-3 rounded-md border border-border bg-bg-elevated p-3"
      role="group"
      aria-label={i18nT('pages.settings.remoteCrewPanel.edit_crew', { name: inst.name })}
    >
      <div className="flex items-center gap-2 mb-3 text-text font-medium text-sm">
        <Pencil className="lucide-inline" />{' '}
        {i18nT('pages.settings.remoteCrewPanel.edit_crew', { name: inst.name })}
      </div>
      <InstanceFormFields
        idPrefix={`edit-instance-${inst.id}`}
        form={form}
        lockTransport={lockTransport}
      />
      {lockTransport && (
        <p className="mt-2 text-[12px] text-warn">
          {i18nT('pages.settings.remoteCrewPanel.transport_locked_note')}
        </p>
      )}
      {/* Only meaningful while a tunnel is actually up: for a disconnected crew
          this is prose about a consequence that cannot occur. Warn-weighted, not
          muted: it is the one consequence of this form the user cannot undo by
          editing again, and muted type is what made it discoverable only after
          the save had already closed the tunnel. */}
      {inst.status?.state === 'connected' ? (
        <p className="mt-2 text-[12px] text-warn">
          {i18nT('pages.settings.remoteCrewPanel.edit_reconnect_note')}
        </p>
      ) : null}
      {stale ? (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {i18nT('pages.settings.remoteCrewPanel.changed_outside_this_form', {
            fields: externallyChanged.join(', '),
          })}
        </p>
      ) : null}
      <ErrorNotice message={err} className="mt-3" />
      <div className="mt-3 flex items-center gap-2">
        {/* Save is withheld while the record is stale, rather than the edit being
            discarded: throwing the typing away would punish the far more common
            case (someone edited this same crew from the CLI) to guard the rarer
            one (the crew was replaced under its id). */}
        {stale ? (
          <Btn primary onClick={onRebase} disabled={saveMutation.isPending}>
            {i18nT('pages.settings.remoteCrewPanel.use_my_edits_anyway')}
          </Btn>
        ) : (
        <Btn
          primary
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending || !form.valid}
        >
          {saveMutation.isPending
            ? i18nT('pages.settings.remoteCrewPanel.saving')
            : i18nT('pages.settings.remoteCrewPanel.save_changes')}
        </Btn>
        )}
        <Btn onClick={onCancel} disabled={saveMutation.isPending}>
          {i18nT('pages.settings.remoteCrewPanel.cancel')}
        </Btn>
      </div>
    </div>
  )
}

const inputCls =
  'bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none focus-ring'
// A frozen field must LOOK frozen: identical styling invites the user to click in,
// type, and discover only from the note below the grid that nothing landed.
const readOnlyCls = `${inputCls} opacity-60 cursor-not-allowed`

export function InstanceFormFields({
  idPrefix,
  form,
  lockTransport = false,
}: {
  idPrefix: string
  form: InstanceFormState
  /** Render the machine-identity fields read-only (see EditInstanceForm). */
  lockTransport?: boolean
}) {
  const { values, set, isSsm, portNum, dupPort, portValid, ttlValid } = form
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <label htmlFor={`${idPrefix}-name`} className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.name')}
        <input id={`${idPrefix}-name`} aria-label={i18nT('pages.settings.instancesPanel.name')} className={inputCls} value={values.name} onChange={e => set('name', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.remote_host_1')} />
      </label>
      {/* Not a <label>: SimpleSelect renders a button, so `htmlFor` would point
          at no form control. The caption text stays put and the accessible name
          moves to the trigger's aria-label (same key). */}
      <div className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.connection_method')}
        <SimpleSelect
          options={['ssh', 'ssm']}
          optionLabels={[i18nT('pages.settings.instancesPanel.ssh_tunnel'), i18nT('pages.settings.instancesPanel.aws_ssm_session_manager')]}
          value={values.method}
          onChange={v => set('method', v as 'ssh' | 'ssm')}
          aria-label={i18nT('pages.settings.instancesPanel.connection_method')}
          disabled={lockTransport}
        />
        <span className="text-[12px] text-muted leading-snug">
          {isSsm
            ? i18nT('pages.settings.instancesPanel.tunnels_via_aws_ssm_start_session_no_inbound_ssh')
            : i18nT('pages.settings.instancesPanel.opens_ssh_n_l_to_the_host_requires_non_interacti')}
        </span>
      </div>
      {isSsm ? (
        <>
          <label htmlFor={`${idPrefix}-ssm-target`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.ssm_target_instance_id')}
            <input id={`${idPrefix}-ssm-target`} aria-label={i18nT('pages.settings.instancesPanel.ssm_target_instance_id')} className={lockTransport ? readOnlyCls : inputCls} aria-readonly={lockTransport || undefined} value={values.ssmTarget} onChange={e => set('ssmTarget', e.target.value)} placeholder="i-0123456789abcdef0" readOnly={lockTransport} />
            <span className="text-[12px] text-muted leading-snug">
              {i18nT('pages.settings.instancesPanel.ec2_instance_id_i_or_ssm_managed_instance_id_mi')}
            </span>
          </label>
          <label htmlFor={`${idPrefix}-aws-profile`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.aws_profile')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
            <input id={`${idPrefix}-aws-profile`} aria-label={i18nT('pages.settings.instancesPanel.aws_profile')} className={lockTransport ? readOnlyCls : inputCls} aria-readonly={lockTransport || undefined} value={values.awsProfile} onChange={e => set('awsProfile', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.default_credential_chain')} readOnly={lockTransport} />
          </label>
          <label htmlFor={`${idPrefix}-aws-region`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.aws_region')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
            <input id={`${idPrefix}-aws-region`} aria-label={i18nT('pages.settings.instancesPanel.aws_region')} className={lockTransport ? readOnlyCls : inputCls} aria-readonly={lockTransport || undefined} value={values.awsRegion} onChange={e => set('awsRegion', e.target.value)} placeholder="us-east-1" readOnly={lockTransport} />
          </label>
          <label htmlFor={`${idPrefix}-ssm-run-as`} className="flex flex-col gap-1 text-[13px] text-muted">
            {i18nT('pages.settings.instancesPanel.remote_user')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
            <input id={`${idPrefix}-ssm-run-as`} aria-label={i18nT('pages.settings.instancesPanel.remote_user')} className={inputCls} value={values.ssmRunAs} onChange={e => set('ssmRunAs', e.target.value)} placeholder="ec2-user" />
            <span className="text-[12px] text-muted leading-snug">
              {i18nT('pages.settings.instancesPanel.the_user_the_remote_gateway_runs_as_sudo_u_for_s')}
            </span>
          </label>
        </>
      ) : (
        <label htmlFor={`${idPrefix}-ssh-host`} className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.ssh_host_alias')}
          <input id={`${idPrefix}-ssh-host`} aria-label={i18nT('pages.settings.instancesPanel.ssh_host_alias')} className={inputCls} value={values.sshHost} onChange={e => set('sshHost', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.host_1_alias')} />
        </label>
      )}
      <label htmlFor={`${idPrefix}-remote-port`} className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.remote_port')}
        <input id={`${idPrefix}-remote-port`} aria-label={i18nT('pages.settings.instancesPanel.remote_port')} className={inputCls} value={values.remotePort} onChange={e => set('remotePort', e.target.value)} placeholder="7777" inputMode="numeric" />
        <span className="text-[12px] text-muted leading-snug">
          {i18nT('pages.settings.instancesPanel.must_match_the_port_the_remote_gateway_serves_on')}
        </span>
        {!portValid ? (
          <span className="text-[12px] text-danger leading-snug">
            {i18nT('pages.settings.remoteCrewPanel.port_must_be_in_range')}
          </span>
        ) : dupPort ? (
          <span className="text-[12px] text-danger leading-snug">
            {i18nT('pages.settings.instancesPanel.port')} {fmtNumber(portNum)} {i18nT('pages.settings.instancesPanel.is_already_used_by_another_instance_choose_a_dif')}
          </span>
        ) : null}
      </label>
      <label htmlFor={`${idPrefix}-ttl`} className="flex flex-col gap-1 text-[13px] text-muted">
        {i18nT('pages.settings.instancesPanel.token_ttl')}
        <input id={`${idPrefix}-ttl`} aria-label={i18nT('pages.settings.instancesPanel.token_ttl')} className={inputCls} value={values.ttl} onChange={e => set('ttl', e.target.value)} placeholder={i18nT('pages.settings.instancesPanel.20h')} />
        {!ttlValid ? (
          <span className="text-[12px] text-danger leading-snug">
            {i18nT('pages.settings.remoteCrewPanel.ttl_must_be_hours_or_minutes')}
          </span>
        ) : null}
      </label>
      <label htmlFor={`${idPrefix}-remote-bin`} className="flex flex-col gap-1 text-[13px] text-muted sm:col-span-2">
        {i18nT('pages.settings.instancesPanel.remote_kirocrew_path')} <span className="text-muted-strong">{i18nT('pages.settings.instancesPanel.optional')}</span>
        <input
          id={`${idPrefix}-remote-bin`}
          aria-label={i18nT('pages.settings.instancesPanel.remote_kirocrew_path')}
          className={inputCls}
          value={values.remoteBin}
          onChange={e => set('remoteBin', e.target.value)}
          placeholder={i18nT('pages.settings.instancesPanel.home_you_local_bin_kirocrew_leave_blank_for_stan')}
        />
        <span className="text-[12px] text-muted leading-snug">
          {i18nT('pages.settings.instancesPanel.only_needed_if')} <code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew')}</code> {i18nT('pages.settings.instancesPanel.is_installed_somewhere_non_standard_on_the_remot')} <code className="text-text">{i18nT('pages.settings.instancesPanel.command_v_kirocrew')}</code>{' '}
          {i18nT('pages.settings.instancesPanel.commonly')} <code className="text-text">{i18nT('pages.settings.instancesPanel.local_bin_kirocrew')}</code>{i18nT('pages.settings.instancesPanel.use_an_absolute_path_no')} <code className="text-text">~</code>).
        </span>
      </label>
    </div>
  )
}
