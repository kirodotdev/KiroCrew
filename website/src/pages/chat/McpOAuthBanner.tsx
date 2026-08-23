import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Lock, ExternalLink, CheckCircle, XCircle, Loader2 } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { api, ApiError } from '../../api/client'
import { queryClient } from '../../api/queryClient'
import { parseErrorCode } from '../../utils/errorReport'
import { isValidLoopbackReturnAddress } from '../../utils/loopbackReturnAddress'
import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
/**
 * Inline banner for kiro-cli MCP OAuth flow. `meta.completed` flips it to the
 * authenticated state; `meta.failed` flips it to the error state.
 */
function isSafeOAuthUrl(url: string): boolean {
  if (!url) return false
  const lower = url.toLowerCase()
  return lower.startsWith('https://') || lower.startsWith('http://')
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
 */
export function renderMcpOAuthMessage(m: ChatMessage, hideCardOwned = false): ReactNode {
  if (hideCardOwned && m.meta?.card_owned) return null
  const serverName = (m.meta?.server_name as string) || ''
  const oauthUrl = (m.meta?.oauth_url as string) || ''
  const completed = !!m.meta?.completed
  const failed = !!m.meta?.failed
  const error = (m.meta?.error as string) || ''
  if (!oauthUrl && !completed && !failed) return null
  return (
    <McpOAuthBanner
      serverName={serverName}
      oauthUrl={oauthUrl}
      completed={completed}
      failed={failed}
      error={error}
    />
  )
}

export default function McpOAuthBanner({
  serverName,
  oauthUrl,
  completed,
  failed,
  error,
}: {
  serverName: string
  oauthUrl: string
  completed: boolean
  failed?: boolean
  error?: string
}) {
  const label = serverName || i18nT('pages.chat.mcpOAuthBanner.mcp_server')

  if (failed) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 rounded-lg ring-1 ring-inset forced-colors:border ring-danger/40 bg-danger/10 text-sm leading-5">
        <XCircle className="shrink-0 text-danger lucide-inline" />
        <span className="flex-1 text-text">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.authentication_failed')}{error ? `: ${error}` : '.'}
        </span>
      </div>
    )
  }

  if (completed) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 rounded-lg ring-1 ring-inset forced-colors:border ring-ok/40 bg-ok/10 text-sm leading-5">
        <CheckCircle className="shrink-0 text-ok lucide-inline" />
        <span className="flex-1 text-text">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.authenticated')}
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
      <RelayAffordance serverName={serverName} />
    </div>
  )
}

/**
 * The paste-back relay, surfaced where the failure actually presents.
 *
 * On a remote gateway the authorize flow ends by redirecting the user's browser
 * to a `localhost` callback that only exists on the GATEWAY host, so the browser
 * cannot reach it and the tab shows a connection error. The gateway's own
 * loopback listener DID mint the code, though, so pasting the failed callback URL
 * back lets the gateway replay it locally and finish the flow. That affordance
 * previously lived only on the Connections page (issue #4491) — undiscoverable
 * from here, where the banner is the only thing the user sees — and only for
 * curated registry providers, excluding user-added/self-hosted servers. This
 * exposes it inline for every server. It only DELIVERS an already-minted code; it
 * never mints one (parked decision #4286, untouched).
 */
function RelayAffordance({ serverName }: { serverName: string }) {
  const ime = useImeGuard()
  const [open, setOpen] = useState(false)
  const [returnAddress, setReturnAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  // The delivered state waits for the server's `meta.completed` to flip the
  // whole banner. If that never arrives (gateway hiccup, refused exchange with
  // no `failed` emitted), a spinner with no exit is a permanent dead-end — so
  // after a bounded wait re-open the affordance with a directive message.
  useEffect(() => {
    if (!done) return
    const timer = setTimeout(() => {
      setDone(false)
      setOpen(true)
      setError(i18nT('pages.chat.mcpOAuthBanner.delivery_timeout'))
    }, 60_000)
    return () => clearTimeout(timer)
  }, [done])

  if (done) {
    // Neutral delivered state, not "authenticated": the relay only DELIVERS the
    // code — the server finishes the token exchange and flips the whole banner
    // via `meta.completed`, which is the single source of truth for success.
    return (
      <div className="flex items-center gap-2 text-[13px] leading-5 text-text/70">
        <Loader2 className="shrink-0 lucide-inline animate-spin motion-reduce:animate-none" size={14} aria-hidden="true" />
        <span role="status">{i18nT('pages.chat.mcpOAuthBanner.code_delivered')}</span>
      </div>
    )
  }

  const runRelay = async () => {
    const value = returnAddress.trim()
    if (!value || busy) return
    // Same client-side pre-check the Connections card runs: a malformed paste
    // fails locally with the specific shared message instead of a round-trip
    // collapsing into the generic delivery-failure copy.
    if (!isValidLoopbackReturnAddress(value)) {
      setError(i18nT('pages.connectionsPage.invalid_return_address'))
      return
    }
    setBusy(true)
    setError('')
    try {
      await api.mcpOAuthRelay(serverName, value)
      setDone(true)
      // The relay just let the server finish authenticating; the infinitely
      // fresh ['mcp-servers'] cache would otherwise keep showing the pre-auth
      // state on the Connections page until a manual reload.
      void queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
    } catch (e) {
      // Branch on the backend's stable `code`, not the human message. A 409
      // approval_superseded means a newer Authorize invalidated this approval —
      // pasting the same URL again can never succeed, so point at Authorize.
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      setError(
        code === 'approval_superseded'
          ? i18nT('pages.chat.mcpOAuthBanner.relay_superseded')
          : i18nT('pages.chat.mcpOAuthBanner.relay_failed'),
      )
    } finally {
      setBusy(false)
    }
  }

  // The paste-back path only matters after a browser-side callback failure, so
  // it collapses behind a disclosure instead of competing with Authorize. The
  // button stays MOUNTED as a real expander — aria-expanded toggles, the panel
  // can be collapsed again, and focus moves to the input on open rather than
  // dropping to <body> when the DOM swaps.
  return (
    <div className="flex flex-col gap-1.5 pt-1 border-t border-warn/20 mt-1">
      <button
        type="button"
        onClick={() => {
          setOpen(prev => !prev)
          if (!open) setTimeout(() => inputRef.current?.focus(), 0)
        }}
        aria-expanded={open}
        className="self-start text-[12px] leading-4 text-text/70 underline decoration-dotted underline-offset-2 cursor-pointer hover:text-text transition-colors"
      >
        {i18nT('pages.chat.mcpOAuthBanner.relay_disclosure')}
      </button>
      {open && (
        <>
          <p className="text-[12px] leading-4 text-text/70">
            {i18nT('pages.chat.mcpOAuthBanner.remote_gateway_hint')}
          </p>
          <div className="flex items-center gap-2">
            <input
              ref={inputRef}
              type="url"
              autoComplete="off"
              spellCheck={false}
              value={returnAddress}
              onChange={e => setReturnAddress(e.target.value)}
              {...ime.bindEnter({ onEnter: () => void runRelay() })}
              placeholder={i18nT('pages.connectionsPage.return_address_placeholder')}
              aria-label={i18nT('pages.connectionsPage.return_address')}
              disabled={busy}
              className="flex-1 min-w-0 px-2 py-1 rounded-md text-[13px] leading-5 font-mono bg-bg text-text ring-1 ring-inset ring-border focus:outline-none focus:ring-accent"
            />
            <button
              type="button"
              onClick={() => void runRelay()}
              disabled={!returnAddress.trim() || busy}
              className="inline-flex items-center gap-1.5 shrink-0 px-3 py-1 rounded-md text-[13px] leading-5 font-semibold bg-accent text-accent-fg cursor-pointer hover:opacity-90 transition-opacity disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {busy && <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" size={13} aria-hidden="true" />}
              {busy
                ? i18nT('pages.chat.mcpOAuthBanner.relaying')
                : i18nT('pages.chat.mcpOAuthBanner.complete_connection')}
            </button>
          </div>
          {error && (
            <p className="text-[12px] leading-4 text-danger" role="alert">
              {error}
            </p>
          )}
        </>
      )}
    </div>
  )
}
