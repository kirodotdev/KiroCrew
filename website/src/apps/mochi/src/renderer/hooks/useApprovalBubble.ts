/**
 * useApprovalBubble — tells the user, ON THE DESKTOP, that a tool is waiting on
 * them.
 *
 * Why this exists: a pending approval BLOCKS the agent until someone answers it,
 * but the card that answers it lives in the chat panel — which is routinely
 * closed while the pet sits on the desktop. Without this the turn just stalls
 * with nothing on screen to explain why, which reads as the pet having frozen.
 *
 * Why it is its OWN hook rather than a branch inside `useBubble`: that hook is
 * bubble TRANSPORT (text, fade, auto-dismiss timer). Approval is a domain — it
 * decides which event is worth a bubble, what the copy says, and when the bubble
 * stops being true. Folding the two together made `useBubble` need a `petName`
 * argument it used for exactly one message, and hid this behaviour where nobody
 * would look for it. Here the whole policy is one file, reachable by name, and
 * `useBubble` stays reusable: approval simply CALLS `showBubble`.
 */
import { useEffect } from 'react'

import { i18nT } from '../../../../../i18n/t'
import { api } from '../../mochiApi'

/**
 * The bubble copy. Deliberately a FIXED sentence naming only the agent's own
 * declared purpose — never the command or its arguments. A bubble floats on the
 * desktop in front of whoever is looking at the screen (and lands in any screen
 * recording or shared window), so it is the wrong surface for a command line;
 * the panel's approval card is where the exact command belongs.
 *
 * Exported for tests: the copy and the empty-purpose fallback are the whole
 * contract of this module, so they are checked without rendering the pet.
 */
export function approvalBubbleText(petName: string, purpose: string): string {
  const trimmed = purpose.trim()
  return trimmed === ''
    ? i18nT('apps.mochi.approval.bubble_needs_approval_bare', { pet: petName })
    : i18nT('apps.mochi.approval.bubble_needs_approval', { pet: petName, purpose: trimmed })
}

/**
 * Pull the purpose out of either approval shape.
 *
 * `purpose` is what `permissionApprovalFromFrame` extracts from an interactive
 * tool call; `tool_purpose` is the field the gateway's own approval frame carries
 * (Slack and background sources). Reading both means one bubble path covers every
 * source instead of silently going blank for one of them.
 */
export function approvalPurpose(req: unknown): string {
  const r = (req ?? {}) as { purpose?: unknown; tool_purpose?: unknown }
  if (typeof r.purpose === 'string' && r.purpose !== '') return r.purpose
  if (typeof r.tool_purpose === 'string') return r.tool_purpose
  return ''
}

/**
 * @param petName   Name the user gave the pet, so the bubble speaks as the pet.
 * @param showBubble Raise path from {@link useBubble}.
 * @param dismissBubble Dismiss path from {@link useBubble}.
 */
export function useApprovalBubble(
  petName: string,
  showBubble: (text: string, sticky: boolean) => void,
  dismissBubble: () => void,
): void {
  useEffect(() => {
    const offRequest = api?.onApprovalRequest?.((req: unknown) => {
      // Sticky: this is a REQUEST, not a status line. A 6s auto-dismiss would
      // routinely expire before the user looked up, leaving a blocked agent and
      // no trace of why.
      showBubble(approvalBubbleText(petName, approvalPurpose(req)), true)
    })
    // A sticky bubble has no timer, so something has to retract it: the approval
    // can be answered in the panel, the dashboard, or Slack, and once it is, the
    // bubble is stating something false. Without this it would sit on the desktop
    // until clicked.
    const offResolved = api?.onApprovalResolvedExternal?.(() => {
      dismissBubble()
    })
    return () => {
      offRequest?.()
      offResolved?.()
    }
  }, [petName, showBubble, dismissBubble])
}
