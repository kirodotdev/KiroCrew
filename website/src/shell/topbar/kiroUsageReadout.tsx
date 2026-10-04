import { useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Coins, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import { parseKiroUsagePayload, type KiroUsageState } from '../../api/kiroUsage'
import { isKiroBackend, type AcpBackendConfig } from '../../api/acpBackend'
import type { KiroAccountUsage } from '../../components/KiroAccountModal'
import { fmtCompact } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/**
 * The Kiro credit reading one derivation serves: the capsule's credits segment,
 * the account modal it opens, and the phone drawers' Kiro Account entry.
 */
export function useKiroUsageReadout() {
  const [kiroUsageOpen, setKiroUsageOpen] = useState(false)
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
  const { data: kiroUsage, isError: kiroUsageFailed } = useQuery<KiroUsageState>({
    queryKey: ['kiro-usage'],
    // The parser is shared with the account modal's Refresh button, which
    // writes its POST result into this same query: one normalization for both.
    queryFn: () => api.sessionsUsage().then(parseKiroUsagePayload),
    refetchInterval: 30_000,
  })
  // ONE derivation feeds both the capsule segment and the account modal, so the
  // drill-in can never report a different state from the pill that opened it —
  // the modal spinning on "checking account" behind a pill that already says
  // "unavailable" is the same falsy-collapse defect one level down.
  // The `none` dash is a Kiro-backend surface: it says "kiro-cli holds no
  // reading yet; open to refresh", and that refresh is a kiro-cli read. On any
  // other harness the same `available: false` means kiro-cli is not what runs
  // agents here, so there is no balance to read and the segment stays hidden.
  // Read POSITIVELY off the selected backend (the gateway's `is_kiro_backend`),
  // never as "not claude": an unloaded config renders no dash.
  const kirocrewCfgQuery = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const { data: kirocrewCfg, isSuccess: kirocrewCfgLoaded } = kirocrewCfgQuery
  const refetchKirocrewCfg = kirocrewCfgQuery.refetch
  const kiroCreditSurface = isKiroBackend(kirocrewCfg)
  // "The config read failed" must survive its own retry: a data-less errored
  // query goes back to `pending` (error cleared) for the whole refetch, and
  // reading `isError` alone would drop the segment to the hidden non-Kiro shape
  // for that second, then bring it back. The last SETTLED outcome is what
  // counts: no data ever arrived and an error has -- so until data lands, the
  // read is failed, in flight or not.
  const kirocrewCfgFailed = kirocrewCfg === undefined && kirocrewCfgQuery.errorUpdatedAt > 0
  // `config-unreadable`: the gateway holds no reading AND the config read that
  // decides whether this is the Kiro backend FAILED. Neither "no plan" nor "not
  // the kiro harness" is established, so the segment must not collapse into the
  // hidden non-Kiro case (that is a verdict; this is a failed read): it renders
  // its own dash, and the modal behind it retries the config read.
  const kiroUsageState: KiroAccountUsage = kiroUsageFailed && !kiroUsage
    ? 'failed'
    : kiroUsage === 'none' && kirocrewCfgFailed
      ? 'config-unreadable'
      : (kiroUsage ?? null)
  // The states with NOTHING TRUE TO SHOW: a usage read that produced no
  // reading, on a harness that does not resolve to kiro-cli. Whether this
  // surface EXISTS is the HARNESS's verdict; the reading only decides what it
  // shows -- so a read that produced nothing must not resurrect a segment the
  // harness has already ruled out.
  //
  // `failed` belongs here with `none`, and leaving it out was the defect. On a
  // non-kiro harness the read fails BY CONSTRUCTION: `GET /api/sessions/usage`
  // is refused with 503 `kiro_prerequisite_required` whenever the kiro-cli
  // readiness latch is not verified-ready, and that is the STANDING state of an
  // install that deliberately runs another harness and never signs kiro-cli in.
  // Because the refusal comes from the gate, the handler never gets to answer
  // `available: false`, so the state can only ever be `failed` -- it can never
  // become the `none` the old rule hid. The result was a credits segment whose
  // modal said "Could not read your balance" about a balance this harness does
  // not have.
  //
  // `config-unreadable` is deliberately NOT one of these: it means the config
  // read itself failed, so "not the kiro harness" was never established, and a
  // failed read must not be shown as a verdict.
  //
  // The two no-reading states need DIFFERENT evidence, which is why this is not
  // one `||`. `none` is a payload the gateway sent, so an unloaded config
  // already hides it (nothing Kiro-only renders on a guess, and
  // `isKiroBackend(undefined)` is false). `failed` is the absence of an answer,
  // so hiding it demands a SETTLED verdict: while the config is unread, "not the
  // kiro harness" is not established, and a dash the user can open is better
  // than silently dropping the only surface that reports the failure.
  const usageSegmentHidden = kiroUsageState === 'none'
    ? !kiroCreditSurface
    : kiroUsageState === 'failed'
      ? kirocrewCfgLoaded && !kiroCreditSurface
      : false
  // The modal only CLOSES on a settled verdict. A modal opened while the cache
  // was warming would otherwise be left over a pill that has just disappeared,
  // with nothing under it to refresh -- but an unloaded config is a pending
  // state, not a verdict, so it hides the dash without closing the modal.
  const pillHidden = kirocrewCfgLoaded && usageSegmentHidden
  useEffect(() => {
    if (pillHidden) setKiroUsageOpen(false)
  }, [pillHidden])
  // Phone drawers: the Kiro Account row (nav drawer) and tile (the chat
  // drawer's rail) are the phone's only way to the account modal -- the readout
  // capsule that carries the desktop credits segment is not rendered on the
  // phone (docs/narrow-viewport.md). On the Kiro backend it is always there
  // (the modal's Refresh is how an empty reading gets filled). On any other
  // harness it appears only for a reading the desktop segment would show
  // (`pillHidden` is the segment's own derivation) and never for the warming
  // `null` -- the desktop paints a spinner there and takes it back if the cache
  // settles on `none`, which for a nav row would be a row blinking in and out.
  const kiroAccountEntry = kiroCreditSurface || (kiroUsageState !== null && !pillHidden)
  return { kiroUsageOpen, setKiroUsageOpen, kiroUsageState, kiroCreditSurface, usageSegmentHidden, kiroAccountEntry, refetchKirocrewCfg }
}

/** The capsule's credits segment for the current reading, or nothing on a harness with no balance. */
export function kiroUsageSegment({ kiroUsageState, usageSegmentHidden, setKiroUsageOpen }: {
  kiroUsageState: KiroAccountUsage
  /**
   * The hook's own "this surface does not exist here" verdict. Passed in rather
   * than re-derived from `kiroCreditSurface`, so the segment and the modal-close
   * effect can never disagree about whether there is a balance to report.
   */
  usageSegmentHidden: boolean
  setKiroUsageOpen: (open: boolean) => void
}, seg: string, isMobile: boolean): ReactNode {
  let node: ReactNode = null
  // A read that produced NO reading on a harness that is not kiro-cli has
  // nothing to refresh and nothing true to say, so the segment is dropped
  // entirely. That one predicate gates the whole chain rather than being
  // re-stated per branch: the branches below are states of a segment that
  // EXISTS, and mixing "does this surface exist" into them is what left
  // `failed` rendering on a harness with no Kiro balance.
  if (usageSegmentHidden) return null
  // Usage segment — Kiro credit plan from Kiro Crew's own usage
  // cache. Spinner while the cache warms, a dash when the fetch
  // failed or when the gateway holds no reading. On the Kiro
  // backend every state keeps the segment on screen: the account
  // modal it opens is where the user refreshes, so a hidden segment
  // would make the one recovery path unreachable.
  if (kiroUsageState === 'none') {
    // No reading: the API returned no plan and the /usage scrape
    // found none either (or is parked). The label says what
    // happened and where to act.
    node = (<button key="usage" className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_no_reading')} aria-label={i18nT('app.kiro_credit_usage_no_reading')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button>)
  } else if (kiroUsageState === 'config-unreadable') {
    // No reading AND the backend setting could not be read: not the
    // hidden non-Kiro case (nothing proved that), a failed read with
    // its retry behind the dash -- the modal re-asks for the config.
    node = (<button key="usage" className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_config_unreadable')} aria-label={i18nT('app.kiro_credit_usage_config_unreadable')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button>)
  }
  if (kiroUsageState !== 'none' && kiroUsageState !== 'config-unreadable') {
    if (kiroUsageState === 'failed') {
      // Failed with nothing cached to fall back on. A dash says that;
      // a spinner would claim a fetch is still in flight. A failure
      // that arrives while a prior value is held keeps that value —
      // the payload's own `stale` flag dims it instead.
      //
      // The dash renders on mobile too, where the reading and the
      // spinner are both dropped: without it the failed and warming
      // states are one coin glyph apart in opacity alone.
      node = (<button key="usage" className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_unavailable')} aria-label={i18nT('app.kiro_credit_usage_unavailable')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button>)
    } else if (kiroUsageState === 'api-key') {
      // API-key auth: the usage API needs an SSO/OIDC token this
      // account type never has, so this is a PERMANENT state, not a
      // failure. Same terminal dash as 'failed' (nothing is in
      // flight), but the label says why, and clicking through opens
      // the modal's fuller explanation.
      node = (<button key="usage" className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_api_key')} aria-label={i18nT('app.kiro_credit_usage_api_key')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button>)
    } else if (kiroUsageState === 'signin-required') {
      // No live Kiro credential could be read (or it was rejected), so
      // the free API never got an answer about this account. Terminal
      // like 'api-key', but the label must name the remedy: signing in
      // again.
      node = (<button key="usage" className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_signin_required')} aria-label={i18nT('app.kiro_credit_usage_signin_required')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button>)
    } else if (!kiroUsageState) {
      node = (<button key="usage" className={`${seg} text-muted`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_checking')} aria-label={i18nT('app.kiro_credit_usage_checking_2')}><Coins size={12} /> {!isMobile && <Loader2 size={11} className="animate-spin" />}</button>)
    } else {
      // Pool every bonus grant into the compact readout. Bonus is
      // drawn down before the plan, so excluding it looks like a
      // frozen counter while promotional credits are active.
      const bonusUsed = kiroUsageState.bonusCredits.reduce((sum, grant) => sum + grant.used, 0)
      const bonusLimit = kiroUsageState.bonusCredits.reduce((sum, grant) => sum + grant.total, 0)
      const totalUsed = kiroUsageState.used + bonusUsed
      const totalLimit = kiroUsageState.limit + bonusLimit
      const usedStr = fmtCompact(totalUsed)
      const limitStr = fmtCompact(totalLimit)
      const title = i18nT('components.kiroAccountModal.kiro_credit_usage')
      node = (<button key="usage" className={kiroUsageState.stale ? `${seg} opacity-60` : seg} onClick={() => setKiroUsageOpen(true)} title={title} aria-label={title}>
        <Coins size={12} /> {!isMobile && <span className="tb-drop-usage font-mono text-[11px] whitespace-nowrap tabular-nums">{usedStr}<span className="text-muted">/{limitStr}</span></span>}
      </button>)
    }
  }
  return node
}
