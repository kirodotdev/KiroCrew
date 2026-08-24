import type { ChatMessage } from '../types'
import { parseRecoveryMessage } from '../pages/chat/RecoveryCard'

/**
 * True when the turn containing `index` had a tool call blocked by policy.
 *
 * Used to drop the generic "Steered" chip from that turn's reply. The chip is
 * correct for a steer the PERSON sent and wrong here: the same mechanism carries
 * a system policy notice, so the chip reads as though the user had steered the
 * turn — the exact misattribution the notice exists to correct. The blocked-tool
 * card already states what happened, so the chip is not just ambiguous but
 * redundant.
 *
 * Classifies the row through `parseRecoveryMessage` rather than re-declaring the
 * marker literal. That literal lives in exactly one production place — the
 * `PREFIXES` table in `RecoveryCard.tsx` — which is the only copy
 * `test_recovery_card_prefixes.py`'s cross-language drift guard reads. A second
 * copy here would sit outside that guard, so a change to the Python constant
 * would keep the card rendering (guarded) while silently breaking this
 * suppression (unguarded).
 *
 * Scanning backwards to the turn head means the answer is identical while the
 * reply is still streaming and after a history reload: the blocked row and the
 * notice row are both appended at deny time, which is before any of the reply's
 * text arrives. A `meta` flag on the assistant row could not do this — the
 * streaming row has no persisted meta yet, so the chip would appear live and
 * vanish on refresh.
 *
 * A turn that ALSO carries a steer the person sent keeps its chip: their steer
 * deserves its acknowledgement, and suppressing on the presence of a policy
 * notice alone would silently swallow it.
 */
export function turnHadPolicyBlock(messages: ChatMessage[], index: number): boolean {
  let blocked = false
  for (let i = Math.min(index, messages.length - 1); i >= 0; i--) {
    const m = messages[i]
    if (!m) continue
    // Turn head: a real user message. A steer is also role `user`, so it must
    // NOT end the scan — it lives INSIDE the turn it was injected into.
    if (m.role === 'user') {
      if ((m.meta as Record<string, unknown> | undefined)?.steer) return false
      break
    }
    if (m.role === 'inject' && parseRecoveryMessage(m.content ?? '')?.kind === 'tool_blocked') {
      blocked = true
    }
  }
  return blocked
}
