import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Lock, SendHorizontal } from 'lucide-react'
import { api, type MemberRosterRow } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'
import SimpleSelect from '../../components/SimpleSelect'
import { Toggle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { INHERIT_MODEL } from '../../components/crew/useCrewEditor'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { crewDisplayName } from '../../components/AgentSelector'
import { Field, ModelField } from '../KiroCrewAgentsPage'
import { uiLocation } from '../../uiLocations/uiLocation'
import { GuideTrustRootRegion } from '../../guide/trustRoot'

/** The approval modes a crewmate's record may pin, in label order; YOLO is process-global. */
const MODES = ['normal', 'trust_reads', 'trust'] as const
type Setting = 'approval_mode' | 'model' | 'reasoning_effort' | 'worker_model' | 'worker_reasoning_effort'
/** The record's "workers run the crewmate's own" spelling for both worker fields. */
const SAME_AS_CREWMATE = ''
/** A REAL worker-model pin, unlike the crewmate's own `model` where it means
 *  inherit: dispatched sessions run on the backend's default. */
const WORKER_AUTO = 'auto'

/** The stored permission a refused compare-and-set PUT reports, or undefined
 *  when the error is anything else. */
function conflictValue(e: unknown): string | undefined {
  if (!(e instanceof ApiError) || e.status !== 409) return undefined
  try {
    const body = JSON.parse(e.body || '{}')
    return body?.code === 'approval_mode_conflict' && typeof body.approval_mode === 'string' ? body.approval_mode : undefined
  } catch { return undefined }
}

export default function CrewProfileSettings({ member, slotKey, waiting = false }: {
  member: MemberRosterRow
  slotKey?: string | null
  /** The crewmate has a thread the page has not confirmed yet (opening, or
   *  refused). A pick now would save the record but could not reach that
   *  thread, so the pickers wait. */
  waiting?: boolean
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Partial<Record<Setting, string>>>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // One save at a time. Each pick writes the record, then the live slot; two
  // picks in flight could land out of order and leave the slot on an older mode.
  const [saving, setSaving] = useState(false)
  const inFlight = useRef(false)
  const models = useAvailableModelsQuery({ enabled: true })
  const { data: resolved, error: resolvedError } = useQuery({
    queryKey: ['agent-resolved-model', member.name],
    queryFn: () => api.agentResolvedModel(member.name),
  })
  const stored = {
    approval_mode: String(member.approval_mode || 'trust'),
    model: member.model || INHERIT_MODEL,
    reasoning_effort: String(member.reasoning_effort || ''),
    worker_model: String(member.worker_model || SAME_AS_CREWMATE),
    worker_reasoning_effort: String(member.worker_reasoning_effort || SAME_AS_CREWMATE),
  }
  const value = (k: Setting) => draft[k] ?? stored[k]
  const mode = value('approval_mode')
  // What each mode lets the crewmate do, under the picker: the bare word
  // "Trust" alone does not say it.
  // Scoped to the crewmate, not one chat: the profile pick covers every thread.
  const name = crewDisplayName(member)
  const modeHint = mode === 'normal'
    ? t('pages.membersPage.profile_mode_normal_hint', { name })
    : mode === 'trust_reads'
      ? t('pages.membersPage.profile_mode_reads_hint', { name })
      : t('pages.membersPage.profile_mode_trust_hint', { name })
  const model = value('model')
  // The model the crewmate itself runs on, named: its pin, else what Inherited resolves to.
  const ownModel = model === INHERIT_MODEL ? resolved?.model : model
  const effortCapable = modelSupportsEffort(ownModel)
  const listedModels = (models.data || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL)
  const modelOptions = [INHERIT_MODEL, ...listedModels]
  // What the crewmate's dispatched workers run on. "Same as <name>" is the
  // record's '' and follows the crewmate's own pick; Auto is a real pin here.
  const workerModel = value('worker_model')
  const workerModelOptions = [SAME_AS_CREWMATE, WORKER_AUTO, ...listedModels.filter((n) => n !== WORKER_AUTO)]
  const workerRunsOn = workerModel === SAME_AS_CREWMATE ? ownModel : workerModel
  const workerEffort = value('worker_reasoning_effort')
  // Offered when the workers' model reasons; a stored level stays visible so it
  // can be cleared. Otherwise the row folds and the model hint says so.
  const workerEffortRowShown = modelSupportsEffort(workerRunsOn) || !!workerEffort
  // Distinct from the model dropdown's "Same as <name>": one names a model, the
  // other a level, and a reader must be able to tell which is which.
  const workerEffortInheritLabel = workerModel === SAME_AS_CREWMATE
    ? t('pages.membersPage.profile_worker_effort_same_as', { name })
    : t('pages.membersPage.profile_worker_effort_model_default')
  const ownEffort = value('reasoning_effort') || resolved?.reasoning_effort || ''
  // The crewmate's own model, named, for the hints; before the resolve answers,
  // or when it cannot, the phrase "<name>'s own model" stands in.
  const ownModelNoun = ownModel || t('pages.membersPage.profile_worker_own_model_fallback', { name })

  // The person's lock on the crewmate's dynamic dashboard: set HERE, on the
  // crewmate's own card, never on the Dashboard tab (which only shows a badge).
  // Read off the dashboard body under the tab's own query prefix, so the gateway's
  // "dashboard moved" frame and this card's own flip refresh both surfaces at once.
  const lockRead = useQuery({
    queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, member.slug, member.name, 'lock'],
    queryFn: () => api.memberDashboard(member.slug, member.name),
    enabled: Boolean(member.slug),
    select: (page) => Boolean(page?.structure_locked),
  })
  const lockFlip = useMutation({
    mutationFn: (want: boolean) => api.memberDashboardLock(member.slug, member.name, want),
    onSettled: () => {
      // Return the promise so TanStack Query awaits the refetch before clearing
      // isPending — without this, isPending goes false before lockRead is updated
      // and displayLocked briefly shows the old position.
      return queryClient.invalidateQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, member.slug] })
    },
  })
  const locked = lockRead.data === true
  // While a flip is in flight, show the state being requested rather than the
  // last-read state: the switch should not snap back to the old position
  // between the mutation's onSettled firing and the refetch landing.
  const displayLocked = lockFlip.isPending ? (lockFlip.variables ?? locked) : locked
  // The error names the state that HELD, so the person knows what to expect.
  const lockError = lockRead.isError
    ? t('pages.membersPage.profile_dashboard_lock_unreadable')
    : lockFlip.isError
      ? t(lockFlip.variables ? 'pages.membersPage.profile_dashboard_lock_failed' : 'pages.membersPage.profile_dashboard_unlock_failed')
      : null

  // Whether a button on the crewmate's Dashboard tab may send its reply in one click.
  // Stored on the crewmate's record and enforced by the gateway; off, a button only
  // fills the chat box. Absent (an older gateway) reads as on, the default.
  const cardSendFlip = useMutation({
    mutationFn: (want: boolean) => api.updateKirocrewAgent(member.name, { dashboard_card_send: want }),
    onSuccess: (_data, want) => {
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
        rows?.map((r) => (r.name === member.name ? { ...r, dashboard_card_send: want } : r)))
    },
  })
  const cardSendOn = member.dashboard_card_send !== false
  // Show the state being requested while the write is in flight, so the switch does
  // not snap back before the roster row is updated.
  const displayCardSend = cardSendFlip.isPending ? (cardSendFlip.variables ?? cardSendOn) : cardSendOn

  const save = async (key: Setting, next: string) => {
    if (inFlight.current || waiting) return
    inFlight.current = true
    setSaving(true)
    setDraft((d) => ({ ...d, [key]: next }))
    setError('')
    setNotice('')
    // The record spells "inherit" as ''; the picker spells it INHERIT_MODEL.
    const wire = key === 'model' && next === INHERIT_MODEL ? '' : next
    try {
      // The permission is compare-and-set: the server applies it only if the
      // record still holds what this card read, so a stale card can never
      // undo a newer pick. Model and effort are plain last-write-wins.
      const body = key === 'approval_mode'
        ? { approval_mode: wire, expected_approval_mode: String(member.approval_mode ?? '') }
        : { [key]: wire }
      try {
        await api.updateKirocrewAgent(member.name, body)
      } catch (e) {
        const current = conflictValue(e)
        if (current === undefined) throw e
        // Someone else's pick won. Show what is stored now; touch no thread.
        queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
          rows?.map((r) => (r.name === member.name ? { ...r, approval_mode: current } : r)))
        setDraft((d) => ({ ...d, [key]: current || 'trust' }))
        setNotice(t('pages.membersPage.profile_permission_changed'))
        return
      }
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
        rows?.map((r) => (r.name === member.name ? { ...r, [key]: wire } : r)))
      // The server applied the permission to the live thread in the same step
      // (PUT /api/agents/{name}). Model and effort still follow from here; if
      // that write fails the error says so, and the next thread uses the record.
      // The worker pair touches no thread: it is read when the crewmate next
      // opens a session, so the record is the whole write.
      if (slotKey && key === 'model') await api.chatSlotModel(slotKey, wire)
      else if (slotKey && key === 'reasoning_effort') await api.chatSlotReasoningEffort(slotKey, wire)
    } catch (e) {
      setDraft((d) => ({ ...d, [key]: undefined }))
      setError(e instanceof Error ? e.message : String((e as { message?: string })?.message ?? e))
    } finally {
      inFlight.current = false
      setSaving(false)
      void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: ['agent-resolved-model', member.name] })
      void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    }
  }

  return (
    <section className="rounded-2xl border border-border bg-bg px-3.5 py-3" data-testid="crew-profile-settings">
      {waiting && <p className="mb-2 text-[12px] text-muted" role="status" data-testid="crew-profile-settings-waiting">{t('pages.membersPage.profile_settings_waiting')}</p>}
      <fieldset disabled={saving || waiting} aria-busy={saving || waiting} className="flex flex-col gap-3 min-w-0 border-0 p-0 m-0">
      {/* The crewmate's approval mode is its own ceiling: no guide points at it,
          by location id or by name, and its portalled options carry the mark too. */}
      <GuideTrustRootRegion>
        <Field label={t('pages.membersPage.profile_permissions')} hint={modeHint}>
          <SimpleSelect
            options={[...MODES]}
            optionLabels={[t('components.approvalModePicker.normal_label'), t('components.approvalModePicker.reads_label'), t('components.approvalModePicker.trust_label')]}
            value={mode}
            onChange={(v) => void save('approval_mode', v)}
            aria-label={t('pages.membersPage.profile_permissions')}
            {...uiLocation('members.permissions')}
          />
        </Field>
      </GuideTrustRootRegion>
      {/* "Inherited" alone does not say what runs: name the model it resolves to. */}
      <ModelField
        options={modelOptions}
        value={model}
        onChange={(v) => void save('model', v)}
        hint={model === INHERIT_MODEL && resolved?.model ? t('pages.membersPage.profile_model_inherited_hint', { model: resolved.model }) : undefined}
      />
      {(effortCapable || !!value('reasoning_effort')) && (
        // Not the crew editor's EffortField: its hint points at a per-chat
        // picker this page's composer no longer has.
        <Field
          label={t('pages.kiroCrewAgentsPage.reasoning_effort')}
          // Always say what a level trades, plus what Inherited resolves to.
          hint={[
            t('pages.membersPage.profile_effort_hint'),
            value('reasoning_effort') === '' && resolved?.reasoning_effort ? t('pages.kiroCrewAgentsPage.effort_resolves_to', { effort: effortLabel(resolved.reasoning_effort) }) : '',
          ].filter(Boolean).join(' ')}
        >
          <SimpleSelect
            options={[...EFFORT_LEVELS]}
            optionLabels={EFFORT_LEVELS.map((l) => (l === '' ? t('pages.kiroCrewAgentsPage.inherited') : effortLabel(l)))}
            value={value('reasoning_effort')}
            onChange={(v) => void save('reasoning_effort', v)}
            aria-label={t('pages.kiroCrewAgentsPage.edit_reasoning_effort')}
          />
        </Field>
      )}
      {/* The helper chats this crewmate starts to split up a task (session_create):
          by default they run what the crewmate runs; the Auto pin makes them cheap.
          When the effort row below folds away (the chosen model does not reason),
          the hint says so, so the row never just vanishes. */}
      <Field
        label={t('pages.membersPage.profile_worker_model')}
        hint={[
          workerModel === SAME_AS_CREWMATE
            ? t('pages.membersPage.profile_worker_same_hint', { name, model: ownModelNoun })
            : workerModel === WORKER_AUTO
              ? t('pages.membersPage.profile_worker_auto_hint', { name, model: ownModelNoun })
              : t('pages.membersPage.profile_worker_model_hint', { name, model: workerModel, own: ownModelNoun }),
          // Says what is true when the effort row folds: the setting does not apply
          // to that model. Never "no reasoning", which reads as a downgrade.
          workerEffortRowShown ? '' : t('pages.membersPage.profile_worker_no_effort_hint'),
        ].filter(Boolean).join(' ')}
      >
        <SimpleSelect
          options={workerModelOptions}
          optionLabels={workerModelOptions.map((m) => (
            m === SAME_AS_CREWMATE ? t('pages.membersPage.profile_worker_same_as', { name }) : m === WORKER_AUTO ? t('pages.membersPage.profile_worker_auto') : m
          ))}
          value={workerModel}
          onChange={(v) => void save('worker_model', v)}
          aria-label={t('pages.membersPage.profile_worker_model')}
        />
      </Field>
      {workerEffortRowShown && (
        // '' means "the crewmate's own level" only while the workers run the
        // crewmate's own model; under another model it is the model's default,
        // and the label says which (worker_session_defaults draws the same line).
        <Field
          label={t('pages.membersPage.profile_worker_effort')}
          // The empty option always gets a note: under the crewmate's model it
          // names the level that carries over; under another model it says no
          // level is pinned and the crewmate's is not copied (what the worker's
          // agent then defaults to is resolved at its first turn, not here).
          hint={workerEffort !== SAME_AS_CREWMATE
            ? undefined
            : workerModel === SAME_AS_CREWMATE
              ? (ownEffort ? t('pages.membersPage.profile_worker_effort_same_hint', { name, effort: effortLabel(ownEffort) }) : undefined)
              : t('pages.membersPage.profile_worker_effort_default_hint', { name, effort: ownEffort ? ` (${effortLabel(ownEffort)})` : '' })}
        >
          <SimpleSelect
            options={[...EFFORT_LEVELS]}
            optionLabels={EFFORT_LEVELS.map((l) => (l === '' ? workerEffortInheritLabel : effortLabel(l)))}
            value={workerEffort}
            onChange={(v) => void save('worker_reasoning_effort', v)}
            aria-label={t('pages.membersPage.profile_worker_effort')}
          />
        </Field>
      )}
      <Field label={t('pages.membersPage.profile_dashboard')}>
        <div className="flex items-center justify-between gap-3 min-w-0" data-testid="crew-profile-dashboard-lock">
          <span className="flex items-center gap-2 min-w-0 text-[13px] text-text">
            <Lock size={14} className="text-muted shrink-0" aria-hidden="true" />
            <span className="truncate">{t('pages.membersPage.profile_dashboard_lock_switch')}</span>
          </span>
          {/* Hide the switch when the lock state cannot be read: showing a dim off switch
              for an unknown state looks identical to "unlocked" and misleads the person.
              The error notice below names the problem and the next step. */}
          {!lockRead.isError && (
          <Toggle
            checked={displayLocked}
            onChange={(v) => lockFlip.mutate(v)}
            disabled={lockRead.data === undefined || lockFlip.isPending}
            label={t('pages.membersPage.profile_dashboard_lock_switch')}
          />
          )}
        </div>
        <span className="text-[11.5px] leading-relaxed text-muted">{t('pages.membersPage.profile_dashboard_lock_hint', { name })}</span>
        <div className="flex items-center justify-between gap-3 min-w-0 mt-1.5" data-testid="crew-profile-dashboard-card-send">
          <span className="flex items-center gap-2 min-w-0 text-[13px] text-text">
            <SendHorizontal size={14} className="text-muted shrink-0" aria-hidden="true" />
            <span className="truncate">{t('pages.membersPage.profile_dashboard_card_send_switch')}</span>
          </span>
          <Toggle
            checked={displayCardSend}
            onChange={(v) => cardSendFlip.mutate(v)}
            disabled={cardSendFlip.isPending}
            label={t('pages.membersPage.profile_dashboard_card_send_switch')}
          />
        </div>
        <span className="text-[11.5px] leading-relaxed text-muted">{t('pages.membersPage.profile_dashboard_card_send_hint', { name })}</span>
      </Field>
      {/* No hand-off on any notice here: the Crewmates page's composer may hold an
          unsent draft to this crewmate, and "Ask the agent" navigates away. */}
      <ErrorNotice
        message={resolvedError ? (resolvedError instanceof Error ? resolvedError.message : String(resolvedError)) : null}
        testId="crew-profile-settings-resolve-error"
      />
      <ErrorNotice
        message={models.error || models.isDegraded ? t('pages.chatSidebar.model_list_failed') : null}
        testId="crew-profile-settings-models-error"
      />
      <ErrorNotice message={error || null} testId="crew-profile-settings-error" />
      <ErrorNotice message={lockError} testId="crew-profile-dashboard-lock-error" />
      <ErrorNotice
        message={cardSendFlip.isError ? t('pages.membersPage.profile_dashboard_card_send_failed') : null}
        testId="crew-profile-dashboard-card-send-error"
      />
      <ErrorNotice message={notice || null} testId="crew-profile-settings-notice" />
      </fieldset>
    </section>
  )
}
