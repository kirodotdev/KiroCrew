/**
 * The one verdict on whether the stored Kiro sign-in still works, shared by the
 * sign-in card (Settings > Agent Harness) and the app-level sign-in notice so the
 * two cannot disagree. A pure predicate over the token-free `/api/kas-login`
 * status, kept out of the card's module so the always-mounted notice does not
 * pull the card's component graph (the OIDC chooser) into its chunk.
 */
import type { KasLoginStatus } from '../../api/client'

/**
 * Whether the stored sign-in can still do its job. Two independent verdicts
 * from the gateway, both token-free: `usable` is the spawn-time predicate (an
 * expired access token with nothing to renew it), `refresh_rejected` is the
 * issuer's refusal recorded by the last refresh attempt. Either one means the
 * agents will be told they are not signed in until the user signs in again --
 * and NOTHING falls back to kiro-cli's login on its own, which is why the card
 * and the notice have to say so rather than let a live-looking account sit there.
 */
export function signInLapsed(status: KasLoginStatus): boolean {
  return !status.usable || status.refresh_rejected
}
