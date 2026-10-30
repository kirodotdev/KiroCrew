import { useContext, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, Cpu, Gauge, Loader2, Lock, Puzzle, Shield, SlidersHorizontal, Sparkles, Users } from 'lucide-react'
import { api, type MemberRosterRow } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'
import { Btn, Toggle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { INHERIT_MODEL } from '../../components/crew/useCrewEditor'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREWMATE_HARNESS } from '../../utils/previewFlags'
import { acpBackendName } from '../../api/acpBackend'
import { EFFORT_LEVELS, effortLabel, modelSupportsEffort } from '../../lib/effort'
import { crewDisplayName } from '../../components/AgentSelector'
import { uiLocation } from '../../uiLocations/uiLocation'
import { GuideTrustRootRegion } from '../../guide/trustRoot'
import { useGuideRevealScope } from '../../guide/GuideRevealScope'
import { cn } from '../../lib/utils'
import { ProfilePagePushContext } from './CrewProfilePanel'
import { Row, RowGroup, RowStatic, type TONE } from './profileRow'

/** The approval modes a crewmate's record may pin, in label order; YOLO is process-global. */
const MODES = ['normal', 'trust_reads', 'trust'] as const
type Setting = 'approval_mode' | 'acp_backend' | 'model' | 'reasoning_effort' | 'worker_model' | 'worker_reasoning_effort'
/** The record's "workers run the crewmate's own" spelling for both worker fields. */
const SAME_AS_CREWMATE = ''
/** A REAL worker-model pin, unlike the crewmate's own `model` where it means
 *  inherit: dispatched sessions run on the backend's default. */
const WORKER_AUTO = 'auto'

/** The record's "inherit the install's harness" spelling for `acp_backend`. */
const INHERIT_HARNESS = ''

/** One choice on a picker page. */
interface Option { value: string; label: string; desc?: string }
/** One setting as the group lists it and the pushed page offers it. */
interface Picker {
  key: Setting
  label: string
  icon: ReactNode
  tone: keyof typeof TONE
  /** The current value, as the row reads it and the page checks it. */
  value: string
  valueLabel: string
  /** A short line under the row's label, for a label that alone does not say
   *  what it controls. */
  sub?: string
  options: Option[]
  /** The line under the page's list: what the current pick resolves to or trades. */
  caption?: string
}

/** The stored permission a refused compare-and-set PUT reports, or undefined
 *  when the error is anything else. */
function conflictValue(e: unknown): string | undefined {
  if (!(e instanceof ApiError) || e.status !== 409) return undefined
  try {
    const body = JSON.parse(e.body || '{}')
    return body?.code === 'approval_mode_conflict' && typeof body.approval_mode === 'string' ? body.approval_mode : undefined
  } catch { return undefined }
}

/** A stored pin the list does not carry still has to be offered, or the page
 *  would show nothing checked and the row a value no option names. */
function withCurrent(options: string[], value: string): string[] {
  return options.includes(value) ? options : [...options, value]
}

/**
 * The crewmate's own settings on its Profile card: permission, model, effort,
 * the worker pair and the dashboard lock. One iOS-style group — each picker is
 * a row reading its current value, and tapping it pushes a single-choice page
 * over the card (the card's own push, through `ProfilePagePushContext`); the
 * lock, a boolean, stays a switch in the group with its one line of help as the
 * group's caption. The page stays open after a pick, check mark moved and any
 * notice shown, until the person goes back: a save can be refused (a stale
 * permission) and a page that popped would have carried that away.
 */
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
  const push = useContext(ProfilePagePushContext)
  const [draft, setDraft] = useState<Partial<Record<Setting, string>>>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // A save that worked and has something to say (a harness pick takes effect
  // later): a plain status line, never an alert.
  const [saved, setSaved] = useState('')
  // One save at a time. Each pick writes the record, then the live slot; two
  // picks in flight could land out of order and leave the slot on an older mode.
  const [saving, setSaving] = useState(false)
  const inFlight = useRef(false)
  // The picker page that is open, and the one last shown: the latter outlives
  // the former for the card's exit slide, so the page does not blank mid-slide.
  const [page, setPage] = useState<Setting | null>(null)
  const [shown, setShown] = useState<Setting | null>(null)
  // The Settings page (the group) is open over the card.
  const [open, setOpen] = useState(false)
  const models = useAvailableModelsQuery({ enabled: true })
  const { data: resolved, error: resolvedError } = useQuery({
    queryKey: ['agent-resolved-model', member.name],
    queryFn: () => api.agentResolvedModel(member.name),
  })
  // A developer preview (Settings > Developer > Feature Previews): the Harness row.
  // The pin itself is read from the owner-only readout above whatever the flag
  // says, so a saved pin always scopes the model lists below.
  const harnessRowOn = usePreviewFlag(PREVIEW_CREWMATE_HARNESS)
  const stored = {
    approval_mode: String(member.approval_mode || 'trust'),
    acp_backend: String(resolved?.acp_backend || INHERIT_HARNESS),
    model: member.model || INHERIT_MODEL,
    reasoning_effort: String(member.reasoning_effort || ''),
    worker_model: String(member.worker_model || SAME_AS_CREWMATE),
    worker_reasoning_effort: String(member.worker_reasoning_effort || SAME_AS_CREWMATE),
  }
  const value = (k: Setting) => draft[k] ?? stored[k]
  const mode = value('approval_mode')
  // Scoped to the crewmate, not one chat: the profile pick covers every thread.
  const name = crewDisplayName(member)
  const model = value('model')
  // The model the crewmate itself runs on, named: its pin, else what Inherited resolves to.
  const ownModel = model === INHERIT_MODEL ? resolved?.model : model
  const effortCapable = modelSupportsEffort(ownModel)
  // A crewmate pinned to a harness picks from THAT harness's models, never the
  // install's: a model only another harness serves is refused at save.
  const harness = value('acp_backend')
  const harnessModels = useQuery({
    queryKey: ['available-models', 'harness', harness],
    queryFn: () => api.harnessModels(harness),
    enabled: !!harness,
  })
  const listedModels = (harness
    ? (harnessModels.data || []).map((m) => m.model_name)
    : (models.data || []).map((m) => m.name)
  ).filter((n) => n && n !== INHERIT_MODEL)
  const modelOptions = withCurrent([INHERIT_MODEL, ...listedModels], model)
  // What the crewmate's dispatched workers run on. "Same as <name>" is the
  // record's '' and follows the crewmate's own pick; Auto is a real pin here.
  const workerModel = value('worker_model')
  const workerModelOptions = withCurrent([SAME_AS_CREWMATE, WORKER_AUTO, ...listedModels.filter((n) => n !== WORKER_AUTO)], workerModel)
  const workerRunsOn = workerModel === SAME_AS_CREWMATE ? ownModel : workerModel
  const workerEffort = value('worker_reasoning_effort')
  // Offered when the workers' model reasons; a stored level stays visible so it
  // can be cleared. Otherwise the row folds and the model caption says so.
  const workerEffortRowShown = modelSupportsEffort(workerRunsOn) || !!workerEffort
  // Distinct from the model picker's "Same as <name>": one names a model, the
  // other a level, and a reader must be able to tell which is which.
  const workerEffortInheritLabel = workerModel === SAME_AS_CREWMATE
    ? t('pages.membersPage.profile_worker_effort_same_as', { name })
    : t('pages.membersPage.profile_worker_effort_model_default')
  const ownEffort = value('reasoning_effort') || resolved?.reasoning_effort || ''
  // The crewmate's own model, named, for the captions; before the resolve answers,
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

  // Dynamic capabilities: a per-crewmate preview, off by default, stored on the
  // crewmate's own record (`kiro_crew/crewmate_dynamic_capabilities.py`). On, the
  // crewmate may find skills or tools for its worker and ask for them on a card.
  const dynFlip = useMutation({
    mutationFn: (want: boolean) => api.updateKirocrewAgent(member.name, { dynamic_capabilities: want }),
    // The saved value lands in the roster cache, so `member` carries it; a later
    // change from elsewhere (an applied card) reaches the switch the same way.
    onSuccess: (_r, want) => {
      queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
        rows?.map((r) => (r.name === member.name ? { ...r, dynamic_capabilities: want } : r)))
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }),
  })
  const dynOn = dynFlip.isPending
    ? (dynFlip.variables ?? false)
    : member.dynamic_capabilities === true

  const save = async (key: Setting, next: string) => {
    if (inFlight.current || waiting) return
    inFlight.current = true
    setSaving(true)
    setDraft((d) => ({ ...d, [key]: next }))
    setError('')
    setNotice('')
    setSaved('')
    // The record spells "inherit" as ''; the picker spells it INHERIT_MODEL.
    const wire = key === 'model' && next === INHERIT_MODEL ? '' : next
    try {
      // The permission is compare-and-set: the server applies it only if the
      // record still holds what this card read, so a stale card can never
      // undo a newer pick. Model and effort are plain last-write-wins.
      // A harness pick also returns both model pins to Inherited: a model chosen
      // for one harness is rarely served by another, and the server would refuse
      // the pick. The person then picks from the new harness's own list.
      const resetModels = key === 'acp_backend'
        ? {
            ...(value('model') !== INHERIT_MODEL ? { model: '' } : {}),
            ...(value('worker_model') !== SAME_AS_CREWMATE ? { worker_model: '' } : {}),
          }
        : {}
      const body = key === 'approval_mode'
        ? { approval_mode: wire, expected_approval_mode: String(member.approval_mode ?? '') }
        : { [key]: wire, ...resetModels }
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
      if (key === 'acp_backend') {
        // The pin is not on the roster row (only the owner readout carries it);
        // the readout is refetched below. The server reloaded the live thread
        // onto the new harness and cleared its own model and effort picks.
        if (Object.keys(resetModels).length) {
          queryClient.setQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY, (rows) =>
            rows?.map((r) => (r.name === member.name ? { ...r, ...resetModels } : r)))
          setDraft((d) => ({
            ...d,
            ...('model' in resetModels ? { model: INHERIT_MODEL } : {}),
            ...('worker_model' in resetModels ? { worker_model: SAME_AS_CREWMATE } : {}),
          }))
        }
        setSaved(t('pages.membersPage.profile_harness_saved', { name }))
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

  const modeLabel = (m: string) => m === 'normal'
    ? t('components.approvalModePicker.normal_label')
    : m === 'trust_reads'
      ? t('components.approvalModePicker.reads_label')
      : t('components.approvalModePicker.trust_label')
  const modelLabel = (m: string) => (m === INHERIT_MODEL ? t('pages.kiroCrewAgentsPage.inherited') : m)
  const harnessLabel = (h: string) => (h === INHERIT_HARNESS ? t('pages.kiroCrewAgentsPage.inherited') : acpBackendName({ id: h }))
  const workerHarnesses = resolved?.worker_harnesses
  const harnessOptions = withCurrent([INHERIT_HARNESS, ...(resolved?.harnesses || [])], harness)
  const workerModelLabel = (m: string) => (
    m === SAME_AS_CREWMATE ? t('pages.membersPage.profile_worker_same_as', { name }) : m === WORKER_AUTO ? t('pages.membersPage.profile_worker_auto') : m
  )

  // The group, top to bottom: the ceiling first, then what runs, then the workers.
  const pickers: Picker[] = [
    {
      key: 'approval_mode',
      label: t('pages.membersPage.profile_permissions'),
      icon: <Shield size={16} />, tone: 'ok',
      value: mode, valueLabel: modeLabel(mode),
      // What each mode lets the crewmate do, under its own label: the bare word
      // "Trust" alone does not say it.
      options: MODES.map((m) => ({
        value: m, label: modeLabel(m),
        desc: m === 'normal'
          ? t('pages.membersPage.profile_mode_normal_desc', { name })
          : m === 'trust_reads'
            ? t('pages.membersPage.profile_mode_reads_desc', { name })
            : t('pages.membersPage.profile_mode_trust_desc', { name }),
      })),
    },
    ...(harnessRowOn ? [{
      key: 'acp_backend' as const,
      label: t('pages.membersPage.profile_harness'),
      icon: <Cpu size={16} />, tone: 'accent' as const,
      // "Agent harness" alone does not say what it is: the row says so under
      // its label, and an Inherited value names the harness it resolves to.
      sub: t('pages.membersPage.profile_harness_sub', { name }),
      value: harness,
      valueLabel: harness === INHERIT_HARNESS && resolved?.inherited_harness
        ? t('pages.membersPage.profile_harness_inherited_value', { harness: acpBackendName({ id: resolved.inherited_harness }) })
        : harnessLabel(harness),
      // Inherited names what it resolves to; a harness that cannot run this
      // crewmate's workers says so under its label, before it is picked. The
      // saved pin that cannot start on this gateway says only that: "can chat"
      // under it would contradict the notice that its chats don't start.
      options: harnessOptions.map((h) => ({
        value: h, label: harnessLabel(h),
        desc: h === INHERIT_HARNESS
          ? (resolved?.inherited_harness ? t('pages.membersPage.profile_harness_inherited_hint', { harness: acpBackendName({ id: resolved.inherited_harness }) }) : undefined)
          : resolved?.harness_error && h === resolved.acp_backend
            ? t('pages.membersPage.profile_harness_cannot_start')
            : workerHarnesses && !workerHarnesses.includes(h) ? t('pages.membersPage.profile_harness_no_workers', { name }) : undefined,
      })),
      caption: t('pages.membersPage.profile_harness_hint', { name }),
    }] : []),
    {
      key: 'model',
      label: t('pages.kiroCrewAgentsPage.model'),
      icon: <Sparkles size={16} />, tone: 'accent',
      value: model, valueLabel: modelLabel(model),
      // The inherit option must NOT read as "auto": here it means "pin nothing,
      // inherit the next tier", which can resolve to a concrete model.
      // "Inherited" alone does not say what runs: the option names the model it
      // resolves to, under its label, so the reader sees it before picking.
      options: modelOptions.map((m) => ({
        value: m, label: modelLabel(m),
        desc: m === INHERIT_MODEL && resolved?.model ? t('pages.membersPage.profile_model_inherited_hint', { model: resolved.model }) : undefined,
      })),
    },
    ...(effortCapable || !!value('reasoning_effort') ? [{
      key: 'reasoning_effort' as const,
      label: t('pages.kiroCrewAgentsPage.reasoning_effort'),
      icon: <Gauge size={16} />, tone: 'accent' as const,
      value: value('reasoning_effort'),
      valueLabel: value('reasoning_effort') === '' ? t('pages.kiroCrewAgentsPage.inherited') : effortLabel(value('reasoning_effort')),
      // The inherit option says what level it resolves to; the caption says
      // what a level trades.
      options: EFFORT_LEVELS.map((l) => ({
        value: l, label: l === '' ? t('pages.kiroCrewAgentsPage.inherited') : effortLabel(l),
        desc: l === '' && resolved?.reasoning_effort ? t('pages.kiroCrewAgentsPage.effort_resolves_to', { effort: effortLabel(resolved.reasoning_effort) }) : undefined,
      })),
      caption: t('pages.membersPage.profile_effort_hint'),
    }] : []),
    {
      // The helper chats this crewmate starts to split up a task (session_create):
      // by default they run what the crewmate runs; the Auto pin makes them cheap.
      // When the effort row below folds away (the chosen model does not reason),
      // the caption says so, so the row never just vanishes.
      key: 'worker_model',
      label: t('pages.membersPage.profile_worker_model'),
      icon: <Users size={16} />, tone: 'info',
      value: workerModel, valueLabel: workerModelLabel(workerModel),
      options: workerModelOptions.map((m) => ({ value: m, label: workerModelLabel(m) })),
      caption: [
        workerModel === SAME_AS_CREWMATE
          ? t('pages.membersPage.profile_worker_same_hint', { name, model: ownModelNoun })
          : workerModel === WORKER_AUTO
            ? t('pages.membersPage.profile_worker_auto_hint', { name, model: ownModelNoun })
            : t('pages.membersPage.profile_worker_model_hint', { name, model: workerModel, own: ownModelNoun }),
        // Says what is true when the effort row folds: the setting does not apply
        // to that model. Never "no reasoning", which reads as a downgrade.
        workerEffortRowShown ? '' : t('pages.membersPage.profile_worker_no_effort_hint'),
      ].filter(Boolean).join(' '),
    },
    ...(workerEffortRowShown ? [{
      // '' means "the crewmate's own level" only while the workers run the
      // crewmate's own model; under another model it is the model's default,
      // and the label says which (worker_session_defaults draws the same line).
      key: 'worker_reasoning_effort' as const,
      label: t('pages.membersPage.profile_worker_effort'),
      icon: <Gauge size={16} />, tone: 'info' as const,
      value: workerEffort,
      valueLabel: workerEffort === '' ? workerEffortInheritLabel : effortLabel(workerEffort),
      options: EFFORT_LEVELS.map((l) => ({ value: l, label: l === '' ? workerEffortInheritLabel : effortLabel(l) })),
      // The empty option always gets a note: under the crewmate's model it
      // names the level that carries over; under another model it says no
      // level is pinned and the crewmate's is not copied (what the worker's
      // agent then defaults to is resolved at its first turn, not here).
      caption: workerEffort !== SAME_AS_CREWMATE
        ? undefined
        : workerModel === SAME_AS_CREWMATE
          ? (ownEffort ? t('pages.membersPage.profile_worker_effort_same_hint', { name, effort: effortLabel(ownEffort) }) : undefined)
          : t('pages.membersPage.profile_worker_effort_default_hint', { name, effort: ownEffort ? ` (${effortLabel(ownEffort)})` : '' }),
    }] : []),
  ]
  // The page last shown keeps painting through the card's exit slide. When its
  // row folded (the pick made on it cleared a stale level, so the row left
  // `pickers`), the live lookup is empty and the last rendered snapshot stands
  // in, held, so the page slides out as it was rather than blank.
  const livePicker = pickers.find((p) => p.key === shown) ?? null
  const lastPicker = useRef<Picker | null>(null)
  useEffect(() => { if (livePicker) lastPicker.current = livePicker }, [livePicker])
  const shownPicker = livePicker ?? lastPicker.current
  const leaving = !livePicker
  const pagePicker = pickers.find((p) => p.key === page) ?? null
  // A row that folded while its page was open has no page left to show: pop
  // it. The card lands focus on itself when the opener (that row) is gone.
  const pageGone = !!page && !pagePicker

  // The card owns the slide, the back control and Escape; this component only
  // says which pages are open: the Settings group at depth 0, a picker over it
  // at depth 1. Each pop callback hands the state back before the slide, so the
  // level under it is already current when the slide ends. The group's pop
  // also drops any picker: both are gone with it.
  // Keyed on the stable `open` / `close`, never on `push` itself: that object
  // changes when a host mounts, and re-running `open` then would rebuild the
  // level with the wrong opener (the back control, where focus has just landed).
  const settingsTitle = t('pages.membersPage.profile_settings')
  const pushOpen = push?.open
  const pushClose = push?.close
  useEffect(() => {
    if (!open || !pushOpen) return
    pushOpen(settingsTitle, () => { setOpen(false); setPage(null) }, 0)
  }, [open, pushOpen, settingsTitle])
  const pageTitle = pagePicker?.label
  useEffect(() => {
    if (!open || !page || !pushOpen || !pageTitle) return
    pushOpen(pageTitle, () => setPage(null), 1)
  }, [open, page, pushOpen, pageTitle])
  useEffect(() => { if (pageGone) { setPage(null); pushClose?.(1) } }, [pageGone, pushClose])
  // The guide's reveal scope: `members.permissions` sits on the Settings page,
  // so a guide pointing at it opens this page first while it is closed.
  useGuideRevealScope('members.settings', open)

  const openSettings = () => setOpen(true)
  const openPage = (key: Setting) => { setPage(key); setShown(key) }
  const busy = saving || waiting || leaving
  // The Settings row's reading: the ceiling and what runs. Not the worker pick:
  // "Same as Kiro" beside Kiro's own name read as a riddle on the card.
  const summary = pickers.filter((p) => p.key === 'approval_mode' || p.key === 'model').map((p) => p.valueLabel).join(' · ')

  // The notices follow the person: on the open page while one is open, under the
  // group otherwise. No hand-off on any of them: the Crewmates page's composer may
  // hold an unsent draft to this crewmate, and "Ask the agent" navigates away.
  const notices = (
    <>
      <ErrorNotice
        message={resolvedError ? (resolvedError instanceof Error ? resolvedError.message : String(resolvedError)) : null}
        testId="crew-profile-settings-resolve-error"
      />
      <ErrorNotice
        message={(harness ? harnessModels.isError : (models.error || models.isDegraded)) ? t('pages.chatSidebar.model_list_failed') : null}
        testId="crew-profile-settings-models-error"
      />
      {/* Our own words, with the harness named, not the server's English sentence,
          and the way out inside it: the Agent harness picker. */}
      <ErrorNotice
        message={resolved?.harness_error && resolved.acp_backend
          ? t('pages.membersPage.profile_harness_unavailable', { name, harness: acpBackendName({ id: resolved.acp_backend }) })
          : null}
        testId="crew-profile-settings-harness-error"
        footer={harnessRowOn && page !== 'acp_backend' ? (
          <Btn type="button" onClick={() => openPage('acp_backend')} data-testid="crew-profile-settings-harness-fix">
            {t('pages.membersPage.profile_harness_choose')}
          </Btn>
        ) : undefined}
      />
      <ErrorNotice message={error || null} testId="crew-profile-settings-error" />
      <ErrorNotice message={notice || null} testId="crew-profile-settings-notice" />
      {saved && <p className="m-0 px-3.5 text-[12px] text-muted" role="status" data-testid="crew-profile-settings-saved">{saved}</p>}
    </>
  )
  const waitingLine = waiting && (
    <p className="text-[12px] text-muted" role="status" data-testid="crew-profile-settings-waiting">{t('pages.membersPage.profile_settings_waiting')}</p>
  )

  const rowProps = (p: Picker) => ({
    icon: p.icon, tone: p.tone, label: p.label, sub: p.sub, value: p.valueLabel,
    onClick: () => openPage(p.key), testId: `crew-profile-setting-${p.key}`,
  })
  const row = (p: Picker) => (p.key === 'approval_mode'
    // The crewmate's approval mode is its own ceiling: no guide points at it,
    // by location id or by name. Its accessible name is the label alone (the
    // find_ui location reads it); the value rides along as the description.
    ? <GuideTrustRootRegion key={p.key}><Row {...rowProps(p)} aria-label={t('pages.membersPage.profile_permissions')} {...uiLocation('members.permissions')} /></GuideTrustRootRegion>
    : <Row key={p.key} {...rowProps(p)} />)

  /** Roving focus over a page's options: Up/Down move (wrapping), Home/End jump.
   *  Moving only focuses; the pick is Enter / Space on the focused option, so
   *  browsing the list never saves. */
  const moveBetweenOptions = (e: KeyboardEvent<HTMLButtonElement>) => {
    const keys: Record<string, number | 'first' | 'last'> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1, Home: 'first', End: 'last' }
    const step = keys[e.key]
    if (step === undefined) return
    const radios = Array.from(e.currentTarget.closest('[role="radiogroup"]')?.querySelectorAll<HTMLButtonElement>('[role="radio"]') ?? [])
    if (!radios.length) return
    const at = radios.indexOf(e.currentTarget)
    const to = step === 'first' ? 0 : step === 'last' ? radios.length - 1 : (at + step + radios.length) % radios.length
    e.preventDefault()
    radios[to]?.focus()
  }

  /** The pushed page: one choice per row, the current one checked. The options
   *  are held with `aria-disabled`, never the `disabled` attribute: a pick is
   *  made from the focused option, and disabling it mid-save would drop focus
   *  onto the body, outside the card, where its Escape handler cannot see it.
   *  `save` already refuses a second pick while one is in flight. */
  const pageBody = (p: Picker) => {
    const list = (
      <div aria-busy={busy} className="flex flex-col gap-3 min-w-0" data-testid={`crew-profile-setting-page-${p.key}`}>
        {waitingLine}
        <RowGroup caption={p.caption}>
          {/* A radio group: one Tab stop (the checked option), arrows between the
              options, Enter/Space pick. */}
          <div role="radiogroup" aria-label={p.label} aria-disabled={busy || undefined}>
            {p.options.map((o) => {
              const on = o.value === p.value
              return (
                <button
                  key={o.value || 'inherit'}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  aria-disabled={busy || undefined}
                  tabIndex={on ? 0 : -1}
                  onKeyDown={moveBetweenOptions}
                  onClick={() => { if (!on && !busy) void save(p.key, o.value) }}
                  className={cn(
                    'flex items-center gap-3 w-full px-3.5 py-3 text-left border-t border-border first:border-t-0 transition-colors',
                    busy ? 'cursor-default' : 'hover:bg-bg-hover cursor-pointer',
                    on && 'text-accent',
                  )}
                  data-testid={`crew-profile-option-${p.key}-${o.value || 'inherit'}`}
                >
                  <span className="flex-1 min-w-0 leading-tight">
                    <span className="block text-[13px] font-semibold">{o.label}</span>
                    {o.desc && <span className="block text-[12px] text-muted leading-snug mt-0.5">{o.desc}</span>}
                  </span>
                  {/* The checked option's mark: a spinner while its save runs, so a
                      pick visibly lands rather than the check just jumping. */}
                  <span className="w-[15px] shrink-0 text-accent" aria-hidden="true">
                    {on && (saving ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />)}
                  </span>
                </button>
              )
            })}
          </div>
        </RowGroup>
        {notices}
      </div>
    )
    // The crewmate's approval mode is its own ceiling: no guide points at it,
    // by location id or by name, on its page any more than on its row.
    return p.key === 'approval_mode' ? <GuideTrustRootRegion>{list}</GuideTrustRootRegion> : list
  }

  /** The Settings page: the group of rows, pushed over the card. */
  const groupBody = (
    <section className="flex flex-col gap-3 min-w-0" data-testid="crew-profile-settings">
      {waitingLine}
      <RowGroup
        // The lock's one line of help, as the group's caption: the row itself
        // carries only the switch and its name.
        caption={t('pages.membersPage.profile_dashboard_lock_hint', { name })}
      >
        {pickers.map(row)}
        <RowStatic icon={<Lock size={16} />} tone="warn" label={t('pages.membersPage.profile_dashboard_lock_switch')} testId="crew-profile-dashboard-lock">
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
        </RowStatic>
      </RowGroup>
      <ErrorNotice message={lockError} testId="crew-profile-dashboard-lock-error" />
      {/* Its own group, so its caption (what the preview lets the crewmate do)
          sits under it rather than under the lock's. */}
      <RowGroup caption={t('pages.membersPage.profile_dynamic_capabilities_hint', { name })}>
        <RowStatic
          icon={<Puzzle size={16} />}
          tone="accent"
          label={t('pages.membersPage.profile_dynamic_capabilities')}
          sub={t('pages.membersPage.profile_dynamic_capabilities_switch', { name })}
          testId="crew-profile-dynamic-capabilities"
        >
          <span
            className="shrink-0 rounded-md bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-accent"
            data-testid="crew-profile-dynamic-capabilities-preview"
          >
            {t('pages.membersPage.profile_preview_tag')}
          </span>
          <Toggle
            checked={dynOn}
            onChange={(v) => dynFlip.mutate(v)}
            disabled={dynFlip.isPending}
            label={t('pages.membersPage.profile_dynamic_capabilities_switch', { name })}
          />
        </RowStatic>
      </RowGroup>
      <ErrorNotice
        message={dynFlip.isError ? t('pages.membersPage.profile_dynamic_capabilities_failed') : null}
        testId="crew-profile-dynamic-capabilities-error"
      />
      {!page && notices}
    </section>
  )

  // Without a card to push onto (no provider) the group renders in place.
  if (!push) return groupBody
  return (
    <>
      {/* The one row the Profile tab shows: a door reading the current picks.
          It is the guide's reveal control for the Settings page. */}
      <Row
        icon={<SlidersHorizontal size={16} />}
        tone="accent"
        label={settingsTitle}
        sub={summary}
        onClick={openSettings}
        testId="crew-profile-settings-row"
        aria-label={t('pages.membersPage.profile_settings')}
        {...uiLocation('members.settings')}
      />
      {/* Painted while the host exists, not while `open`: the host outlives the
          pop for the exit slide, and the page must not go blank on its way out. */}
      {push.hosts[0] ? createPortal(groupBody, push.hosts[0]) : null}
      {push.hosts[1] && shownPicker ? createPortal(pageBody(shownPicker), push.hosts[1]) : null}
    </>
  )
}
