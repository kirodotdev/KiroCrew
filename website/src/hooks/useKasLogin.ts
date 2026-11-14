import { useCallback, useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  api,
  type KasLoginDeviceSession,
  type KasLoginLoopbackSession,
  type KasLoginStatus,
} from '../api/client'
import { ApiError, apiErrorCode } from '../api/apiError'
import { clearCachedModels } from '../providers/adapters/acp'
import type { KasLoginProvider } from '../components/kasProviderLabel'
import { i18nT } from '../i18n/t'

/**
 * The one cache entry every Kiro sign-in surface reads the stored sign-in
 * under: the flow, the Settings card, the app-level notice and the chat's
 * sign-in prompt. A sign-in, sign-out or switch landing in one re-reads it for
 * all of them.
 */
export const KAS_LOGIN_STATUS_KEY = ['kas-login'] as const

// Device-flow poll cadence. The auth service does not return a per-provider
// interval, so we poll at the flow's default (kiro-cli uses 5s).
const DEVICE_POLL_INTERVAL_MS = 5_000
// Loopback poll cadence: the listener is in-process on this machine, so a tight
// poll is free and the app opens within a couple of seconds of the redirect.
const LOOPBACK_POLL_INTERVAL_MS = 2_000
// How often the flow re-reads the stored sign-in while it is mounted.
const STATUS_POLL_INTERVAL_MS = 30_000

// Extra begin-device fields the `idc` provider needs (the company's IAM Identity
// Center access-portal URL, plus an optional region); other providers send none.
export type KasLoginExtra = { start_url?: string; region?: string }

// `extra` keeps Company SSO's start URL and region, so Start over can begin the
// same sign-in again without the form.
export type ActiveSession = { provider: KasLoginProvider; extra?: KasLoginExtra } & (
  | ({ kind: 'device' } & KasLoginDeviceSession)
  | ({ kind: 'loopback' } & KasLoginLoopbackSession)
)

/** True when this page is itself served from a loopback address — the only case
 *  in which a portal redirect to `http://localhost:<port>` reaches the gateway. */
function browserOnLoopback(): boolean {
  const h = window.location.hostname
  return h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h === '::1'
}

/** The begin-loopback 409: "take the device flow", not an error to display.
 *  Reads the structured error (status + JSON body `code`); the message text is
 *  only a fallback for a transport that lost the body. */
export function isLoopbackUnavailable(err: unknown): boolean {
  if (err instanceof ApiError) {
    if (err.status === 409) return true
    try {
      const parsed = JSON.parse(err.body || '{}') as { code?: unknown }
      if (parsed.code === 'loopback_unavailable') return true
    } catch {
      /* non-JSON body: fall through to the message check */
    }
  }
  return /loopback_unavailable/.test(String((err as { message?: string })?.message ?? err))
}

/** Open the portal in a new tab. Popup blockers may refuse; the wait screen
 *  keeps an explicit "open again" button for exactly that case. */
export function openAuthTab(url: string): void {
  try {
    window.open(url, '_blank', 'noopener,noreferrer')
  } catch {
    /* blocked or headless: the wait screen's button is the fallback */
  }
}

/**
 * The stored Kiro sign-in, read under the shared key. `poll` re-reads it on
 * that interval; leave it off to observe what another surface reads.
 */
export function useKasLoginStatus({ enabled = true, poll }: { enabled?: boolean; poll?: number } = {}) {
  return useQuery({
    queryKey: KAS_LOGIN_STATUS_KEY,
    queryFn: api.kasLoginStatus,
    enabled,
    ...(poll ? { refetchInterval: poll } : {}),
  })
}

/**
 * The Kiro sign-in flow's state and actions, with no rendering: read the
 * stored sign-in, start a browser (loopback) or device-code sign-in, poll it,
 * fall back from loopback to the device code, cancel it, and land the result.
 * Every sign-in surface renders from this one hook, so the flow's state
 * machine has exactly one copy.
 */
export function useKasLoginFlow() {
  const queryClient = useQueryClient()
  // The sign-in in flight, tagged by transport. `loopback` means the gateway is
  // listening on a local port for the portal's redirect; `device` means the user
  // confirms a code. Null whenever the chooser owns the screen.
  const [session, setSession] = useState<ActiveSession | null>(null)
  // Set when a loopback attempt degraded to the device flow, so the code screen
  // can say why it appeared instead of the promised no-typing sign-in.
  const [fellBack, setFellBack] = useState(false)
  // True while the user is replacing a signed-in identity from the embedded
  // card: the chooser renders over a still-authenticated status, and the next
  // credential to land supersedes the stored one. Cleared when the poll answers
  // authorized (the status re-read then shows the new account) or on cancel.
  const [reauth, setReauth] = useState(false)
  // Set when a switch's new credential landed but the previous slot could not
  // be removed (`previous_identity_not_removed`): the old account still takes
  // precedence, and the signed-in view must say so until the user acts.
  const [replaceWarning, setReplaceWarning] = useState(false)
  // True from the moment the user abandons a login until the gateway has
  // acknowledged the cancel and the status has been re-read. The waiting screen
  // stays up with its transition buttons disabled, so no second login can be
  // started while the first is still being unwound.
  const [settling, setSettling] = useState(false)
  // A cancel (or the status read after it) that failed used to leave the
  // waiting screen up with no explanation — buttons re-enabled, nothing said.
  // Held here and rendered on that screen; cleared by the next attempt.
  const [settleError, setSettleError] = useState('')
  // The login a signed-in status dropped whose cancel did not settle: the
  // gateway keeps it open, so the signed-in view says so and offers a retry.
  const [unsettledLoginId, setUnsettledLoginId] = useState<string | null>(null)
  const statusQuery = useKasLoginStatus({ poll: STATUS_POLL_INTERVAL_MS })

  // While replacing a signed-in identity, every begin -- first attempt or the
  // loopback→device fallback -- names the slot being replaced. The gateway
  // removes that slot only after the NEW credential has landed, so the switch
  // is real (the store resolves by slot priority, not recency: a Google sign-in
  // stored beside a still-present Identity Center entry would leave the agents
  // on the old account) and a failed or abandoned attempt leaves it untouched.
  const replaces =
    reauth && statusQuery.data?.authenticated && statusQuery.data.identity
      ? statusQuery.data.identity
      : undefined

  const beginDevice = useMutation({
    mutationFn: ({ provider, extra }: { provider: KasLoginProvider; extra?: KasLoginExtra }) =>
      api.kasLoginBeginDevice(provider, replaces ? { ...(extra ?? {}), replaces } : extra),
    onSuccess: (s, { provider, extra }) => setSession({ kind: 'device', provider, extra, ...s }),
  })

  const beginLoopback = useMutation({
    mutationFn: (provider: KasLoginProvider) =>
      replaces
        ? api.kasLoginBeginLoopback(provider, { replaces })
        : api.kasLoginBeginLoopback(provider),
    onSuccess: (s, provider) => {
      setSession({ kind: 'loopback', provider, ...s })
      openAuthTab(s.auth_url)
    },
    // 409 loopback_unavailable (busy ports, or the gateway decided against it) is
    // not a failure the user can act on: silently take the device path instead.
    onError: (err, provider) => {
      if (isLoopbackUnavailable(err)) {
        setFellBack(true)
        beginDevice.mutate({ provider })
      }
    },
  })

  const pollQuery = useQuery({
    queryKey: ['kas-login-poll', session?.login_id],
    // `session!` is safe: `enabled` gates this off until a session exists.
    queryFn: () => api.kasLoginPoll(session!.login_id),
    // Off while a cancel is settling: the id is about to become unknown, and an
    // error answer here must not read as a loopback failure to degrade from.
    enabled: !!session && !settling,
    // Poll at the server-requested cadence and STOP on any terminal answer —
    // an authorized/expired login has nothing left to poll, and a transport
    // failure should surface as the problem screen rather than retry forever.
    refetchInterval: (query) => {
      if (query.state.status === 'error') return false
      const s = query.state.data?.status
      if (s && s !== 'pending') return false
      // The device flow polls at the auth service's expected cadence (kiro-cli
      // uses 5s); the loopback listener is local, so a tighter poll costs nothing
      // and shortens the gap between the redirect landing and the app opening.
      return session?.kind === 'loopback' ? LOOPBACK_POLL_INTERVAL_MS : DEVICE_POLL_INTERVAL_MS
    },
    retry: false,
  })

  // Success is observed, not returned: the poll answering 'authorized' means
  // the gateway now holds a token, so re-read status (the single authority on
  // `authenticated`) and drop the session.
  // The model list belongs to the account: on the KAS bundle route the gateway
  // forgets the previous account's catalog the moment a sign-in lands, and
  // `['available-models']` is refetched only on a session spawn (global
  // `staleTime: Infinity`), so without this the picker keeps showing the
  // previous account's models and prices. Drop localStorage first, then reset,
  // the same pair the Agent Harness switch uses. Called from BOTH places a
  // sign-in is confirmed: the poll answering 'authorized', and a cancel whose
  // fresh status read finds the credential already stored (the poll is detached
  // by then and never reports 'authorized' for it).
  const forgetModelList = useCallback(() => {
    clearCachedModels()
    void queryClient.resetQueries({ queryKey: ['available-models'] })
  }, [queryClient])

  const authorized = pollQuery.data?.status === 'authorized'
  const authorizedCode = pollQuery.data?.code
  useEffect(() => {
    if (!authorized) return
    void queryClient.invalidateQueries({ queryKey: KAS_LOGIN_STATUS_KEY })
    // The gateway's "would a new chat need a sign-in" answer rides on the
    // engine status, so the chat's sign-in prompt re-reads it too.
    void queryClient.invalidateQueries({ queryKey: ['kas-bundle'] })
    forgetModelList()
    // A switch whose old slot could not be removed is a sign-in that landed AND
    // a failure the user has to act on: the previous account still wins by slot
    // priority, so the re-read status will keep showing it. Kept as a notice
    // over the signed-in view rather than dropped with the session, and cleared
    // only by the user or by the next sign-in attempt.
    setReplaceWarning(authorizedCode === 'previous_identity_not_removed')
    setSession(null)
    setFellBack(false)
    setReauth(false)
  }, [authorized, authorizedCode, forgetModelList, queryClient])

  // Loopback degradation: a listener nobody reached (timeout — the browser is
  // not on this machine) or that failed before persisting a token restarts the
  // SAME provider on the device flow. Only a persist failure is a real dead end.
  const pollCode = pollQuery.data?.code
  const pollStatus = pollQuery.data?.status
  useEffect(() => {
    if (session?.kind !== 'loopback' || settling) return
    if (pollStatus !== 'expired' && pollStatus !== 'error') return
    if (pollCode === 'token_store_failed') return
    const provider = session.provider
    setFellBack(true)
    setSession(null)
    beginDevice.mutate({ provider })
    // beginDevice is a stable mutation handle; listing it would re-fire on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, pollStatus, pollCode, settling])

  const status = statusQuery.data

  // Abandon the in-flight login and learn whether a credential landed anyway.
  // The gateway serialises cancel against the poll's persist step, so once
  // cancel SUCCEEDS the outcome is settled: either nothing was written, or the
  // token is already stored. Deciding the next screen before that answer would
  // leave an abandoned credential active behind a second sign-in.
  //   'signed_in'  -- a token landed; the refreshed status renders the app.
  //   'signed_out' -- nothing landed; safe to start another login.
  //   'unknown'    -- cancel or the status read failed; the old login may still
  //                  complete, so the caller must NOT start another one.
  const settleCancel = async (loginId: string): Promise<'signed_in' | 'signed_out' | 'unknown'> => {
    // While replacing a signed-in identity the old credential is still stored
    // after the cancel, so only a CHANGED one means this login landed. Read
    // right before the cancel, not from the cache: a token refresh since the
    // cached read moves expires_at too.
    let before: KasLoginStatus | undefined
    try {
      if (reauth) before = await api.kasLoginStatus()
      await api.kasLoginCancel(loginId)
    } catch {
      return 'unknown'
    }
    // An independent request, not fetchQuery: fetchQuery joins a status refetch
    // already in flight, and one that left before the token landed answers
    // "signed out" for a login that just succeeded. The fresh answer is pushed
    // into the cache so the gate re-renders on it.
    try {
      const fresh = await api.kasLoginStatus()
      queryClient.setQueryData(KAS_LOGIN_STATUS_KEY, fresh)
      const landed =
        fresh.authenticated &&
        !(before && fresh.identity === before.identity && fresh.expires_at === before.expires_at)
      // A sign-in that landed under the cancel is still a sign-in: the model
      // list is the new account's now, and the poll that would have dropped the
      // old one is already detached.
      if (landed) forgetModelList()
      return landed ? 'signed_in' : 'signed_out'
    } catch {
      return 'unknown'
    }
  }

  const reset = async () => {
    const loginId = session?.login_id
    beginDevice.reset()
    beginLoopback.reset()
    if (!loginId) {
      setSession(null)
      setFellBack(false)
      return
    }
    // The waiting screen stays up, buttons disabled, until the gateway has
    // answered. A confirmed outcome clears the session; an unknown one (cancel
    // or the status read failed) keeps it and lets the poll resume, because the
    // old login may still complete and the chooser must not offer a second one.
    setSettling(true)
    setSettleError('')
    const outcome = await settleCancel(loginId)
    setSettling(false)
    if (outcome === 'unknown') { setSettleError(i18nT('components.kasLogin.cancel_unsettled')); return }
    setSession(null)
    setFellBack(false)
  }

  // Abandon the attempt on screen, then hand it to `next` to start another for
  // the same provider.
  const replaceAttempt = async (next: (prev: ActiveSession) => void) => {
    if (!session) return
    const prev = session
    setSettling(true)
    setSettleError('')
    const outcome = await settleCancel(prev.login_id)
    setSettling(false)
    // Unknown: the cancel never settled, so the old login is still live -- stay
    // on its screen (a waiting screen resumes polling) rather than racing it.
    if (outcome === 'unknown') { setSettleError(i18nT('components.kasLogin.cancel_unsettled')); return }
    setSession(null)
    // Signed in after all (the portal redirect landed while the user reached
    // for the button): the refreshed status renders the app, or the card's
    // signed-in view when this was a switch. Only nothing landing starts
    // another login.
    if (outcome === 'signed_in') setReauth(false)
    else next(prev)
  }

  const switchToCode = () =>
    replaceAttempt(({ provider }) => {
      setFellBack(true)
      beginDevice.mutate({ provider })
    })

  // Start over: a fresh attempt with the same provider on the same transport,
  // one click. Switching method is the chooser's job (`reset`).
  const startOver = () =>
    replaceAttempt((prev) =>
      prev.kind === 'loopback'
        ? beginLoopback.mutate(prev.provider)
        : beginDevice.mutate({ provider: prev.provider, extra: prev.extra }),
    )

  // Signed in another way (another tab, the CLI) while this login was pending:
  // the signed-in view hides it, and it would poll unseen until its code
  // expires. Drop it, then cancel it the way a user's cancel does. The session
  // is never restored, so an unsettled cancel does not resume the poll.
  const cancelDropped = async (loginId: string) => {
    setUnsettledLoginId(null)
    if ((await settleCancel(loginId)) === 'unknown') setUnsettledLoginId(loginId)
  }
  useEffect(() => {
    if (!status?.authenticated || reauth || !session || settling) return
    setSession(null)
    setFellBack(false)
    void cancelDropped(session.login_id)
    // cancelDropped is re-created every render; listing it would re-fire on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.authenticated, reauth, session, settling])

  // Loopback only when BOTH sides agree the browser shares this machine: the
  // gateway's install-shape verdict, and the page itself being served from a
  // loopback address. A dashboard reached over a tunnel or tailnet fails the
  // second test, so the portal's redirect to localhost would land on the wrong
  // machine — the device flow is the right transport there regardless of shape.
  const loopbackOk = status?.transport === 'loopback' && browserOnLoopback()

  const beginBusy = beginDevice.isPending || beginLoopback.isPending

  /** Start a sign-in with *provider*, on the transport this page and gateway allow. */
  const pick = (provider: KasLoginProvider, extra?: KasLoginExtra) => {
    beginDevice.reset()
    beginLoopback.reset()
    setFellBack(false)
    setReplaceWarning(false)
    // Only the two portal-brokered social providers have a loopback flow;
    // Builder ID / IdC run the SSO-OIDC device flow on every shape (as in kiro-cli).
    if (loopbackOk && (provider === 'google' || provider === 'github')) {
      beginLoopback.mutate(provider)
      return
    }
    beginDevice.mutate({ provider, extra })
  }

  /** Back out of replacing a signed-in identity; the stored one was never touched. */
  const cancelReauth = () => {
    beginDevice.reset()
    beginLoopback.reset()
    setReauth(false)
  }

  return {
    statusQuery,
    status,
    session,
    fellBack,
    reauth,
    startReauth: () => setReauth(true),
    cancelReauth,
    replaceWarning,
    dismissReplaceWarning: () => setReplaceWarning(false),
    settling,
    settleError,
    unsettledLoginId,
    retryDroppedCancel: (loginId: string) => void cancelDropped(loginId),
    dismissUnsettled: () => setUnsettledLoginId(null),
    pollQuery,
    beginBusy,
    beginError:
      beginDevice.error?.message ??
      (beginLoopback.error && !isLoopbackUnavailable(beginLoopback.error)
        ? beginLoopback.error.message
        : ''),
    beginErrorCode: apiErrorCode(beginDevice.error),
    pick,
    reset,
    switchToCode,
    startOver,
  }
}

export type KasLoginFlowState = ReturnType<typeof useKasLoginFlow>
