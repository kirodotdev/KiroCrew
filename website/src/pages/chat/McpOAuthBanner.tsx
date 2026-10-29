import { useState, type ReactNode } from 'react'
import { Lock, ExternalLink, CheckCircle, History } from 'lucide-react'
import type { ChatMessage } from '../../types'

import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import OAuthRelayAffordance, { isSafeOAuthUrl } from '../../components/OAuthRelayAffordance'

/**
 * Inline banner for kiro-cli MCP OAuth flow. `meta.completed` flips it to the
 * authenticated state; `meta.failed` flips it to the error state. A card whose
 * flow was replaced (`meta.superseded`) or whose sign-in attempt ended
 * (`meta.expired_reason === 'attempt_ended'`) renders nothing: its link can no
 * longer be redeemed, so the transcript keeps at most the one live card per server.
 */

/** The chat banner's relay strings — its own `pages.chat.mcpOAuthBanner.*` keys.
 *  The MCP-table sign-in reuses these same keys rather than duplicating them. */
function bannerRelayStrings() {
  return {
    disclosure: i18nT('pages.chat.mcpOAuthBanner.relay_disclosure'),
    remoteGatewayHint: i18nT('pages.chat.mcpOAuthBanner.remote_gateway_hint'),
    completeConnection: i18nT('pages.chat.mcpOAuthBanner.complete_connection'),
    relaying: i18nT('pages.chat.mcpOAuthBanner.relaying'),
    codeDelivered: i18nT('pages.chat.mcpOAuthBanner.code_delivered'),
    deliveryTimeout: i18nT('pages.chat.mcpOAuthBanner.delivery_timeout'),
    relayFailed: i18nT('pages.chat.mcpOAuthBanner.relay_failed'),
    relaySuperseded: i18nT('pages.chat.mcpOAuthBanner.relay_superseded'),
  }
}

/** Render an mcp_oauth message into a banner, or null if there's nothing to show.
 *
 * `hideCardOwned` drops requests the backend tagged `card_owned` — a Connections
 * card owns that consent flow and shows the same Authorize action, so repeating
 * it in chat is a duplicate prompt that re-fires on every session init. Callers
 * pass it only when the card is actually reachable (`connections_ui` on); the
 * default renders everything, which is what every surface without a card does.
 * The message itself is always delivered either way — the card reads its approval
 * URL out of it.
 *
 * `handOff` is the list's hand-off switch (`ctx.handOff`): `false` drops the
 * "Ask the agent" button from the failed state, for a host whose unsaved local
 * state the hand-off's navigation would drop. Default `true`.
 */
export function renderMcpOAuthMessage(m: ChatMessage, hideCardOwned = false, handOff = true): ReactNode {
  if (hideCardOwned && m.meta?.card_owned) return null
  const serverName = (m.meta?.server_name as string) || ''
  const oauthUrl = (m.meta?.oauth_url as string) || ''
  const completed = !!m.meta?.completed
  const failed = !!m.meta?.failed
  const expired = !!m.meta?.expired
  const error = (m.meta?.error as string) || ''
  // A replaced card or an ended attempt is not shown at all. Replaced: a newer
  // card for the same server is the one to use. Ended: the next offer mints a
  // fresh card. A late success still shows, because the backend flips that row
  // to `completed`, and a terminal outcome wins over both checks here.
  if (!completed && !failed) {
    if (m.meta?.superseded) return null
    if (expired && m.meta?.expired_reason === 'attempt_ended') return null
  }
  // `expired` (the hosting process is gone) carries no `oauth_url` (the backend
  // pops it), so it is named here or the banner would vanish instead of telling
  // the user to send a message for a fresh link: nothing else will offer one.
  if (!oauthUrl && !completed && !failed && !expired) return null
  return (
    <McpOAuthBanner
      serverName={serverName}
      oauthUrl={oauthUrl}
      completed={completed}
      failed={failed}
      expired={expired}
      error={error}
      askAgent={handOff}
    />
  )
}

export default function McpOAuthBanner({
  serverName,
  oauthUrl,
  completed,
  failed,
  expired,
  error,
  askAgent = true,
}: {
  serverName: string
  oauthUrl: string
  completed: boolean
  failed?: boolean
  expired?: boolean
  error?: string
  /** Offer the "Ask the agent" hand-off on the failed state. */
  askAgent?: boolean
}) {
  const label = serverName || i18nT('pages.chat.mcpOAuthBanner.mcp_server')
  // Probe-confirmed grant when `meta.completed` never arrived (gateway hiccup):
  // the exchange succeeded, so render the same authenticated state that the
  // meta update would have produced instead of stranding on the relay spinner.
  const [confirmedSignedIn, setConfirmedSignedIn] = useState(false)

  if (failed) {
    // A failed OAuth exchange is a backend error the agent can diagnose, so it
    // takes the shared notice with the hand-off on. On the chat page the hand-off
    // opens a fresh slot without navigating away, so nothing is at risk there.
    // A host holding state the hand-off's navigation would drop (ChatEmbed,
    // ChatPane) turns it off through `askAgent`. The server name is the bold
    // lead; the sentence stays the journal key.
    return (
      <ErrorNotice
        title={label}
        message={`${i18nT('pages.chat.mcpOAuthBanner.authentication_failed')}${error ? `: ${error}` : '.'}`}
        askAgent={askAgent}
      />
    )
  }

  if (completed || confirmedSignedIn) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 rounded-lg ring-1 ring-inset forced-colors:border ring-ok/40 bg-ok/10 text-sm leading-5">
        <CheckCircle className="shrink-0 text-ok lucide-inline" />
        <span className="flex-1 text-text">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.authenticated')}
        </span>
      </div>
    )
  }

  // The child process hosting this flow is gone (a gateway restart or a session
  // reset), so the link cannot be redeemed by anyone. A still-pending request is
  // re-announced on the next session init, so the honest instruction is to send a
  // message and take the fresh link that appears. Rendered rather than hidden:
  // nothing else offers a way back in (issue #7654).
  if (expired) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 rounded-lg ring-1 ring-inset forced-colors:border ring-border bg-muted/10 text-sm leading-5">
        <History className="shrink-0 text-text/50 lucide-inline" />
        <span className="flex-1 text-text">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.expired')}
        </span>
      </div>
    )
  }

  // Defense-in-depth: backend already validates, but never render a non-http(s) URL on <a href>.
  const safeUrl = isSafeOAuthUrl(oauthUrl) ? oauthUrl : ''
  if (!safeUrl) return null

  return (
    <div className="flex flex-col gap-2 px-4 py-3 rounded-lg ring-1 ring-inset forced-colors:border ring-warn/40 bg-warn/10 text-sm leading-5">
      <div className="flex items-center gap-2">
        <Lock className="shrink-0 text-warn lucide-inline" />
        <span className="flex-1 text-text min-w-0 break-words">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.requires_authentication')}
        </span>
      </div>
      <a
        href={safeUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center justify-center gap-2 self-start px-4 py-2 rounded-md text-[13px] leading-5 font-semibold bg-accent text-accent-fg cursor-pointer hover:opacity-90 transition-opacity no-underline"
      >
        {i18nT('pages.chat.mcpOAuthBanner.authorize')} {label} <ExternalLink className="lucide-inline" size={13} />
      </a>
      <RelayAffordance serverName={serverName} onConfirmedSignedIn={() => setConfirmedSignedIn(true)} />
    </div>
  )
}

/** The banner's relay affordance: the shared component wired to the banner's own
 *  strings, with NO onDeadEnd — the whole banner flips on `meta.completed`, so the
 *  inline directive message is the right terminal state here. `onConfirmedSignedIn`
 *  covers the one gap that leaves: an exchange that SUCCEEDED while the
 *  `meta.completed` update never arrived — the bounded-wait probe observes the
 *  grant and the banner flips to its authenticated state anyway. */
function RelayAffordance({ serverName, onConfirmedSignedIn }: { serverName: string; onConfirmedSignedIn: () => void }) {
  return (
    <OAuthRelayAffordance
      serverName={serverName}
      strings={bannerRelayStrings()}
      onConfirmedSignedIn={onConfirmedSignedIn}
    />
  )
}

