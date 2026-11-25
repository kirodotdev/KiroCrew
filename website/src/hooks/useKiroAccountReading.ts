import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { parseKiroUsagePayload, kiroUsageRefetchInterval, type KiroUsageState } from '../api/kiroUsage'
import { isKiroHarness, type AcpBackendConfig } from '../api/acpBackend'
import type { KiroAccountUsage } from '../components/KiroAccountModal'
import { failedWithNoData } from '../api/queryState'
import { noteKiroAccount } from '../providers/adapters/acp'

/**
 * The Kiro credit reading one derivation serves: the capsule's credits segment,
 * the account modal it opens, and the Credits section of Settings -> Kiro
 * account. Reads only; the modal's open state and the sign-in prompt are
 * {@link useKiroUsageReadout}'s.
 */
export function useKiroAccountReading() {
  // Kiro CLI monthly credit usage. /api/sessions/usage TRIGGERS the background
  // `kiro-cli /usage` fetch AND returns the cached result, so the pill is
  // self-sufficient on any page. Month-to-date total = credits_used, which the
  // backend already sets to the TRUE total (covered + overage). Do NOT add
  // credits_covered on top — that double-counts the in-plan portion and is the
  // bug that rendered a capped 10K plan as "20.0K". Returns null until the
  // background cache warms.
  //
  // `isError` is read alongside `data` because `data` alone cannot tell "the
  // backend cache has not warmed yet" (null) apart from "the request failed"
  // (undefined) — both are falsy. Without it a failing endpoint renders as a
  // spinner that never resolves, since the 30s refetch keeps retrying forever.
  const queryClient = useQueryClient()
  const { data: kiroUsage, isError: kiroUsageFailed } = useQuery<KiroUsageState>({
    queryKey: ['kiro-usage'],
    // The parser is shared with the account modal's Refresh button, which
    // writes its POST result into this same query: one normalization for both.
    // This poll is also how the browser learns of a `kiro-cli login` to another
    // account: the model list cached for the previous one is dropped and re-read.
    queryFn: () =>
      api.sessionsUsage().then(d => {
        if (noteKiroAccount(d?.account)) void queryClient.resetQueries({ queryKey: ['available-models'] })
        return parseKiroUsagePayload(d)
      }),
    // The GET warms the gateway's cache in the background, so a reopen hits an
    // empty cache (`null`) and, before kiro-cli readiness is verified, a 503 the
    // query surfaces as an error. Poll fast while the state is unsettled so the
    // pill fills in a couple of seconds instead of waiting a full interval for
    // each of those reads; drop to the steady cadence once a reading (or a
    // terminal state) lands.
    refetchInterval: (query) =>
      kiroUsageRefetchInterval(
        query.state.data ?? null,
        query.state.status === 'error',
        query.state.dataUpdateCount + query.state.errorUpdateCount,
      ),
  })
  // ONE derivation feeds both the capsule segment and the account modal, so the
  // drill-in can never report a different state from the pill that opened it —
  // the modal spinning on "checking account" behind a pill that already says
  // "unavailable" is the same falsy-collapse defect one level down.
  // The `none` dash is a Kiro-harness surface: kiro-cli and its KAS relay both
  // run on the Kiro account, so "no reading yet; open to refresh" has a
  // balance behind it. On any other harness the same `available: false` means
  // no Kiro account runs agents here, so there is no balance to read and the
  // segment stays hidden. Read POSITIVELY off the selected backend, never as
  // "not claude": an unloaded config renders no dash.
  const kirocrewCfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const { data: kirocrewCfg, isSuccess: kirocrewCfgLoaded } = kirocrewCfgQuery
  const kiroCreditSurface = isKiroHarness(kirocrewCfg)
  // "The config read failed" must survive its own retry: a data-less errored
  // query goes back to `pending` (error cleared) for the whole refetch, and
  // reading `isError` alone would drop the segment to the hidden non-Kiro shape
  // for that second, then bring it back. The last SETTLED outcome is what
  // counts: no data ever arrived and an error has -- so until data lands, the
  // read is failed, in flight or not.
  const kirocrewCfgFailed = failedWithNoData(kirocrewCfgQuery)
  // `config-unreadable`: the gateway holds no reading AND the config read that
  // decides whether this is a Kiro harness FAILED. Neither "no plan" nor "not
  // a Kiro harness" is established, so the segment must not collapse into the
  // hidden non-Kiro case (that is a verdict; this is a failed read): it renders
  // its own dash, and the modal behind it retries the config read.
  const kiroUsageState: KiroAccountUsage = kiroUsageFailed && !kiroUsage
    ? 'failed'
    : kiroUsage === 'none' && kirocrewCfgFailed
      ? 'config-unreadable'
      : (kiroUsage ?? null)
  // The states with NOTHING TRUE TO SHOW: a usage read that produced no
  // reading, on a harness that does not run on the Kiro account. Whether this
  // surface EXISTS is the HARNESS's verdict; the reading only decides what it
  // shows -- so a read that produced nothing must not resurrect a segment the
  // harness has already ruled out.
  //
  // `failed` belongs here with `none`. On a non-Kiro harness the read fails BY
  // CONSTRUCTION: `GET /api/sessions/usage` is refused with 503
  // `kiro_prerequisite_required` whenever the kiro-cli readiness latch is not
  // verified-ready, which is the STANDING state of an install that runs another
  // harness and never signs kiro-cli in. The gate refuses before the handler can
  // answer `available: false`, so the state is always `failed`, never `none`.
  //
  // `config-unreadable` is deliberately NOT one of these: the config read itself
  // failed, so "not a Kiro harness" was never established.
  //
  // The two no-reading states need DIFFERENT evidence, which is why this is not
  // one `||`. `none` is a payload the gateway sent, so an unloaded config
  // already hides it (`isKiroHarness(undefined)` is false). `failed` is the
  // absence of an answer, so hiding it demands a SETTLED verdict: while the
  // config is unread, a dash the user can open beats silently dropping the only
  // surface that reports the failure.
  const usageSegmentHidden = kiroUsageState === 'none' || kiroUsageState === 'api-key'
    ? !kiroCreditSurface
    : kiroUsageState === 'failed'
      ? kirocrewCfgLoaded && !kiroCreditSurface
      : false
  // The modal only CLOSES on a settled verdict. A modal opened while the cache
  // was warming would otherwise be left over a pill that has just disappeared,
  // with nothing under it to refresh -- but an unloaded config is a pending
  // state, not a verdict, so it hides the dash without closing the modal.
  const pillHidden = kirocrewCfgLoaded && usageSegmentHidden
  // The Credits section of Settings -> Kiro account: the phone's only view of
  // the account and its balance -- the readout capsule that carries the desktop
  // credits segment is not rendered on the phone (docs/narrow-viewport.md). On
  // a Kiro harness it is always there (its Refresh is how an empty reading gets
  // filled). On any other harness it appears only for a reading the desktop
  // segment would show (`pillHidden` is the segment's own derivation) and never
  // for the warming `null` -- the desktop paints a spinner there and takes it
  // back if the cache settles on `none`, which for a section would blink in and out.
  const kiroAccountEntry = kiroCreditSurface || (kiroUsageState !== null && !pillHidden)
  return { kiroUsageState, usageSegmentHidden, pillHidden, kiroAccountEntry }
}

