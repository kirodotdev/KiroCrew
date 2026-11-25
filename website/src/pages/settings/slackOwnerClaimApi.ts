/**
 * Slack owner claim: the direct-local window in which the person sends the bot
 * a DM and confirms themselves as the owner (`/api/slack/owner-claim`).
 *
 * Kept out of the shared `api` object so it loads with the claim block's own
 * chunk; it goes through the same installed transport.
 */

import { apiTransport } from '../../api/apiTransport'

/** One sender offered for confirmation. */
export interface SlackOwnerClaimCandidate {
  user_id: string
  display_name: string
  handle: string
  /** An `https://` avatar URL, or "". */
  avatar: string
  team_name: string
  /** The message text the sender typed, cut to a few hundred characters. */
  text: string
  /** Seconds since the epoch. */
  received_at: number
}

/** GET /api/slack/owner-claim. */
export interface SlackOwnerClaimState {
  open: boolean
  /** Seconds since the epoch; 0 when closed. */
  expires_at: number
  team_name: string
  /** The bot's own Slack name, so the page can say whom to DM; "" when closed. */
  bot_name: string
  /** Newest first. */
  candidates: SlackOwnerClaimCandidate[]
  /** More than one person sent a message: the page warns before Confirm. */
  multiple: boolean
  ignored: number
  /** The owner this gateway process confirmed, if any. */
  confirmed: { user_id: string; handle: string; team_name: string } | null
}

/** One object, so a test can spy on each call. */
export const slackOwnerClaimApi = {
  get: async () => (await apiTransport.j(await apiTransport.get('/api/slack/owner-claim'))) as SlackOwnerClaimState,
  start: async () => (await apiTransport.j(await apiTransport.post('/api/slack/owner-claim/start', {}))) as SlackOwnerClaimState,
  cancel: async () => (await apiTransport.j(await apiTransport.post('/api/slack/owner-claim/cancel', {}))) as SlackOwnerClaimState,
  confirm: async (userId: string) =>
    (await apiTransport.j(await apiTransport.post('/api/slack/owner-claim/confirm', { user_id: userId }))) as {
      owner_id: string
      connected: boolean
      connect_error: string
    },
}
