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
 * A refusal is rendered by the HOST (through `refusalText`) so it can sit
 * where that surface puts its notices; coded answers first (the server's
 * `code`): the thread must be open once before the switch can address it; a
 * structured monitor is not this switch's loop; a gateway with auto-nudge off
 * has no loop to arm. Anything else shows the server's own sentence.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { parseErrorCode } from '../../utils/errorReport'
import { errMessage } from '../../utils/thunkError'
import { Toggle } from '../ui'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../autoNudgeLoop'
import { useCrewPerpetual, type CrewPerpetualOptions, type CrewPerpetualReading } from './useCrewPerpetual'

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
  const pending = mutation.isPending
  // Keyed on the mutation's submit time so a second press restarts the window.
  const [savedFor, setSavedFor] = useState<number | null>(null)
  useEffect(() => {
    if (!mutation.isSuccess) return
    setSavedFor(mutation.submittedAt)
    const timer = setTimeout(() => setSavedFor(null), SAVED_MS)
    return () => clearTimeout(timer)
  }, [mutation.isSuccess, mutation.submittedAt])
  const justSaved = !pending && savedFor !== null && savedFor === mutation.submittedAt
  const refusal = mutation.isError ? mutation.error : null
  const refusalText = refusal
    ? (() => {
        const code = parseErrorCode((refusal as { body?: string }).body)
        const key = code ? REFUSAL_KEY[code] : undefined
        return key ? t(key) : errMessage(refusal)
      })()
    : ''

  // The switch is offered once both reads answered and the crew has a roster
  // row to address: a switch over an unknown state would promise a change it
  // cannot describe. A gateway with no nudge service (`enabled: false`) cannot
  // run the mode at all: the switch is withheld and the host says so.
  const canSwitch = loaded && !failed && !missing && !!slug && enabled
  const threadClosed = canSwitch && !slotKey
  const press = (enabled: boolean) => {
    if (!pending && !threadClosed) mutation.mutate(enabled)
  }
  return { ...reading, pending, justSaved, refusalText, canSwitch, threadClosed, press }
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
