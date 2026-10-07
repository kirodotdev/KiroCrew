import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { INHERIT_MODEL } from '../../components/crew/useCrewEditor'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { crewDisplayName } from '../../components/AgentSelector'
import { Field, ModelField } from '../KiroCrewAgentsPage'

/** The approval modes a crewmate's record may pin, in label order; YOLO is process-global. */
const MODES = ['normal', 'trust_reads', 'trust'] as const
type Setting = 'approval_mode' | 'model' | 'reasoning_effort'

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
  const effortCapable = modelSupportsEffort(model === INHERIT_MODEL ? resolved?.model : model)
  const modelOptions = [INHERIT_MODEL, ...(models.data || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL)]

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
      <Field label={t('pages.membersPage.profile_permissions')} hint={modeHint}>
        <SimpleSelect
          options={[...MODES]}
          optionLabels={[t('components.approvalModePicker.normal_label'), t('components.approvalModePicker.reads_label'), t('components.approvalModePicker.trust_label')]}
          value={mode}
          onChange={(v) => void save('approval_mode', v)}
          aria-label={t('pages.membersPage.profile_permissions')}
        />
      </Field>
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
      <ErrorNotice message={notice || null} testId="crew-profile-settings-notice" />
      </fieldset>
    </section>
  )
}
