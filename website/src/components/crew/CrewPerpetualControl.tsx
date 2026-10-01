/**
 * The Perpetual mode SWITCH, shared by the two places the owner reads the
 * state: the crewmate's detail page (`CrewPerpetualSection`, the crew editor's
 * Schedules pane) and the Crewmates page side panel (`MembersPage`, the Work
 * log's Perpetual mode block). One switch, one request, one registry: both
 * hosts render this control, so a press in either place does exactly the same
 * thing and the other place follows on the next registry read.
 *
 * The switch holds NO truth of its own: its position is the loop registry's
 * record (`useCrewPerpetual`), and a press only asks the server
 * (`POST /api/members/{slug}/perpetual`). The registry is re-read when the
 * answer lands -- success or failure -- so a refused press settles back on
 * what the backend holds, never on an optimistic ON. While the request is out
 * the switch is disabled, not flipped: "asked, not yet answered". A press
 * that landed says "Saved", briefly, where "Saving…" just was.
 *
 * Press state is scoped to the crewmate it was pressed for. The detail page
 * remounts this hook per crew (`key={editing}`), but the Crewmates page does
 * not: it re-renders the same hook with whichever crewmate the reader selected,
 * so pending / refused / saved would otherwise be read against a crewmate
 * nothing was asked of.
 *
 * A refusal is rendered by the HOST (through `refusalText`) so it can sit
 * where that surface puts its notices; coded answers first (the server's
 * `code`): the thread must be open once before the switch can address it; a
 * structured monitor is not this switch's loop; a gateway with auto-nudge off
 * has no loop to arm. Anything else uses one plain fallback; server wording
 * stays out of the page.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { fmtNumber } from '../../i18n/format'
import { parseErrorCode } from '../../utils/errorReport'
import { Toggle } from '../ui'
import { AUTONUDGE_LOOPS_QUERY_KEY, intervalText } from '../autoNudgeLoop'
import {
  perpetualIdleSecs,
  useCrewPerpetual,
  wakesPerDay,
  type CrewPerpetualOptions,
  type CrewPerpetualReading,
} from './useCrewPerpetual'

/** Coded refusals the switch can foresee, in plain words. */
const REFUSAL_KEY: Record<string, string> = {
  member_thread_not_open: 'components.crewPerpetualSection.refused_thread_not_open',
  structured_monitor_not_convertible: 'components.crewPerpetualSection.refused_structured_monitor',
  autonudge_disabled: 'components.crewPerpetualSection.refused_autonudge_disabled',
}

/** How long "Saved" stays beside the switch after a press lands. */
const SAVED_MS = 2_500

export interface CrewPerpetualSwitch extends CrewPerpetualReading {
  /** A press is out and unanswered. */
  pending: boolean
  /** A press landed within the last `SAVED_MS`. */
  justSaved: boolean
  /** The last press was refused: its plain-words sentence, '' otherwise. */
  refusalText: string
  /** Both reads answered and the crew has a roster row to address, and the
   *  gateway runs a nudge service: the switch can be offered. */
  canSwitch: boolean
  /** The switch is offered but the thread was never opened: shown disabled
   *  with that sentence as its description instead of pressed into a 409. */
  threadClosed: boolean
  /** Ask the server; ignored while pending or with the thread closed. */
  press: (enabled: boolean) => void
}

export function useCrewPerpetualSwitch(crew: string, options?: CrewPerpetualOptions): CrewPerpetualSwitch {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const reading = useCrewPerpetual(crew, options)
  const { slug, slotKey, loaded, failed, missing, enabled } = reading

  const mutation = useMutation({
    mutationFn: (enabled: boolean) => api.memberPerpetualSet(slug, crew, enabled),
    onSettled: () => {
      // Both readers of the switch: the registry (the switch's own position and
      // readouts) and the roster (its `perpetual` field, the badge on the
      // Crewmates page). Settled, not success -- a refusal must re-read too, so
      // the switch settles on what the backend holds.
      void queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
    },
  })
  // The crewmate the press state on this hook belongs to. The Crewmates page
  // does NOT remount the hook when the reader selects another crewmate -- it
  // re-renders it with a new name (`useCrewPerpetualSwitch(activeMemberName)`)
  // -- so one mutation instance outlives the crewmate it was pressed for.
  // Unscoped, A's refusal, its "Saving…" and its "Saved" all render against B,
  // attributing a change to a crewmate nothing was asked of. The name is
  // recorded on the press and every derived state is withheld unless it still
  // matches, so the switch-over is clean in the SAME render as the new name.
  const [pressedFor, setPressedFor] = useState('')
  const ownPress = pressedFor === crew
  // Keyed on the mutation's submit time so a second press restarts the window.
  const [savedFor, setSavedFor] = useState<number | null>(null)
  useEffect(() => {
    if (!mutation.isSuccess) return
    setSavedFor(mutation.submittedAt)
    const timer = setTimeout(() => setSavedFor(null), SAVED_MS)
    return () => clearTimeout(timer)
  }, [mutation.isSuccess, mutation.submittedAt])
  // `reset` is the observer's own method and stable for the hook's life; held in
  // a ref so the crew-change effect below depends on the NAME alone and cannot
  // re-run itself.
  const resetRef = useRef(mutation.reset)
  resetRef.current = mutation.reset
  useEffect(() => {
    // A crew change drops the previous crewmate's press state for good: the
    // render above already withholds it, and clearing the mutation keeps a
    // later return to that crewmate from resurrecting a stale refusal. An
    // in-flight request still settles and still invalidates both reads, so the
    // switch it belonged to catches up from the registry, not from this state.
    setPressedFor('')
    setSavedFor(null)
    resetRef.current()
  }, [crew])
  const pending = ownPress && mutation.isPending
  const justSaved = ownPress && !mutation.isPending && savedFor !== null && savedFor === mutation.submittedAt
  const refusal = ownPress && mutation.isError ? mutation.error : null
  const refusalText = refusal
    ? (() => {
        const code = parseErrorCode((refusal as { body?: string }).body)
        const key = code ? REFUSAL_KEY[code] : undefined
        return key ? t(key) : t('components.crewPerpetualSection.refused_unknown')
      })()
    : ''

  // The switch is offered once both reads answered and the crew has a roster
  // row to address: a switch over an unknown state would promise a change it
  // cannot describe. A gateway with no nudge service (`enabled: false`) cannot
  // run the mode at all: the switch is withheld and the host says so.
  const canSwitch = loaded && !failed && !missing && !!slug && enabled
  const threadClosed = canSwitch && !slotKey
  const press = (enabled: boolean) => {
    if (pending || threadClosed) return
    setPressedFor(crew)
    mutation.mutate(enabled)
  }
  return { ...reading, pending, justSaved, refusalText, canSwitch, threadClosed, press }
}

/** The id of a host's fact list — what that host's switch is described by.
 *  Derived from the same `testIdPrefix` the control is given, so the two can
 *  only ever be scoped to the same host (`crew-perpetual-facts` on the detail
 *  page, `member-perpetual-facts` in the Work log), and two hosts on one
 *  document cannot collide. */
export function perpetualFactsId(testIdPrefix: string): string {
  return `${testIdPrefix}-facts`
}

/**
 * The facts that decide the press, for BOTH hosts of the switch: what work it
 * continues, what each check costs in message-sized terms, and where the owner
 * reviews that work. It is the switch's accessible
 * description (`aria-describedby={perpetualFactsId(prefix)}`), which is why it
 * is rendered from one place rather than written out per host -- a host that
 * states only one fact, or none, asks for a press on an unstated cost.
 *
 * Body colour, label/value rows: the PRIMARY layer. What does not decide the
 * press (caps, what OFF does, scheduled jobs, the cadence, which control
 * saves) is secondary copy and stays on the detail page, under this list --
 * the side panel is not the place to repeat it, and repeating it there is how
 * a wall of muted sentences grows back.
 *
 * Both facts are stated with the SAME interval -- this crewmate's own record
 * when the registry holds one, else the interval an arm starts on -- so the
 * check timing and cost can never disagree on screen. Nothing renders while the
 * switch cannot be offered: an unknown state has no cost to state, and the
 * control is withheld in exactly the same cases.
 */
export function CrewPerpetualFacts({
  sw,
  testIdPrefix,
  className = '',
}: {
  sw: CrewPerpetualSwitch
  testIdPrefix: string
  /** Host spacing only; the list's own type scale and wrapping are shared. */
  className?: string
}) {
  const { t } = useTranslation()
  if (!sw.canSwitch) return null
  const idleSecs = perpetualIdleSecs(sw.loop)
  return (
    <dl
      id={perpetualFactsId(testIdPrefix)}
      className={`m-0 flex flex-col gap-1.5 text-[11.5px] leading-relaxed ${className}`.trim()}
      data-testid={`${testIdPrefix}-facts`}
    >
      <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
        <dt className="font-medium text-text-strong sm:w-24 sm:flex-none">
          {t('components.crewPerpetualSection.fact_first_wake_label')}
        </dt>
        <dd className="m-0 min-w-0" data-testid={`${testIdPrefix}-fact-first-wake`}>
          {t('components.crewPerpetualSection.fact_first_wake', { interval: intervalText(idleSecs) })}
        </dd>
      </div>
      <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
        <dt className="font-medium text-text-strong sm:w-24 sm:flex-none">
          {t('components.crewPerpetualSection.fact_each_wake_label')}
        </dt>
        <dd className="m-0 min-w-0" data-testid={`${testIdPrefix}-fact-each-wake`}>
          {t('components.crewPerpetualSection.fact_each_wake', { n: fmtNumber(wakesPerDay(idleSecs)) })}
        </dd>
      </div>
      <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
        <dt className="font-medium text-text-strong sm:w-24 sm:flex-none">
          {t('components.crewPerpetualSection.fact_review_chat_label')}
        </dt>
        <dd className="m-0 min-w-0">
          <a
            href={sw.chatHref}
            target="_blank"
            rel="noreferrer"
            className="text-accent underline underline-offset-2 hover:text-accent-hover"
            data-testid={`${testIdPrefix}-review-chat`}
          >
            {t('components.crewPerpetualSection.fact_review_chat')}
          </a>
        </dd>
      </div>
    </dl>
  )
}

/**
 * The control itself: "Saving…" / "Saved" beside a `Toggle` (role=switch).
 * Rendered by a host that already called `useCrewPerpetualSwitch`; returns
 * nothing while the switch cannot be offered. `testIdPrefix` keeps each
 * host's ids its own (`crew-perpetual-*` on the detail page,
 * `member-perpetual-*` in the side panel).
 */
export function CrewPerpetualControl({
  sw,
  testIdPrefix,
  describedBy,
}: {
  sw: CrewPerpetualSwitch
  testIdPrefix: string
  describedBy?: string
}) {
  const { t } = useTranslation()
  if (!sw.canSwitch) return null
  const { state, pending, justSaved, threadClosed } = sw
  return (
    <span
      className="ml-auto flex items-center gap-2"
      data-testid={`${testIdPrefix}-control`}
      data-pending={pending || undefined}
      aria-busy={pending || undefined}
    >
      {(pending || justSaved) && (
        <span className="text-[11px] text-muted" aria-live="polite" data-testid={`${testIdPrefix}-save-state`}>
          {pending ? t('components.jobForm.saving') : t('components.crewPerpetualSection.saved')}
        </span>
      )}
      <span data-testid={`${testIdPrefix}-switch`} data-checked={state === 'on'}>
        <Toggle
          checked={state === 'on'}
          onChange={sw.press}
          disabled={pending || threadClosed}
          label={t('components.crewPerpetualSection.title')}
          describedBy={describedBy}
        />
      </span>
    </span>
  )
}
