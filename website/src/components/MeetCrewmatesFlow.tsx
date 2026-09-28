import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Check, ChevronRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { api, type SlackConfigData } from '../api/client'
import { ApiError } from '../api/apiError'
import { cronJobsQuery } from '../api/cronJobsQuery'
import { parseErrorCode } from '../utils/errorReport'
import { useDocumentImeLatch, useImeGuard } from '../hooks/useImeGuard'
import { compareText } from '../i18n/format'
import CrewAvatar from './CrewAvatar'
import ErrorNotice from './ErrorNotice'
import OnboardingChapterShell, { OnboardingShellContext } from './OnboardingChapterShell'
import SimpleSelect from './SimpleSelect'
import { Btn, Input, SendBtn, Toggle } from './ui'

/**
 * "Meet CrewMates" — the first-run flow for a user who has NO crewmates and NO
 * custom agents (an existing user with custom agents gets the opt-in step
 * instead; `App` gates the two on the same fact so they never both fire).
 *
 * Four steps in the shipped split-screen chapter chrome:
 *   1. Meet CrewMates            what a crewmate is + three examples
 *   2. Name your first crewmate  name (prefilled) + Built from
 *   3. Give <Name> a job         what it looks after / when / where it reports
 *   4. <Name> is ready           big avatar + when it starts
 *
 * Create (step 3 → 4) is two existing writes: POST /api/agents (the crewmate,
 * with its own memory allocated by the server) and, unless "Only when I ask" was
 * picked, POST /api/crons bound to the crewmate (`member_id`) so the job runs on
 * the crewmate's own memory. The job text is stored as the crewmate's
 * `description` — the roster's "what it is for" field — and repeated in the
 * schedule's message so the crewmate knows what to do on each wake.
 *
 * Completion and dismissal both report through `onDone`; the host persists the
 * flag (`dashboard.crewmates_onboarded`) so the flow fires once per workspace.
 * The Crewmates page empty state re-opens it through the `mc-start-meet-crewmates`
 * window event, which the host listens for.
 */

export const START_MEET_CREWMATES_EVENT = 'mc-start-meet-crewmates'

const TOTAL_STEPS = 4
const NAME_MAX = 24
/**
 * The backend's agent-name grammar (`validation._AGENT_NAME_RE`), which `POST
 * /api/agents` now enforces (`invalid_agent_name`). This copy exists only so
 * the hint under the name field can appear as the user types, before the
 * request; the server is the gate. `test/test_meet_crewmates_builtin_pin.py`
 * keeps the two in step.
 */
export const AGENT_NAME_RE = /^[a-zA-Z0-9](?:[a-zA-Z0-9_-]{0,62}[a-zA-Z0-9])?$/
export function isValidCrewmateName(name: string): boolean {
  return AGENT_NAME_RE.test(name)
}
const JOB_MAX = 200
/** The hour the "Every morning" schedule fires at, in the browser's zone. */
const MORNING_HOUR = 9
/** The built-in agent every crewmate can be built from. */
const DEFAULT_TEMPLATE = 'kirocrew'

type WhenChoice = 'morning' | 'hourly' | 'ask'
const WHEN_CHOICES: readonly WhenChoice[] = ['morning', 'hourly', 'ask']
// Literal keys, never assembled: the dead-key and dynamic-key gates read the
// source for quoted dotted keys.
const WHEN_KEYS: Record<WhenChoice, string> = {
  morning: 'components.meetCrewmatesFlow.when_morning',
  hourly: 'components.meetCrewmatesFlow.when_hourly',
  ask: 'components.meetCrewmatesFlow.when_ask',
}
/** The three example crewmates: name, the row's job line, the chip gloss and the
 *  imperative task the job field is prefilled with. */
const EXAMPLES = [
  {
    id: 'radar',
    name: 'components.meetCrewmatesFlow.example_radar_name',
    job: 'components.meetCrewmatesFlow.example_radar_job',
    chip: 'components.meetCrewmatesFlow.example_radar_chip',
    task: 'components.meetCrewmatesFlow.example_radar_task',
  },
  {
    id: 'scribe',
    name: 'components.meetCrewmatesFlow.example_scribe_name',
    job: 'components.meetCrewmatesFlow.example_scribe_job',
    chip: 'components.meetCrewmatesFlow.example_scribe_chip',
    task: 'components.meetCrewmatesFlow.example_scribe_task',
  },
  {
    id: 'fixer',
    name: 'components.meetCrewmatesFlow.example_fixer_name',
    job: 'components.meetCrewmatesFlow.example_fixer_job',
    chip: 'components.meetCrewmatesFlow.example_fixer_chip',
    task: 'components.meetCrewmatesFlow.example_fixer_task',
  },
] as const

interface InstalledAgentRow {
  name: string
  source?: string
  private_to?: string
}

/** Schedule body for the chosen "When", or null for on-demand. */
export function scheduleFor(when: WhenChoice, timeZone: string): { cron?: string; every?: number; timezone?: string } | null {
  if (when === 'morning') return { cron: `0 ${MORNING_HOUR} * * *`, timezone: timeZone }
  if (when === 'hourly') return { every: 3600 }
  return null
}

function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/**
 * The agent templates offered under "Built from": the built-in first, then every
 * installed custom agent that is not some crew's private copy. The first-run
 * gate means this is normally just the built-in, but the flow is also reachable
 * later from the Crewmates page, by which time custom agents may exist.
 */
export function builtFromOptions(installed: InstalledAgentRow[] | undefined): string[] {
  const rows = Array.isArray(installed) ? installed : []
  const customs = rows
    .filter(a => a.name && a.name !== DEFAULT_TEMPLATE && a.name !== 'kirocrew-lite' && !a.private_to)
    .map(a => a.name)
    .sort(compareText)
  return [DEFAULT_TEMPLATE, ...customs.filter(n => n !== DEFAULT_TEMPLATE)]
}

const FIELD_LABEL_CLS = 'block text-[11px] uppercase tracking-wide text-muted mb-1.5'

/**
 * App-level notice for a failed eligibility read (roster or installed agents):
 * the chapter cannot decide whether to fire, so it says so instead of silently
 * never appearing. No agent hand-off, by decision: a first-run user has not
 * met the agent yet; the Crew Members page entry is the recovery path.
 */
export function MeetCrewmatesEligibilityNotice({ onDismiss }: { onDismiss: () => void }) {
  const { t } = useTranslation()
  const btn =
    'px-3 py-1 rounded-lg text-[12px] font-medium cursor-pointer bg-card border border-border text-text hover:border-border-strong transition-colors'
  return (
    <div className="fixed bottom-safe-offset-4 right-safe-offset-4 z-40 max-w-sm flex flex-col gap-2" data-testid="meet-crewmates-eligibility-error">
      {/* Nothing to lose here (a read failed before the flow ever opened), so
          the hand-off is on per `errors-use-error-notice`; it also dismisses
          the notice, since the chat it opens sits behind this overlay. */}
      <ErrorNotice message={t('components.meetCrewmatesFlow.eligibility_error')} askAgent onHandoff={onDismiss} />
      <div className="flex justify-end gap-2">
        <button type="button" className={btn} onClick={onDismiss} data-testid="meet-crewmates-eligibility-dismiss">
          {t('app.dismiss')}
        </button>
        {/* The remedy IS the flow, not a page the user may already be on: the
            same event the Crew Members entry fires, then the notice goes. */}
        <button
          type="button"
          className={btn}
          onClick={() => {
            window.dispatchEvent(new Event(START_MEET_CREWMATES_EVENT))
            onDismiss()
          }}
          data-testid="meet-crewmates-eligibility-open"
        >
          {t('components.meetCrewmatesFlow.eligibility_open')}
        </button>
      </div>
    </div>
  )
}

/** Renders nothing; runs `onMount` once when its subtree enters the DOM. Placed
 *  inside the incoming step so focus is seated on controls that exist, after
 *  `AnimatePresence mode="wait"` has removed the outgoing step. */
function FocusSeat({ onMount }: { onMount: () => void }) {
  const onMountRef = useRef(onMount)
  onMountRef.current = onMount
  useEffect(() => {
    onMountRef.current()
  }, [])
  return null
}

export default function MeetCrewmatesFlow({
  open,
  onDone,
  onCreated,
  persistFailed = false,
}: {
  open: boolean
  /** Fired exactly once per opening, when the user leaves the flow (Not now,
   *  Escape, or "Open <Name>'s chat"). The host closes the flow on it. */
  onDone: (outcome: 'completed' | 'dismissed') => void
  /** Fired the moment the crewmate exists, BEFORE the ready step is shown, so
   *  the host can persist "done" without closing: closing the tab on step 4
   *  must not re-run the flow over a crewmate that is already there. */
  onCreated: () => void
  /** The host could not persist "done"; rendered as an ErrorNotice on the
   *  current step (a refusal at exit is carried to the next entry -- an exit
   *  never waits for the server). */
  persistFailed?: boolean
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const reduceMotion = useReducedMotion()
  const ime = useImeGuard()

  const [step, setStep] = useState(1)
  const [name, setName] = useState(() => t('components.meetCrewmatesFlow.example_radar_name'))
  const [builtFrom, setBuiltFrom] = useState(DEFAULT_TEMPLATE)
  const [job, setJob] = useState(() => t('components.meetCrewmatesFlow.example_radar_task'))
  const [when, setWhen] = useState<WhenChoice>('morning')
  // "Its own chat": whether each run gets a chat of its own in the sidebar
  // (`hide_in_chat: false`). Slack is not a choice: a connected Slack always
  // receives the run (the runtime's owner-DM leg), so that row only tells the
  // truth about it.
  const [reportChat, setReportChat] = useState(true)
  // Step-3 write outcome. `createError` keeps the user on step 3: `unknown`
  // is false for a refusal (4xx: nothing was made) and true when the write got
  // no usable answer (the crewmate MAY exist; the notice points at the
  // Crewmates page instead of inviting a second one). `schedule` is what step
  // 4 says about the cron: `saved`, `refused`
  // (the server answered 4xx, so nothing exists) or `unknown` (a transport
  // error or 5xx AFTER the request left -- the job may exist, so the user is
  // sent to the Schedule page rather than told to create another).
  const [createError, setCreateError] = useState<{ message: string; unknown: boolean } | null>(null)
  // A taken name is a step-2 problem: the flow returns there and says it under
  // the name field, where the fix is. Cleared as soon as the name changes.
  // `taken` = the server's 409: the notice then carries the way to that
  // crewmate (the Crew Members page) and the matching suggestion chip is marked.
  const [nameError, setNameError] = useState<{ message: string; taken: boolean; name: string } | null>(null)
  const [schedule, setSchedule] = useState<'saved' | 'refused' | 'unknown' | 'none'>('saved')
  const [createdName, setCreatedName] = useState('')
  // Direction of the last step change, for the slide.
  const dirRef = useRef(1)

  const trimmed = name.trim()
  const nameValid = isValidCrewmateName(trimmed)
  // A client-side validation hint, shown as plain text under the field once
  // the user has typed something the server would persist but never list;
  // never on the empty field (Next is disabled anyway). Not an ErrorNotice:
  // nothing failed, no request was made.
  const nameShapeHint = trimmed && !nameValid ? t('components.meetCrewmatesFlow.error_name_shape') : null
  const displayName = trimmed || t('components.meetCrewmatesFlow.example_radar_name')

  // Reset on every opening so a re-entry from the Crewmates page starts clean.
  // `t` is read through a ref: a language switch mid-flow must not reset the
  // user's typed name and job.
  const tRef = useRef(t)
  tRef.current = t
  useEffect(() => {
    if (!open) return
    dirRef.current = 1
    setStep(1)
    setName(tRef.current('components.meetCrewmatesFlow.example_radar_name'))
    setBuiltFrom(DEFAULT_TEMPLATE)
    setJob(tRef.current('components.meetCrewmatesFlow.example_radar_task'))
    setWhen('morning')
    setReportChat(true)
    setCreateError(null)
    setNameError(null)
    setSchedule('saved')
    setCreatedName('')
  }, [open])

  const { data: installed, isError: installedFailed } = useQuery<InstalledAgentRow[]>({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
    enabled: open,
  })
  const { data: slack, isError: slackFailed } = useQuery<SlackConfigData>({
    queryKey: ['slack-config'],
    queryFn: api.getSlackConfig,
    enabled: open,
    retry: false,
  })
  // The toggle promises a CONNECTED Slack, not merely configured tokens.
  const slackReady = !!slack?.connected
  const templates = useMemo(() => builtFromOptions(installed), [installed])
  const templateLabels = useMemo(
    () => templates.map(n => (n === DEFAULT_TEMPLATE ? t('components.meetCrewmatesFlow.default_agent_option') : n)),
    [templates, t],
  )

  // Every exit reports through the host, which closes at once; the exits that
  // also navigate do so right after, onto a page the chapter no longer covers.
  const finish = useCallback(
    (outcome: 'completed' | 'dismissed') => {
      onDone(outcome)
    },
    [onDone],
  )

  const go = (next: number) => {
    dirRef.current = next > step ? 1 : -1
    setCreateError(null)
    setStep(next)
  }

  const create = useMutation({
    mutationFn: async () => {
      const crewmate = trimmed
      const jobText = job.trim()
      // The crewmate's IDENTITY is held only from a clean create response: the
      // immutable `member_id` the server allocated with its member memory
      // (`member_config_for_id` resolves it; a display name or slug is never
      // identity). It is the one thing this flow binds to or reconciles against
      // later. A create that leaves without a usable answer (a dropped
      // response, a 5xx) holds none, so nothing is claimed by NAME: two
      // openings prefill the same example name, and a same-named crewmate this
      // flow cannot prove it made is someone else's -- a 409 `agent_exists` is
      // a taken name on every attempt. The unanswered case is disclosed on
      // step 3 instead (`createError.unknown`), naming the page to check.
      const r = (await api.createKirocrewAgent({
        name: crewmate,
        kiro_agent: builtFrom,
        description: jobText,
        source: 'kirocrew',
      })) as { ok?: boolean; error?: string; member_id?: string }
      if (r?.error) throw new Error(r.error)
      const identity = r?.member_id
      if (!identity) throw new Error('create returned no identity')
      const spec = scheduleFor(when, browserTimeZone())
      // `none`: the user chose "Only when I ask", so there is no schedule to
      // report on. `saved` is reserved for a schedule that actually exists.
      let outcome: 'saved' | 'refused' | 'unknown' | 'none' = spec ? 'saved' : 'none'
      if (spec) {
        const cronName = t('components.meetCrewmatesFlow.cron_name', { name: crewmate })
        try {
          await api.createCron({
            name: cronName,
            message: jobText,
            agent: builtFrom,
            // Bound by the immutable identity, never the display name: a name
            // is late-resolved server-side, so a crewmate deleted and remade
            // under the same name between the two writes would receive this
            // job; the identity resolves to exactly the crewmate made above
            // or to nothing (a refusal the flow then reports honestly).
            member_id: identity,
            // Delivery is MECHANICAL, not an instruction to the model: every run
            // rings the dashboard bell, lands in a chat of its own in the sidebar
            // unless "Its own chat" is off (`hide_in_chat`), and reaches a
            // connected Slack through the runtime's owner-DM leg.
            silent: false,
            hide_in_chat: !reportChat,
            ...spec,
          })
        } catch (e) {
          // A 4xx is the server saying no: nothing exists. Anything else (a
          // dropped response, a 5xx) may have committed, so reconcile against
          // the Schedule list before hedging -- but only a job that IS the one
          // asked for counts: bound to this crewmate's identity (what the
          // server stores as the job's `member_id`), with this exact name,
          // this exact message and this exact schedule. Any other job on the
          // crewmate -- an older one, one someone else made -- is not evidence
          // that THIS write landed. Only when nothing matches, or the read
          // fails, is the outcome `unknown`, and the user is pointed at the
          // Schedule page, never at "make one".
          if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
            outcome = 'refused'
          } else {
            const listed = await api.crons().catch(() => null)
            const isOurs = (j: { member_id?: string; name: string; message: string; cron_expr?: string | null; every_secs?: number | null }) =>
              j.member_id === identity &&
              j.name === cronName &&
              j.message === jobText &&
              (spec.cron ? j.cron_expr === spec.cron : j.every_secs === spec.every)
            outcome = listed?.jobs?.some(isOurs) ? 'saved' : 'unknown'
          }
        }
      }
      return { crewmate, outcome }
    },
    onSuccess: ({ crewmate, outcome }) => {
      // The roster lives under the crew-registry prefix; the Schedule page
      // under its own key.
      qc.invalidateQueries({ queryKey: ['kirocrew-agents'] })
      qc.invalidateQueries({ queryKey: cronJobsQuery.queryKey })
      setCreatedName(crewmate)
      setSchedule(outcome)
      dirRef.current = 1
      setStep(4)
      // Persist "done" the moment the crewmate exists (the host keeps the flow
      // open for the ready step); `finish` runs only when the user leaves.
      onCreated()
    },
    onError: (e: Error) => {
      if (e instanceof ApiError && e.status === 409 && parseErrorCode(e.body) === 'agent_exists') {
        setNameError({ message: t('components.meetCrewmatesFlow.error_name_taken', { name: trimmed }), taken: true, name: trimmed })
        go(2)
        return
      }
      if (e instanceof ApiError && e.status === 400 && parseErrorCode(e.body) === 'invalid_agent_name') {
        // The server is the grammar gate; the hint under the field is only its
        // preview. If they ever disagree, the server's word lands here.
        setNameError({ message: t('components.meetCrewmatesFlow.error_name_shape'), taken: false, name: trimmed })
        go(2)
        return
      }
      // A 4xx is the server saying no: nothing was made, trying again is safe.
      // Anything else (a dropped response, a 5xx) may have committed a crewmate
      // this flow holds no identity for, so it is neither claimed nor called
      // absent: the notice names the page where the answer is.
      const refused = e instanceof ApiError && e.status >= 400 && e.status < 500
      setCreateError({
        message: t(
          refused ? 'components.meetCrewmatesFlow.error_create_failed' : 'components.meetCrewmatesFlow.error_create_unknown',
          { name: trimmed },
        ),
        unknown: !refused,
      })
    },
  })
  const busy = create.isPending

  const dismiss = useCallback(() => {
    if (busy) return
    finish('dismissed')
  }, [busy, finish])

  const openChat = () => {
    finish('completed')
    navigate(`/members?member=${encodeURIComponent(createdName)}`)
  }

  // ── Dialog a11y: initial focus, Tab trap, Escape ──────────────────────────
  const shellHost = useContext(OnboardingShellContext)
  const localDialogRef = useRef<HTMLDivElement>(null)
  const dialogRef = shellHost?.dialogRef ?? localDialogRef
  const imeLatch = useDocumentImeLatch(open)
  const getFocusable = useCallback(() => {
    const node = dialogRef.current
    if (!node) return [] as HTMLElement[]
    return Array.from(
      node.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),
    ).filter(el => !el.hasAttribute('disabled'))
  }, [dialogRef])
  // Seats focus on the first control of the step that is IN the DOM. Called
  // from `<FocusSeat>` inside the incoming step's subtree, because the step
  // bodies swap through `AnimatePresence mode="wait"`: on the `step` commit the
  // dialog still holds only the OUTGOING step, so a seat taken from the parent
  // effect at that moment lands on a control that unmounts 0.22 s later and
  // focus falls back to `document.body` -- outside the Tab trap below.
  const seatFocus = useCallback(() => {
    getFocusable()[0]?.focus()
  }, [getFocusable])
  const busyKeyRef = useRef<boolean | null>(null)
  useEffect(() => {
    if (!open) return
    if (!dialogRef.current) return
    // Re-seat only when `busy` FLIPS: the controls it disables may hold focus,
    // and when they come back the first control is the sane place to land.
    // Step entry is seated by `<FocusSeat>` (see `seatFocus`); this effect
    // re-runs on each keystroke (through `dismiss`), and re-seating focus here
    // would yank the caret out of the name field.
    if (busyKeyRef.current === null) busyKeyRef.current = busy
    else if (busyKeyRef.current !== busy) {
      busyKeyRef.current = busy
      seatFocus()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        // After the crewmate exists Escape only closes; before, it is "Not now".
        if (step === 4) finish('completed')
        else dismiss()
        return
      }
      if (e.key !== 'Tab') return
      const items = getFocusable()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const wrapsBackward = e.shiftKey && document.activeElement === first
      const wrapsForward = !e.shiftKey && document.activeElement === last
      if (!wrapsBackward && !wrapsForward) return
      // A Tab the IME owns must not cycle focus (see useImeGuard's contract).
      if (!imeLatch.claimKey(e)) return
      e.preventDefault()
      ;(wrapsBackward ? last : first).focus()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
    // `shellHost?.sectionSlot` is a dependency on purpose: in host mode the
    // dialog node does not exist on the commit where `open` flips, so the
    // effect bails above and must re-run once the host has rendered it --
    // otherwise step 1 would ship with no Escape and no Tab trap.
  }, [open, step, busy, dismiss, finish, dialogRef, getFocusable, seatFocus, imeLatch, shellHost?.sectionSlot])

  if (!open) return null

  const eyebrow = t('components.meetCrewmatesFlow.step_eyebrow', { n: step, total: TOTAL_STEPS })
  const aside = {
    ariaLabel: t('components.meetCrewmatesFlow.aria_label'),
    panelHeadline: t('components.meetCrewmatesFlow.panel_headline'),
    panelBody: t('components.meetCrewmatesFlow.panel_body'),
    panelFootnote: t('components.meetCrewmatesFlow.panel_footnote'),
  }

  // One step slides out, the next slides in. The step counter in the eyebrow
  // and the footer fade so the shell chrome never jumps.
  const slide = reduceMotion
    ? { initial: false as const, animate: { opacity: 1, x: 0 }, exit: { opacity: 1, x: 0 } }
    : {
        initial: { opacity: 0, x: 24 * dirRef.current },
        animate: { opacity: 1, x: 0 },
        exit: { opacity: 0, x: -24 * dirRef.current },
      }
  const stepMotion = { ...slide, transition: { duration: reduceMotion ? 0 : 0.22, ease: 'easeOut' as const } }
  const fade = reduceMotion
    ? { initial: false as const, animate: { opacity: 1 }, exit: { opacity: 1 } }
    : { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 } }
  const footerMotion = { ...fade, transition: { duration: reduceMotion ? 0 : 0.16 } }

  const title = (text: string, body?: string) => (
    <div className="mb-6">
      <h1 tabIndex={-1} className="text-2xl font-semibold text-text-strong outline-hidden" data-testid="meet-crewmates-title">
        {text}
      </h1>
      {body && <p className="mt-2 text-sm leading-relaxed text-muted">{body}</p>}
    </div>
  )

  let body: ReactNode
  let footer: ReactNode
  if (step === 1) {
    body = (
      <>
        {title(t('components.meetCrewmatesFlow.step1_title'), t('components.meetCrewmatesFlow.step1_body'))}
        <ul className="flex flex-col divide-y divide-border rounded-xl border border-border bg-bg-elevated list-none m-0 p-0" data-testid="meet-crewmates-examples">
          {EXAMPLES.map(ex => (
            <li key={ex.id}>
              {/* A row that looks selectable IS selectable: it preselects this
                  example's name and job and moves on to step 2. */}
              <button
                type="button"
                onClick={() => {
                  setName(t(ex.name))
                  setNameError(null)
                  setJob(t(ex.task))
                  go(2)
                }}
                aria-label={t('components.meetCrewmatesFlow.start_with', { name: t(ex.name) })}
                className="w-full flex items-center gap-4 px-4 py-3.5 text-left cursor-pointer hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring first:rounded-t-xl last:rounded-b-xl"
                data-testid={`meet-crewmates-example-${ex.id}`}
              >
                <CrewAvatar seed={t(ex.name)} size={44} />
                <div className="min-w-0 flex-1">
                  <div className="text-[14px] font-medium text-text-strong">{t(ex.name)}</div>
                  <div className="text-[13px] text-muted truncate">{t(ex.job)}</div>
                </div>
                <ChevronRight size={16} className="shrink-0 text-muted" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
        <p className="mt-4 text-[13px] leading-relaxed text-muted">{t('components.meetCrewmatesFlow.step1_footnote')}</p>
      </>
    )
    footer = (
      <>
        <Btn type="button" className="h-9 rounded-lg px-4" onClick={dismiss} data-testid="meet-crewmates-not-now">
          {t('components.meetCrewmatesFlow.not_now')}
        </Btn>
        <SendBtn type="button" onClick={() => go(2)} data-testid="meet-crewmates-next">
          {t('components.meetCrewmatesFlow.next')}
        </SendBtn>
      </>
    )
  } else if (step === 2) {
    body = (
      <>
        {title(t('components.meetCrewmatesFlow.step2_title'), t('components.meetCrewmatesFlow.step2_body'))}
        <div className="flex items-start gap-5">
          {/* Greyed while the name fails the grammar: the avatar is drawn from
              the name, and a fresh colour beside the hint read as "the name
              changed", not "the name is not accepted". */}
          <div className={nameShapeHint ? 'shrink-0 pt-5 opacity-40 grayscale' : 'shrink-0 pt-5'} data-testid="meet-crewmates-avatar">
            <CrewAvatar seed={displayName} size={72} />
          </div>
          <div className="min-w-0 flex-1">
            <label htmlFor="meet-crewmates-name" className={FIELD_LABEL_CLS}>
              {t('components.meetCrewmatesFlow.name_label')}
            </label>
            <Input
              id="meet-crewmates-name"
              type="text"
              value={name}
              onChange={e => {
                setName(e.target.value)
                setNameError(null)
              }}
              autoComplete="off"
              spellCheck={false}
              maxLength={NAME_MAX}
              aria-invalid={nameError || nameShapeHint ? true : undefined}
              aria-describedby={nameError ? 'meet-crewmates-name-error' : nameShapeHint ? 'meet-crewmates-name-hint' : undefined}
              className="w-full text-[13px]"
              data-testid="meet-crewmates-name"
              {...ime.bindEnter({ onEnter: () => { if (nameValid && !nameError) go(3) } })}
            />
            {nameError && (
              /* The server's refusal (409 taken / 400 grammar). No hand-off: the
                 name and job typed in this flow are unsaved. A taken name carries
                 the way to the crewmate that owns it -- the same footer button
                 the unknown-create notice has -- instead of naming a page the
                 wizard gives no way to reach. */
              <div id="meet-crewmates-name-error">
                <ErrorNotice
                  message={nameError.message}
                  variant={nameError.taken ? 'block' : 'inline'}
                  className={nameError.taken ? 'mt-2 text-left' : 'mt-2'}
                  testId="meet-crewmates-name-error"
                  footer={
                    nameError.taken ? (
                      <button
                        type="button"
                        className="text-[12px] font-medium underline underline-offset-2 text-danger hover:opacity-80 cursor-pointer bg-transparent border-none p-0"
                        onClick={() => {
                          finish('dismissed')
                          navigate('/members')
                        }}
                        data-testid="meet-crewmates-name-taken-open"
                      >
                        {t('components.meetCrewmatesFlow.open_crewmates')}
                      </button>
                    ) : undefined
                  }
                />
              </div>
            )}
            {!nameError && nameShapeHint && (
              /* Warning colour, not muted: the hint only appears once the name
                 fails, and grey read as advice rather than "not accepted yet".
                 Still not an ErrorNotice -- nothing failed, no request was made. */
              <p id="meet-crewmates-name-hint" className="mt-2 text-[12px] text-warn-fg" data-testid="meet-crewmates-name-hint">
                {nameShapeHint}
              </p>
            )}
            <div className="flex flex-wrap gap-1.5 mt-2.5" role="group" aria-label={t('components.meetCrewmatesFlow.suggested_names')}>
              {EXAMPLES.map(ex => {
                const label = t(ex.name)
                const on = trimmed === label
                // The chip whose name the server just refused as taken is marked
                // so it reads as "that one exists", not as a fresh suggestion.
                const taken = !!nameError?.taken && nameError.name === label
                return (
                  <button
                    key={ex.id}
                    type="button"
                    title={taken ? t('components.meetCrewmatesFlow.chip_taken') : undefined}
                    onClick={() => {
                      setName(label)
                      // A chip is a name change like typing one: the server's
                      // refusal of the PREVIOUS name no longer applies, so the
                      // notice clears and Next comes back.
                      setNameError(null)
                      // Only a prefilled (or empty) job follows the chip; a job
                      // the user typed on step 3 is never overwritten.
                      const untouched = !job.trim() || EXAMPLES.some(e => t(e.task) === job)
                      if (untouched) setJob(t(ex.task))
                    }}
                    aria-pressed={on}
                    className={`flex items-center gap-1 rounded-full px-3 py-1.5 text-[13px] cursor-pointer transition-colors border ${
                      taken
                        ? 'border-danger/40 bg-transparent text-muted line-through decoration-danger/60'
                        : on
                          ? 'border-accent bg-accent-subtle text-accent font-medium'
                          : 'border-border bg-transparent text-text hover:text-text-strong'
                    }`}
                  >
                    {on && !taken && <Check className="lucide-inline" aria-hidden />}
                    {label}
                    <span className="text-muted">· {taken ? t('components.meetCrewmatesFlow.chip_taken') : t(ex.chip)}</span>
                  </button>
                )
              })}
            </div>
            <div className="mt-6">
              <label htmlFor="meet-crewmates-built-from" className={FIELD_LABEL_CLS}>
                {t('components.meetCrewmatesFlow.built_from_label')}
              </label>
              <SimpleSelect
                id="meet-crewmates-built-from"
                options={templates}
                optionLabels={templateLabels}
                value={builtFrom}
                onChange={setBuiltFrom}
                aria-label={t('components.meetCrewmatesFlow.built_from_label')}
              />
              <p className="mt-1.5 text-[12px] text-muted">{t('components.meetCrewmatesFlow.built_from_hint', { name: displayName })}</p>
              {installedFailed && (
                /* No hand-off: the name typed above is unsaved. */
                <ErrorNotice
                  message={t('components.meetCrewmatesFlow.built_from_unavailable')}
                  variant="inline"
                  className="mt-3"
                  testId="meet-crewmates-built-from-error"
                />
              )}
            </div>
          </div>
        </div>
      </>
    )
    footer = (
      <>
        <Btn type="button" className="h-9 rounded-lg px-4" onClick={() => go(1)} data-testid="meet-crewmates-back">
          {t('components.meetCrewmatesFlow.back')}
        </Btn>
        <SendBtn type="button" disabled={!nameValid || !!nameError} onClick={() => go(3)} data-testid="meet-crewmates-next">
          {t('components.meetCrewmatesFlow.next')}
        </SendBtn>
      </>
    )
  } else if (step === 3) {
    const jobOk = !!job.trim()
    body = (
      <>
        {title(t('components.meetCrewmatesFlow.step3_title', { name: displayName }), t('components.meetCrewmatesFlow.step3_body'))}
        <label htmlFor="meet-crewmates-job" className={FIELD_LABEL_CLS}>
          {t('components.meetCrewmatesFlow.job_label')}
        </label>
        <Input
          id="meet-crewmates-job"
          type="text"
          value={job}
          onChange={e => setJob(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          maxLength={JOB_MAX}
          disabled={busy}
          className="w-full text-[13px]"
          data-testid="meet-crewmates-job"
          {...ime.bindEnter({ onEnter: () => { if (jobOk && !busy) create.mutate() } })}
        />
        <div className="mt-5">
          <label htmlFor="meet-crewmates-when" className={FIELD_LABEL_CLS}>
            {t('components.meetCrewmatesFlow.when_label')}
          </label>
          <SimpleSelect
            id="meet-crewmates-when"
            options={[...WHEN_CHOICES]}
            optionLabels={WHEN_CHOICES.map(w => t(WHEN_KEYS[w]))}
            value={when}
            onChange={v => setWhen(v as WhenChoice)}
            disabled={busy}
            aria-label={t('components.meetCrewmatesFlow.when_label')}
          />
        </div>
        <div className="mt-5">
          <div id="meet-crewmates-reports-label" className={FIELD_LABEL_CLS}>
            {t('components.meetCrewmatesFlow.reports_label')}
          </div>
          <div
            className="flex flex-col divide-y divide-border rounded-xl border border-border bg-bg-elevated"
            role="group"
            aria-labelledby="meet-crewmates-reports-label"
            data-testid="meet-crewmates-reports"
          >
            <div className="flex items-center justify-between gap-4 px-4 py-3">
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-text-strong">{t('components.meetCrewmatesFlow.report_chat')}</div>
                <div className="text-[12px] text-muted">{t('components.meetCrewmatesFlow.report_chat_hint', { name: displayName })}</div>
              </div>
              <Toggle checked={reportChat} onChange={setReportChat} disabled={busy} label={t('components.meetCrewmatesFlow.report_chat')} />
            </div>
            <div className="px-4 py-3" data-testid="meet-crewmates-slack-row">
              {/* Not a switch: a connected Slack always receives the run, a
                  disconnected one cannot. A toggle here would promise a choice
                  the runtime does not offer, so the row reads as status. The
                  state word sits IN the label line ("Slack DM — Active"), never
                  in the column the toggle above occupies, where plain text
                  still read as something to click. */}
              <div className="text-[13px] font-medium text-text-strong">
                {t('components.meetCrewmatesFlow.report_slack')}
                <span className="font-normal text-muted">{' — '}</span>
                <span className="font-normal text-muted" data-testid="meet-crewmates-slack-state">
                  {slackReady
                    ? t('components.meetCrewmatesFlow.report_slack_state_auto')
                    : t('components.meetCrewmatesFlow.report_slack_state_off')}
                </span>
              </div>
              <div className="text-[12px] text-muted" data-testid="meet-crewmates-slack-hint">
                {slackReady
                  ? t('components.meetCrewmatesFlow.report_slack_hint')
                  : t('components.meetCrewmatesFlow.report_slack_not_connected')}
              </div>
            </div>
          </div>
        </div>
        {slackFailed && (
          /* No hand-off: the job typed above is unsaved. */
          <ErrorNotice
            message={t('components.meetCrewmatesFlow.slack_status_unavailable')}
            variant="inline"
            className="mt-5"
            testId="meet-crewmates-slack-error"
          />
        )}
        {createError && (
          /* No hand-off: the name and job typed above are unsaved. An UNKNOWN
             outcome carries the way to the answer -- the Crewmates page -- so the
             user checks before creating a second one; leaving closes the flow. */
          <ErrorNotice
            message={createError.message}
            /* The block variant carries the footer button; a plain refusal
               stays the one-line inline notice. */
            variant={createError.unknown ? 'block' : 'inline'}
            className={createError.unknown ? 'mt-5 max-w-md text-left' : 'mt-5'}
            testId="meet-crewmates-error"
            footer={
              createError.unknown ? (
                <button
                  type="button"
                  className="text-[12px] font-medium underline underline-offset-2 text-danger hover:opacity-80 cursor-pointer bg-transparent border-none p-0"
                  onClick={() => {
                    finish('completed')
                    navigate('/members')
                  }}
                  data-testid="meet-crewmates-open-crewmates"
                >
                  {t('components.meetCrewmatesFlow.open_crewmates')}
                </button>
              ) : undefined
            }
          />
        )}
      </>
    )
    footer = (
      <>
        <Btn type="button" className="h-9 rounded-lg px-4" disabled={busy} onClick={() => go(2)} data-testid="meet-crewmates-back">
          {t('components.meetCrewmatesFlow.back')}
        </Btn>
        <SendBtn type="button" disabled={!jobOk || busy} onClick={() => create.mutate()} data-testid="meet-crewmates-create">
          {busy
            ? t('components.meetCrewmatesFlow.creating', { name: displayName })
            : t('components.meetCrewmatesFlow.create', { name: displayName })}
        </SendBtn>
      </>
    )
  } else {
    const failed = schedule === 'refused' || schedule === 'unknown'
    const startsKey = failed
      ? 'components.meetCrewmatesFlow.ready_change_later'
      : when === 'morning'
        ? new Date().getHours() < MORNING_HOUR
          ? 'components.meetCrewmatesFlow.ready_starts_morning_today'
          : 'components.meetCrewmatesFlow.ready_starts_morning'
        : when === 'hourly'
          ? 'components.meetCrewmatesFlow.ready_starts_hourly'
          : 'components.meetCrewmatesFlow.ready_starts_ask'
    body = (
      <div className="flex flex-col items-center pt-10 text-center" data-testid="meet-crewmates-ready">
        <div data-testid="meet-crewmates-avatar">
          <CrewAvatar seed={createdName} size={144} />
        </div>
        <h1 tabIndex={-1} className="mt-8 text-2xl font-semibold text-text-strong outline-hidden" data-testid="meet-crewmates-title">
          {t('components.meetCrewmatesFlow.step4_title', { name: createdName })}
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-muted">
          {t(startsKey, { name: createdName })}
          {schedule === 'saved' && (
            <>
              <br />
              {t(reportChat ? 'components.meetCrewmatesFlow.ready_where' : 'components.meetCrewmatesFlow.ready_where_hidden', { name: createdName })}
            </>
          )}
          {!failed && (
            <>
              <br />
              {t('components.meetCrewmatesFlow.ready_change_later')}
            </>
          )}
        </p>
        {failed && (
          /* The crewmate exists and nothing typed is unsaved, so the hand-off
             is on (`errors-use-error-notice`); it closes the flow the way "Open
             <name>'s chat" does, since the chat sits behind this dialog. The
             page the copy names is the recovery path, so the notice also
             carries a button that goes there. */
          <ErrorNotice
            askAgent
            onHandoff={() => finish('completed')}
            message={t(
              schedule === 'refused'
                ? 'components.meetCrewmatesFlow.ready_no_schedule'
                : 'components.meetCrewmatesFlow.ready_schedule_unknown',
              { name: createdName },
            )}
            className="mt-5 max-w-md text-left"
            testId="meet-crewmates-schedule-error"
            footer={
              <button
                type="button"
                className="text-[12px] font-medium underline underline-offset-2 text-danger hover:opacity-80 cursor-pointer bg-transparent border-none p-0"
                onClick={() => {
                  finish('completed')
                  navigate('/schedule')
                }}
                data-testid="meet-crewmates-open-schedule"
              >
                {t('components.meetCrewmatesFlow.open_schedule')}
              </button>
            }
          />
        )}
      </div>
    )
    footer = (
      <>
        <Btn type="button" className="h-9 rounded-lg px-4" onClick={() => finish('completed')} data-testid="meet-crewmates-done">
          {t('components.meetCrewmatesFlow.done')}
        </Btn>
        <SendBtn type="button" onClick={openChat} data-testid="meet-crewmates-open-chat">
          {t('components.meetCrewmatesFlow.open_chat', { name: createdName })}
        </SendBtn>
      </>
    )
  }

  return (
    <OnboardingChapterShell
      {...aside}
      eyebrow={eyebrow}
      dialogRef={dialogRef}
      header={null}
      footer={
        <AnimatePresence mode="wait" initial={false}>
          <motion.div key={step} {...footerMotion} className="flex flex-wrap items-center justify-end gap-3">
            {footer}
          </motion.div>
        </AnimatePresence>
      }
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={step} {...stepMotion} data-testid={`meet-crewmates-step-${step}`}>
          <FocusSeat onMount={seatFocus} />
          {body}
          {persistFailed && (
            /* No hand-off on steps 2-3: the typed name and job are unsaved
               drafts. On steps 1 and 4 nothing can be lost, so the hand-off is on
               (`errors-use-error-notice`); it closes the flow the way the step's
               own exit does, because the chat it opens sits behind this dialog. */
            <ErrorNotice
              message={t('components.meetCrewmatesFlow.save_failed')}
              variant="inline"
              className="mt-5"
              testId="meet-crewmates-persist-error"
              askAgent={step === 1 || step === 4}
              onHandoff={() => finish(step === 4 ? 'completed' : 'dismissed')}
            />
          )}
        </motion.div>
      </AnimatePresence>
    </OnboardingChapterShell>
  )
}
