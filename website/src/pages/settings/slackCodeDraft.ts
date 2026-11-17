/**
 * The Slack Code slice of the Slack settings draft: its shape, how it is read
 * from GET /api/slack/config and what of it goes into the PUT. Shared by the
 * Slack page (eager) and the code-channel defaults card (its own chunk).
 */
import type { SlackCodeSwitchKey, SlackConfigData, SlackConfigSave } from '../../api/client'
import { INHERIT_MODEL } from '../../components/crew/crewFields'
import { SLACK_CODE_SWITCHES } from './slackCodeSwitches'

/** The Slack Code slice of the Slack settings draft. '' = not set. */
export interface SlackCodeDraft {
  /** Every registered switch, as the draft has it. */
  switches: Record<SlackCodeSwitchKey, boolean>
  /** `slack.code_channel_dispatch`, the sub-toggle under Code channels. Not a
   *  registered switch: it carries no manifest scope. */
  dispatch: boolean
  /** As read: turning Code channels on also turns `dispatch` on (nothing stored
   *  yet). Never edited; the card shows the sub-toggle on with that switch. */
  dispatchOnEnable: boolean
  /** As read: config.local.json sets (or may set) `dispatch`, so the sub-toggle
   *  is locked and its value is never sent. Never edited. */
  dispatchLocked: boolean
  folder: string
  agent: string
  /** INHERIT_MODEL ('auto') = keep the agent's own model. */
  model: string
  effort: string
  /** One of APPROVAL_MODES; 'ask' (the default) is stored as unset. */
  approval: string
}

/** The approval modes a code channel's session can start in, in picker order.
 *  The labels are the session approval picker's own, so the mode reads the same
 *  here and on the session where it can be changed later. */
export const APPROVAL_MODES = ['ask', 'trust_reads', 'trust'] as const

function approvalFromWire(raw: string | null | undefined): string {
  return raw === 'trust_reads' || raw === 'trust' ? raw : 'ask'
}

export function slackCodeDraftFrom(c: SlackConfigData): SlackCodeDraft {
  const d = c.code_channel_defaults
  // The stored folder may be a sidebar path (a hand-edited config) while the
  // picker matches ids only: seed it with the id the server resolved it to. A
  // reference that did not resolve stays as stored, for its warning.
  const resolvedId = d?.folder && !(c.code_channel_warnings ?? []).includes('folder_missing')
    ? c.code_channel_effective?.folder?.id
    : ''
  return {
    switches: Object.fromEntries(SLACK_CODE_SWITCHES.map(k => [k, !!c[k]])) as Record<SlackCodeSwitchKey, boolean>,
    dispatch: c.code_channel_dispatch === true,
    dispatchOnEnable: c.code_channel_dispatch_on_enable === true,
    dispatchLocked: c.code_channel_dispatch_overlay === true,
    folder: resolvedId || (d?.folder ?? ''),
    agent: d?.agent ?? '',
    model: d?.model ?? INHERIT_MODEL,
    effort: d?.reasoning_effort ?? '',
    approval: approvalFromWire(d?.approval_mode),
  }
}

/** The PUT fields for the Slack Code slice: only what differs from *saved*, so a
 *  save of an unrelated Slack field never re-validates a default (a stored model
 *  that has since become unavailable must not block a token change). A cleared
 *  picker sends null, which clears the stored value. */
export function slackCodePayload(d: SlackCodeDraft, saved: SlackCodeDraft): Partial<SlackConfigSave> {
  const out: Partial<SlackConfigSave> = {}
  for (const k of SLACK_CODE_SWITCHES) {
    if (d.switches[k] !== saved.switches[k]) out[k] = d.switches[k]
  }
  // On a save that turns Code channels on, the sub-toggle's shown value is always
  // sent (unless config.local.json owns it, where the toggle is locked): the
  // server would otherwise decide helpers from the files as they are now, which
  // a card read earlier may not show, and an off chosen here is then stored as
  // an explicit off.
  const enabling = d.switches.code_channels && !saved.switches.code_channels && !saved.dispatchLocked
  if (d.dispatch !== saved.dispatch || enabling) out.code_channel_dispatch = d.dispatch
  const wire = (x: SlackCodeDraft) => ({
    folder: x.folder || null,
    agent: x.agent || null,
    model: x.model && x.model !== INHERIT_MODEL ? x.model : null,
    reasoning_effort: x.effort || null,
    approval_mode: x.approval === 'ask' ? null : x.approval,
  })
  const next = wire(d)
  const prev = wire(saved)
  const changed = (Object.keys(next) as (keyof typeof next)[]).filter(k => next[k] !== prev[k])
  if (changed.length) {
    out.code_channel_defaults = Object.fromEntries(changed.map(k => [k, next[k]]))
  }
  return out
}
