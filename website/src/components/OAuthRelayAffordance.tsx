import { useEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'

import { api, ApiError } from '../api/client'
import { queryClient } from '../api/queryClient'
import type { McpServer } from '../types'
import { parseErrorCode } from '../utils/errorReport'
import { isValidLoopbackReturnAddress, normalizeLoopbackReturnAddress } from '../utils/loopbackReturnAddress'
import { i18nT } from '../i18n/t'
import { useImeGuard } from '../hooks/useImeGuard'

/** Defense-in-depth: never render a non-http(s) URL on an <a href>. The backend
 *  already gates on this; the render layer refuses too. Exported so the sole
 *  copy lives here and both the chat banner and the MCP-table sign-in share it. */
export function isSafeOAuthUrl(url: string): boolean {
  if (!url) return false
  const lower = url.toLowerCase()
  return lower.startsWith('https://') || lower.startsWith('http://')
}

/** Take a FRESH probe and repaint the ['mcp-servers'] cache with it, returning
 *  whether the named server was observed signed in. GET /api/mcp replays the last
 *  probe's cached observation, so after a grant lands (a concurrent flow, or the
 *  gateway finishing a relayed exchange) invalidating the cache alone would keep
 *  painting the pre-consent "sign-in required" — the same staleness the
 *  Connections granted path solves with an explicit probe. */
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

/**
 * The strings a host surface supplies. Each host passes its OWN i18n keys so the
 * shared markup carries no copy of its own — the chat banner passes
 * `pages.chat.mcpOAuthBanner.*` and the MCP table passes the same keys (it reuses
 * them rather than duplicating), and the placement-specific wording stays with
 * the host.
 */
export interface RelayStrings {
  /** Disclosure toggle: "Browser showed a connection error after authorizing?" */
  disclosure: string
  /** Hint paragraph explaining the remote-gateway localhost-callback failure. */
  remoteGatewayHint: string
  /** Idle submit button: "Complete connection". */
  completeConnection: string
  /** Busy submit button: "Completing…". */
  relaying: string
  /** Neutral delivered status (NOT "signed in"). */
  codeDelivered: string
  /** 60s dead-end message when meta.completed / the grant never arrives. */
  deliveryTimeout: string
  /** Generic relay failure (bad pasted URL) — retryable in place. */
  relayFailed: string
  /** 409 approval_superseded — re-pasting can never succeed. */
  relaySuperseded: string
}

/**
 * The paste-back relay, surfaced where the OAuth failure actually presents.
 *
 * On a remote gateway the authorize flow ends by redirecting the browser to a
 * `localhost` callback that only exists on the GATEWAY host, so the browser
 * cannot reach it and the tab shows a connection error. The gateway's own
 * loopback listener DID mint the code, so pasting the failed callback URL back
 * lets the gateway replay it locally and finish the flow. It only DELIVERS an
 * already-minted code; it never mints one (parked decision #4286, untouched).
 *
 * `onDeadEnd` lets a host that has NO out-of-band completion signal (the MCP
 * table, whose only feedback is the row badge) route the two terminal
 * dead-ends — a 409 `approval_superseded` and the 60s delivery timeout — to a
 * surface with a real "start over" control, instead of the inline "click X"
 * copy that has no such control in place. The chat banner omits it: its whole
 * banner flips on `meta.completed`, so the inline message is fine there.
 */
export default function OAuthRelayAffordance({
  serverName,
  strings,
  onDeadEnd,
  onConfirmedSignedIn,
  invalidateQueryKey = ['mcp-servers'],
}: {
  serverName: string
  strings: RelayStrings
  onDeadEnd?: (message: string) => void
  /** Called when the bounded-wait probe OBSERVES the grant. A host whose normal
   *  completion signal is out-of-band (the chat banner's `meta.completed`) uses
   *  this to reach its own terminal success state when that signal never
   *  arrives — otherwise a successful exchange would strand the host on the
   *  neutral "code delivered" spinner with no exit. Hosts repainted by the
   *  probe itself (the MCP table row) do not need it. */
  onConfirmedSignedIn?: () => void
  invalidateQueryKey?: readonly unknown[]
}) {
  const ime = useImeGuard()
  const [open, setOpen] = useState(false)
  const [returnAddress, setReturnAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  // Latest onDeadEnd without retriggering the delivery-timeout effect when the
  // host passes a fresh closure each render.
  const onDeadEndRef = useRef(onDeadEnd)
  useEffect(() => { onDeadEndRef.current = onDeadEnd }, [onDeadEnd])
  const onConfirmedSignedInRef = useRef(onConfirmedSignedIn)
  useEffect(() => { onConfirmedSignedInRef.current = onConfirmedSignedIn }, [onConfirmedSignedIn])

  // The delivered state waits for the host to observe success (the banner's
  // `meta.completed`, or the table's next probe). If that never arrives (gateway
  // hiccup, refused exchange with no failure emitted), a spinner with no exit is
  // a dead-end — so after a bounded wait first take a FRESH probe: the relay only
  // DELIVERS the code and the gateway finishes the exchange afterwards, while
  // GET /api/mcp replays the cached pre-consent observation, so a successful
  // exchange looks "incomplete" to the cache. A probe that observes the grant
  // repaints the row (unmounting this component) instead of raising a false
  // error. Only a probe that STILL sees no grant surfaces the directive: inline
  // when the host has no better place, or via onDeadEnd when it does.
  useEffect(() => {
    if (!done) return
    let cancelled = false
    const timer = setTimeout(() => {
      void (async () => {
        const signedIn = await probeShowsSignedIn(serverName)
        if (cancelled) return
        if (signedIn) {
          // The table row is repainted by the probe's own cache write; a host
          // with an out-of-band completion signal that never arrived still
          // needs telling, or it strands on the delivered spinner (GPT review
          // finding on this PR).
          onConfirmedSignedInRef.current?.()
          return
        }
        if (onDeadEndRef.current) {
          onDeadEndRef.current(strings.deliveryTimeout)
          return
        }
        setDone(false)
        setOpen(true)
        setError(strings.deliveryTimeout)
      })()
    }, 60_000)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [done, serverName, strings.deliveryTimeout])

  if (done) {
    // Neutral delivered state, NOT "signed in": the relay only delivers the code.
    // The server finishes the token exchange; the row's badge (repainted after
    // the next probe via the invalidated cache) is the source of truth.
    return (
      <div className="flex items-center gap-2 text-[12px] leading-4 text-text/70">
        <Loader2 className="shrink-0 lucide-inline animate-spin motion-reduce:animate-none" size={13} aria-hidden="true" />
        <span role="status">{strings.codeDelivered}</span>
      </div>
    )
  }

  const runRelay = async () => {
    // Normalize a scheme-less mobile paste to http:// first (#7406), then run
    // the same client-side pre-check the Connections card runs: a malformed
    // paste fails locally with the specific shared message instead of a
    // round-trip collapsing into the generic delivery-failure copy. The
    // NORMALIZED value is what gets submitted, so older gateways without the
    // backend normalization accept it too.
    const value = normalizeLoopbackReturnAddress(returnAddress)
    if (!value || busy) return
    if (!isValidLoopbackReturnAddress(value)) {
      setError(i18nT('pages.connectionsPage.invalid_return_address'))
      return
    }
    setBusy(true)
    setError('')
    try {
      await api.mcpOAuthRelay(serverName, value)
      // MODULE-LEVEL SINGLETON invalidate — never useQueryClient() here (a
      // widely-rendered component that reaches for the hook needs a provider at
      // every render site and breaks CI shards). The row badge stays the source
      // of truth; the next probe repaints it.
      void queryClient.invalidateQueries({ queryKey: invalidateQueryKey })
      setDone(true)
    } catch (e) {
      // Branch on the backend's stable `code`, not the human message. A 409
      // approval_superseded means a newer Authorize invalidated this approval, so
      // re-pasting can never succeed — a terminal dead-end. Route it out of the
      // affordance (onDeadEnd) when the host has a start-over surface; otherwise
      // show it inline pointing at Authorize.
      const code = e instanceof ApiError ? parseErrorCode(e.body) : undefined
      if (code === 'approval_superseded') {
        if (onDeadEndRef.current) {
          onDeadEndRef.current(strings.relaySuperseded)
        } else {
          setError(strings.relaySuperseded)
        }
      } else {
        // A bad pasted URL is retryable in place — stay inline regardless.
        setError(strings.relayFailed)
      }
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
        {strings.disclosure}
      </button>
      {open && (
        <>
          <p className="text-[12px] leading-4 text-text/70">
            {strings.remoteGatewayHint}
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
              {busy ? strings.relaying : strings.completeConnection}
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
