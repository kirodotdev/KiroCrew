import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Coins, Info, KeyRound, Loader2 } from 'lucide-react'
import { api, type KasLoginStatus } from '../../api/client'
import { useOpenKiroSignIn } from '../../pages/developer/useOpenKiroSignIn'
import { useKiroSignInOffered } from '../../pages/developer/useKiroSignInOffered'
import { isAccountOnly } from '../../api/kiroUsage'
import type { KiroAccountUsage } from '../../components/KiroAccountModal'
import { fmtCompact } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useKiroAccountReading } from '../../hooks/useKiroAccountReading'
import HoverTip from '../../components/HoverTip'

/** {@link useKiroAccountReading} plus the account modal's open state and the
 *  capsule's "Sign in to Kiro" segment. */
export function useKiroUsageReadout() {
  const [kiroUsageOpen, setKiroUsageOpen] = useState(false)
  const { kiroUsageState, usageSegmentHidden, pillHidden, kiroAccountEntry } = useKiroAccountReading()
  useEffect(() => {
    if (pillHidden) setKiroUsageOpen(false)
  }, [pillHidden])
  // On a harness with no balance here (settled: `pillHidden`), the Kiro sign-in
  // still serves what runs as the Kiro account, so a build that offers it says
  // when none is stored, quietly, in the balance's place. Read from Crew's own
  // sign-in status, never from the usage read: off KAS that read goes through
  // kiro-cli and says nothing about Crew's sign-in. A stored sign-in shows
  // nothing here (no balance is read for it off KAS); a lapsed one is the
  // app-level sign-in notice's to report.
  const signInOffered = useKiroSignInOffered() === true
  const { data: kasLogin } = useQuery<KasLoginStatus>({
    queryKey: ['kas-login'],
    queryFn: api.kasLoginStatus,
    enabled: pillHidden && signInOffered,
  })
  const kiroSignInPrompt = pillHidden && signInOffered && kasLogin?.authenticated === false
  const openSignIn = useOpenKiroSignIn()
  const openKiroSignIn = useCallback(() => openSignIn(), [openSignIn])
  return { kiroUsageOpen, setKiroUsageOpen, kiroUsageState, usageSegmentHidden, kiroAccountEntry, kiroSignInPrompt, openKiroSignIn }
}

/** The capsule's quiet "Sign in to Kiro" segment, shown in the balance's place
 *  while `kiroSignInPrompt` holds. It moves to the Kiro account tab; it signs
 *  nothing in by itself. */
export function kiroSignInSegment(openKiroSignIn: () => void, seg: string, isMobile: boolean): ReactNode {
  const label = i18nT('components.kasLogin.sign_in_to_kiro')
  return (
    <HoverTip key="usage" label={label}><button data-testid="kiro-sign-in-segment" className={`${seg} text-muted`} onClick={openKiroSignIn} aria-label={label}>
      <KeyRound size={12} aria-hidden="true" />
      {!isMobile && <span className="tb-drop-usage text-[11px] whitespace-nowrap">{label}</span>}
    </button></HoverTip>
  )
}

/** The capsule's credits segment for the current reading, or nothing on a harness with no balance. */
export function kiroUsageSegment({ kiroUsageState, usageSegmentHidden, setKiroUsageOpen }: {
  kiroUsageState: KiroAccountUsage
  /**
   * The hook's own "this surface does not exist here" verdict. Passed in rather
   * than re-derived from the harness, so the segment and the modal-close effect
   * can never disagree about whether there is a balance to report.
   */
  usageSegmentHidden: boolean
  setKiroUsageOpen: (open: boolean) => void
}, seg: string, isMobile: boolean): ReactNode {
  let node: ReactNode = null
  // One predicate gates the whole chain rather than being re-stated per
  // branch: the branches below are states of a segment that EXISTS.
  if (usageSegmentHidden) return null
  // Usage segment — Kiro credit plan from Kiro Crew's own usage
  // cache. Spinner while the cache warms, a dash when the fetch
  // failed or when the gateway holds no reading. On a Kiro
  // harness every state keeps the segment on screen: the account
  // modal it opens is where the user refreshes, so a hidden segment
  // would make the one recovery path unreachable.
  if (kiroUsageState === 'none') {
    // No reading: the API returned no plan and the /usage scrape
    // found none either (or is parked). The label says what
    // happened and where to act.
    node = (<HoverTip key="usage" label={i18nT('app.kiro_credit_usage_no_reading')}><button className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('app.kiro_credit_usage_no_reading')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button></HoverTip>)
  } else if (kiroUsageState === 'config-unreadable') {
    // No reading AND the backend setting could not be read: not the
    // hidden non-Kiro case (nothing proved that), a failed read with
    // its retry behind the dash -- the modal re-asks for the config.
    node = (<HoverTip key="usage" label={i18nT('app.kiro_credit_usage_config_unreadable')}><button className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('app.kiro_credit_usage_config_unreadable')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button></HoverTip>)
  }
  if (kiroUsageState !== 'none' && kiroUsageState !== 'config-unreadable') {
    if (isAccountOnly(kiroUsageState)) {
      // Signed in with no balance tied to the user (an external identity
      // provider sign-in). Permanent for that sign-in: the same
      // muted dash the sibling no-value states show, so it reads as "no value
      // here", plus an info mark, the one the modal's notice uses, so it is
      // not mistaken for a failed read. It only arises with kiro-cli, so it
      // never hides with the non-Kiro `none` case.
      node = (<HoverTip key="usage" label={i18nT('app.kiro_credit_usage_sign_in_type')}><button className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('app.kiro_credit_usage_sign_in_type')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span> <Info size={11} aria-hidden="true" data-testid="kiro-usage-sign-in-type-mark" /></button></HoverTip>)
    } else if (kiroUsageState === 'failed') {
      // Failed with nothing cached to fall back on. A dash says that;
      // a spinner would claim a fetch is still in flight. A failure
      // that arrives while a prior value is held keeps that value —
      // the payload's own `stale` flag dims it instead.
      //
      // The dash renders on mobile too, where the reading and the
      // spinner are both dropped: without it the failed and warming
      // states are one coin glyph apart in opacity alone.
      node = (<HoverTip key="usage" label={i18nT('app.kiro_credit_usage_unavailable')}><button className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('app.kiro_credit_usage_unavailable')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button></HoverTip>)
    } else if (kiroUsageState === 'api-key') {
      // The sessions bill a KIRO_API_KEY, not the login kiro-cli holds, so there
      // is no balance to show. A muted dash with the same info mark as the
      // account-only state, so it reads as a configuration and not a failed read;
      // the modal behind it says whose credits are being spent.
      node = (<HoverTip key="usage" label={i18nT('components.kiroAccountModal.credit_usage_api_key')}><button className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('components.kiroAccountModal.credit_usage_api_key')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span> <Info size={11} aria-hidden="true" data-testid="kiro-usage-api-key-mark" /></button></HoverTip>)
    } else if (kiroUsageState === 'signin-required' || kiroUsageState === 'signin-rejected') {
      // No live Kiro credential could be read (or it was rejected), so
      // the free API never got an answer about this account. Terminal,
      // and the label must name the remedy: signing in.
      node = (<HoverTip key="usage" label={i18nT('app.kiro_credit_usage_signin_required')}><button className={`${seg} text-muted opacity-60`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('app.kiro_credit_usage_signin_required')}><Coins size={12} /> <span className="font-mono text-[11px] tabular-nums">—</span></button></HoverTip>)
    } else if (!kiroUsageState) {
      node = (<HoverTip key="usage" label={i18nT('app.kiro_credit_usage_checking')}><button className={`${seg} text-muted`} onClick={() => setKiroUsageOpen(true)} aria-label={i18nT('app.kiro_credit_usage_checking_2')}><Coins size={12} /> {!isMobile && <Loader2 size={11} className="animate-spin" />}</button></HoverTip>)
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
      // Names the account and subscription, not just the balance: this pill
      // is the desktop's way to the plan page behind the modal (#23812).
      const title = i18nT('app.kiro_account_credits')
      node = (<HoverTip key="usage" label={title}><button className={kiroUsageState.stale ? `${seg} opacity-60` : seg} onClick={() => setKiroUsageOpen(true)} aria-label={title}>
        <Coins size={12} /> {!isMobile && <span className="tb-drop-usage font-mono text-[11px] whitespace-nowrap tabular-nums">{usedStr}<span className="text-muted">/{limitStr}</span></span>}
      </button></HoverTip>)
    }
  }
  return node
}
