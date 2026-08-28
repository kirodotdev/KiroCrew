import { useEffect, useRef, useState } from 'react'
import { KeyRound, ExternalLink, Loader2 } from 'lucide-react'

import { api } from '../../api/client'
import { queryClient } from '../../api/queryClient'
import { i18nT } from '../../i18n/t'
import type { McpServer } from '../../types'
import OAuthRelayAffordance, { isSafeOAuthUrl, type RelayStrings } from '../../components/OAuthRelayAffordance'

/** Take a FRESH probe and repaint the table with it, returning whether the
 *  named server was observed signed in. GET /api/mcp replays the last probe's
 *  cached observation, so after a grant lands elsewhere (a concurrent flow, or
 *  the gateway finishing a relayed exchange) invalidating ['mcp-servers'] alone
 *  would keep painting the pre-consent "sign-in required" — the same staleness
 *  ConnectionsPage's granted path solves with an explicit probe. */
async function probeShowsSignedIn(serverName: string): Promise<boolean> {
  try {
    const probed = (await api.mcpProbe()) as McpServer[]
    if (!Array.isArray(probed)) return false
    queryClient.setQueryData<McpServer[]>(['mcp-servers'], probed)
    const row = probed.find(s => s.name === serverName)
    // Only an explicit true is success — an absent observation must not claim it.
    return row?.authGrantPresent === true
  } catch {
    return false
  }
}

/** How long a mint may sit in minting/waiting without producing an approval URL
 *  before the flow gives up and offers a retry. 30 polls at 2s. */
const POLL_INTERVAL_MS = 2_000
const MAX_POLLS = 30

/** After the approval URL arrives we keep polling the SAME mint feed for the
 *  local authorize to complete: a `localhost`-callback sign-in never round-trips
 *  through this component (no relay is pasted), so without this poll the row
 *  sticks on "Authorize {{name}}". 150 polls at 2s ≈ 5 min, then stop silently —
 *  the row's badge is the source of truth and the user can re-probe. Mirrors
 *  ConnectionsPage's post-authorize mint reconciliation. */
const MAX_AUTHORIZE_POLLS = 150

type Phase = 'idle' | 'minting' | 'authorize' | 'error'

/** The relay strings this table reuses from the chat banner's catalog, so the
 *  shared affordance carries one set of copy across both surfaces. */
function relayStrings(): RelayStrings {
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

/**
 * In-place sign-in for a registry-managed OAuth MCP server, shown in the MCP
 * Servers table when a row resolves to a curated Connections provider AND needs
 * a sign-in. It reuses the SAME headless mint machinery the Connections cards
 * use (`connectionsMint` starts a one-server approval flow, `connectionsMintState`
 * feeds the approval URL) plus the shared paste-back relay for remote gateways.
 *
 * It NEVER mints for arbitrary URLs — the caller gates on `connectionProviderForServer`
 * resolving, which is fenced by parked maintainer decision #4286. It also never
 * claims the server is signed in: the row's status badge stays the single source
 * of truth, so on a successful relay this only invalidates ['mcp-servers'] so the
 * next probe repaints the row.
 *
 * Unlike the chat banner, this surface has NO out-of-band completion signal — its
 * only feedback is the row badge — so the relay's terminal dead-ends (a 409
 * `approval_superseded` and the 60s delivery timeout) are routed to THIS
 * component's `error` phase (a real "Try again" control), via `onDeadEnd`,
 * instead of the banner's inline "click X" copy that has no such control here.
 */
export default function McpRowSignIn({ slug, serverName }: { slug: string; serverName: string }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [oauthUrl, setOauthUrl] = useState('')
  const [error, setError] = useState('')
  // Guards a stale poll loop from a superseded attempt writing state after the
  // component started a new mint or unmounted.
  const attemptRef = useRef(0)

  useEffect(() => () => { attemptRef.current += 1 }, [])

  const startSignIn = async () => {
    const attempt = (attemptRef.current += 1)
    setPhase('minting')
    setError('')
    setOauthUrl('')
    try {
      await api.connectionsMint(slug)
    } catch {
      if (attemptRef.current !== attempt) return
      setPhase('error')
      setError(i18nT('pages.overview.mcpTab.sign_in_mint_failed'))
      return
    }
    // Poll the mint feed for the approval URL. The mint activates a one-server
    // spec on the backend; the URL appears once kiro-cli has produced it.
    for (let i = 0; i < MAX_POLLS; i += 1) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS))
      if (attemptRef.current !== attempt) return
      let state
      try {
        state = await api.connectionsMintState(slug)
      } catch {
        continue
      }
      if (attemptRef.current !== attempt) return
      const url = state.oauth_url || ''
      if (url && isSafeOAuthUrl(url)) {
        setOauthUrl(url)
        setPhase('authorize')
        // Keep watching the same feed: a successful LOCAL authorize completes on
        // the gateway with no paste-back, so nothing else would move this off
        // the authorize view. attempt-guarded so a superseding mint cancels it.
        void pollAuthorize(attempt)
        return
      }
      if (state.state === 'granted') {
        // The sign-in completed without this attempt ever producing a URL — a
        // concurrent flow (another tab, a chat session) finished first. Probe
        // so the repainted row shows the grant; without it the loop would run
        // out and report a false timeout on an already-signed-in server.
        await probeShowsSignedIn(serverName)
        if (attemptRef.current !== attempt) return
        setPhase('idle')
        return
      }
      if (state.state === 'failed' || state.state === 'expired') {
        setPhase('error')
        setError(i18nT('pages.overview.mcpTab.sign_in_mint_failed'))
        return
      }
    }
    setPhase('error')
    setError(i18nT('pages.overview.mcpTab.sign_in_timeout'))
  }

  /** After the approval URL is shown, keep polling the mint feed so a completed
   *  local authorize repaints the row instead of leaving it stuck on "Authorize".
   *  `granted` → fresh probe + stop; `failed`/`expired` → error phase; running
   *  out of the cap stops silently (the badge + re-probe remain the fallback). */
  const pollAuthorize = async (attempt: number) => {
    for (let i = 0; i < MAX_AUTHORIZE_POLLS; i += 1) {
      await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS))
      if (attemptRef.current !== attempt) return
      let state
      try {
        state = await api.connectionsMintState(slug)
      } catch {
        continue
      }
      if (attemptRef.current !== attempt) return
      if (state.state === 'granted') {
        await probeShowsSignedIn(serverName)
        if (attemptRef.current !== attempt) return
        setPhase('idle')
        return
      }
      if (state.state === 'failed' || state.state === 'expired') {
        setPhase('error')
        setError(i18nT('pages.overview.mcpTab.sign_in_mint_failed'))
        return
      }
    }
    // Cap reached: stop quietly. The row badge is the source of truth and the
    // user can re-probe — a spurious error here would be worse than silence.
  }

  /** The relay's terminal dead-ends land here: an `error` phase with a real
   *  retry control, unlike the banner's inline "click Authorize" (there is no
   *  such control inside the authorize phase on this surface). */
  const onDeadEnd = (message: string) => {
    setPhase('error')
    setError(message)
  }

  if (phase === 'idle') {
    return (
      <button
        type="button"
        onClick={() => void startSignIn()}
        className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md text-[13px] leading-5 font-semibold bg-accent text-accent-fg cursor-pointer hover:opacity-90 transition-opacity"
      >
        <KeyRound className="lucide-inline" size={13} aria-hidden="true" />
        {i18nT('pages.overview.mcpTab.sign_in')}
      </button>
    )
  }

  if (phase === 'minting') {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12px] text-warn">
        <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" size={13} aria-hidden="true" />
        <span role="status">{i18nT('pages.overview.mcpTab.sign_in_preparing')}</span>
      </span>
    )
  }

  if (phase === 'error') {
    return (
      <div className="flex flex-col gap-1.5">
        <p className="text-[12px] leading-4 text-danger" role="alert">{error}</p>
        <button
          type="button"
          onClick={() => void startSignIn()}
          className="self-start inline-flex items-center gap-1.5 px-3 py-1 rounded-md text-[13px] leading-5 font-semibold bg-accent text-accent-fg cursor-pointer hover:opacity-90 transition-opacity"
        >
          <KeyRound className="lucide-inline" size={13} aria-hidden="true" />
          {i18nT('pages.overview.mcpTab.sign_in_retry')}
        </button>
      </div>
    )
  }

  // phase === 'authorize'
  return (
    <div className="flex flex-col gap-2">
      <a
        href={oauthUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center justify-center gap-2 self-start px-3 py-1 rounded-md text-[13px] leading-5 font-semibold bg-accent text-accent-fg cursor-pointer hover:opacity-90 transition-opacity no-underline"
      >
        {i18nT('pages.overview.mcpTab.sign_in_authorize', { name: serverName })}
        <ExternalLink className="lucide-inline" size={13} aria-hidden="true" />
      </a>
      <OAuthRelayAffordance serverName={serverName} strings={relayStrings()} onDeadEnd={onDeadEnd} />
      {/* The row's badge is the source of truth for success, not this component:
          after the relay delivers the code (or a local authorize completes) the
          next probe repaints the row. Shown from the authorize phase onward so it
          only appears once there is a pending sign-in to reconcile. */}
      <span className="text-muted text-[12px]">
        {i18nT('pages.overview.mcpTab.sign_in_row_updates_after_probe')}
      </span>
    </div>
  )
}
