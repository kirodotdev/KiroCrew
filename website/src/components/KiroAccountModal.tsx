import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, Coins, ExternalLink, Eye, EyeOff, Gift, Loader2, RefreshCw, UserRound } from 'lucide-react'

import { api } from '../api/client'
import type { KiroBonusCreditGrant, KiroCreditUsage, KiroUsageRefreshResponse } from '../api/client'
import { parseKiroUsagePayload } from '../api/kiroUsage'
import { fmtCurrency, fmtDateFields, fmtNumber, fmtPercent, fmtTime } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import Clickable from './Clickable'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'

// The usage view model is owned by `api/client.ts` next to the wire payload it
// is normalized from, so this panel and the credits pill cannot drift apart.
export type { KiroBonusCreditGrant, KiroCreditUsage }

/**
 * What the modal can be handed: a reading, `null` while the gateway's usage
 * cache warms, `'none'` when the gateway holds no reading (no plan from the API
 * and none from the scrape; the pill shows this state only on the Kiro backend,
 * where the Refresh below can fill it), `'failed'` when
 * the fetch itself failed with nothing cached, `'api-key'` when the account
 * authenticates with an API key (usage needs an SSO/OIDC token that auth type
 * never has, so the state is terminal by construction), `'signin-required'`
 * when no live Kiro credential could be read, or `'config-unreadable'` when the
 * gateway holds no reading AND the config read that decides whether this is the
 * Kiro backend at all failed -- so nothing can be shown as a fact, and the read
 * is retried from here. `null` is the ONLY value that means "still loading" —
 * the others have nothing more to wait for, so spinning on them would repeat
 * the defect this distinction exists to remove.
 */
export type KiroAccountUsage =
  | KiroCreditUsage
  | null
  | 'none'
  | 'failed'
  | 'api-key'
  | 'signin-required'
  | 'config-unreadable'

/** True only for an actual reading, so the sentinels cannot reach a field access. */
const isUsageReading = (usage: KiroAccountUsage): usage is KiroCreditUsage =>
  typeof usage === 'object' && usage !== null

/**
 * The no-reading states a refresh can fill. `'api-key'` is excluded because that
 * auth type has no credit readout at all, and `'signin-required'` takes the
 * sign-in error surface before this is consulted: the `/usage` read needs the
 * same sign-in. `null` is still loading, so there is nothing to refresh yet.
 */
const canRefreshUsage = (usage: KiroAccountUsage): boolean =>
  usage === 'failed' || usage === 'none' || usage === 'config-unreadable'

/**
 * Why a refresh did not produce a new reading, mapped to one plain sentence
 * each. Every one of them is surfaced to the user through the shared error
 * surface with its agent hand-off -- `in_flight` (the gateway refused the press
 * because its own refresh is running; the re-read below brings that result) and
 * `stale` (the refresh ran and returned the earlier reading) included.
 */
type UsageRefreshNotice =
  | { kind: 'in_flight' }
  | { kind: 'stale' }
  | { kind: 'parked'; minutes: number }
  | { kind: 'failed' }

/**
 * Classify a refused refresh from the status the gateway sends. Duck-typed on
 * `status` rather than `instanceof ApiError`, the same way `isNotFoundError`
 * is, so a suite that mocks `api/client` still reaches every branch.
 */
function classifyRefreshError(err: unknown): UsageRefreshNotice {
  const e = err as { status?: unknown } | null
  if (typeof e === 'object' && e !== null && e.status === 409) return { kind: 'in_flight' }
  return { kind: 'failed' }
}

function refreshNoticeMessage(notice: UsageRefreshNotice): string {
  switch (notice.kind) {
    case 'in_flight':
      return i18nT('components.kiroAccountModal.refresh_in_flight')
    case 'parked':
      return i18nT('components.kiroAccountModal.refresh_paused', { minutes: fmtNumber(notice.minutes) })
    case 'stale':
      return i18nT('components.kiroAccountModal.refresh_returned_stale')
    default:
      // The same sentence as the standing failure notice this replaces: a
      // refresh that failed leaves the user exactly where "could not read your
      // balance" already had them, so a second wording would carry nothing.
      return i18nT('components.kiroAccountModal.credit_usage_unavailable')
  }
}

/**
 * How long after a 409 to re-read the pill's query. The gateway's own refresh
 * is a whoami + `/usage` subprocess pair that usually settles within a few
 * seconds; one re-read then shows its result without waiting for the pill's
 * 30 s poll. If it is still running, that poll picks the result up later.
 */
const IN_FLIGHT_RECHECK_MS = 3000

interface KiroAccountModalProps {
  open: boolean
  onClose: () => void
  usage: KiroAccountUsage
}

const KIRO_ACCOUNT_URL = 'https://app.kiro.dev/settings/account'
const KIRO_ACCOUNT_EMAIL_HIDDEN_KEY = 'kirocrew:account-email-hidden'

function formatResetDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return value
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  if (Number.isNaN(date.getTime())) return value
  return fmtDateFields(date, {
    month: 'short',
    day: 'numeric',
    year: date.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
  })
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between items-baseline gap-4 py-2 border-b border-border last:border-b-0">
      <span className="text-[12px] text-muted">{label}</span>
      <span className="text-[13px] font-medium text-text text-right">{value}</span>
    </div>
  )
}

function formatCredits(value: number): string {
  return fmtNumber(value, { maximumFractionDigits: 2 })
}

function emailInitials(email: string): string {
  const localPart = email.split('@', 1)[0] ?? ''
  const parts = localPart.split(/[^A-Za-z0-9]+/).filter(Boolean)
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase()
  return (parts[0] ?? '?').slice(0, 2).toUpperCase()
}

function humanizeProviderId(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .trim()
}

function accountProviderLabel(accountType?: string, startUrl?: string): string {
  const rawType = accountType?.trim()
  if (rawType?.startsWith('Social')) {
    const socialProvider = humanizeProviderId(rawType.slice('Social'.length))
    return socialProvider || i18nT('app.social_login')
  }

  const accountKind = rawType === 'IamIdentityCenter'
    ? i18nT('app.iam_identity_center')
    : rawType === 'BuilderId'
      ? i18nT('app.builder_id')
      : rawType
        ? humanizeProviderId(rawType)
        : undefined

  let issuerHost: string | undefined
  if (startUrl) {
    try { issuerHost = new URL(startUrl).host } catch { issuerHost = undefined }
  }
  return [accountKind, issuerHost].filter(Boolean).join(' · ')
}

function BonusCredits({ grants }: { grants: KiroBonusCreditGrant[] }) {
  if (grants.length === 0) return null

  return (
    <section className="rounded-lg border border-border" aria-labelledby="kiro-bonus-credits-title">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2.5 text-[12px] font-medium text-text">
        <Gift className="lucide-inline text-accent" />
        <span id="kiro-bonus-credits-title">{i18nT('app.bonus_credits')}</span>
      </div>
      <div className="divide-y divide-border px-3">
        {grants.map((grant, index) => {
          const remaining = Math.max(grant.total - grant.used, 0)
          return (
            <div key={`${grant.name}-${index}`} className="py-2.5">
              <div className="flex items-baseline justify-between gap-4">
                <span className="min-w-0 truncate text-[12px] font-medium text-text" title={grant.name}>
                  {grant.name}
                </span>
                <span className="shrink-0 text-[12px] font-semibold text-text">
                  {i18nT('components.kiroAccountModal.remaining_credit_balance', {
                    count: formatCredits(remaining),
                  })}
                </span>
              </div>
              <div className="mt-1 flex items-center justify-between gap-4 text-[11px] text-muted">
                <span>
                  {i18nT('components.kiroAccountModal.used_credits', {
                    used: formatCredits(grant.used),
                    total: formatCredits(grant.total),
                  })}
                </span>
                {grant.daysLeft != null && (
                  <span>
                    {i18nT('components.kiroAccountModal.days_until_expiration', {
                      count: fmtNumber(grant.daysLeft),
                    })}
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function AccountIdentity({ usage }: { usage: KiroAccountUsage }) {
  const [emailHidden, setEmailHidden] = useState(
    () => safeGetItem(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY) !== '0',
  )

  const toggleEmailVisibility = () => {
    setEmailHidden(hidden => {
      const next = !hidden
      safeSetItem(KIRO_ACCOUNT_EMAIL_HIDDEN_KEY, next ? '1' : '0')
      return next
    })
  }

  const email = isUsageReading(usage) ? usage.email : undefined
  const account = isUsageReading(usage) ? usage.account : undefined
  const identity = email || account
  const provider = isUsageReading(usage)
    ? accountProviderLabel(usage.accountType, usage.startUrl)
    : ''

  return (
    <div className="flex flex-col items-center px-4 py-3 text-center">
      <div className="relative flex h-14 w-14 shrink-0 items-center justify-center rounded-full border border-accent/30 bg-accent/10 text-accent shadow-sm">
        {identity ? (
          <span aria-hidden="true" className="text-[17px] font-semibold tracking-[0.04em]">
            {emailInitials(identity)}
          </span>
        ) : (
          <UserRound className="h-6 w-6" strokeWidth={1.7} />
        )}
      </div>
      <div className="mt-3 min-w-0 max-w-full">
        {usage === null ? (
          <div className="flex items-center justify-center gap-2 text-[13px] text-muted">
            <Loader2 className="lucide-inline animate-spin" /> {i18nT('components.kiroAccountModal.checking_account')}
          </div>
        ) : !isUsageReading(usage) || !identity ? (
          <div className="flex items-center justify-center gap-2 text-[13px] text-muted">
            <AlertCircle className="lucide-inline" /> {i18nT('components.kiroAccountModal.account_details_unavailable')}
          </div>
        ) : (
          <>
            <div className="flex min-w-0 items-center justify-center gap-2">
              <span
                className={`min-w-0 truncate text-[16px] font-semibold leading-6 text-text-strong transition-[filter,opacity] duration-150 ${email && emailHidden ? 'select-none blur-[5px] opacity-60' : ''}`}
                title={email && emailHidden ? undefined : identity}
              >
                {identity}
              </span>
              {email && (
                <Clickable
                  onClick={toggleEmailVisibility}
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border text-muted transition-colors hover:border-accent/35 hover:bg-accent/10 hover:text-accent focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-accent"
                  aria-label={i18nT(emailHidden ? 'components.kiroAccountModal.show_email' : 'components.kiroAccountModal.hide_email')}
                  title={i18nT(emailHidden ? 'components.kiroAccountModal.show_email' : 'components.kiroAccountModal.hide_email')}
                >
                  {emailHidden ? <Eye className="lucide-inline" /> : <EyeOff className="lucide-inline" />}
                </Clickable>
              )}
            </div>
            {provider && (
              <div className="mt-2.5 inline-flex items-center rounded-full border border-border bg-bg px-2.5 py-1 text-[12px] text-muted">
                {i18nT('app.signed_in_with', { provider })}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function UsageSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-label={i18nT('components.kiroAccountModal.checking_credit_usage')}>
      <div className="skeleton h-7 w-48 rounded-md" />
      <div className="skeleton h-2 w-full rounded-full" />
      <div className="skeleton h-20 w-full rounded-lg" />
    </div>
  )
}

/**
 * Refresh the credit reading on demand. One press asks the gateway for one
 * refresh (`POST /api/sessions/usage/refresh`: the free API, then the `/usage`
 * scrape) and the reading lands in the same `['kiro-usage']` query the top-bar
 * pill polls, so the pill updates with the modal.
 */
function useUsageRefresh() {
  const queryClient = useQueryClient()
  const [notice, setNotice] = useState<UsageRefreshNotice | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)
  const mutation = useMutation({
    mutationFn: () => api.sessionsUsageRefresh(),
    // Never retried: the 409 the gateway sends while a refresh is already
    // running is an answer to show, not a throttle to wait out, and a retry
    // would queue a second whoami + /usage subprocess pair behind the first.
    retry: false,
    onMutate: () => setNotice(null),
    onSuccess: (res: KiroUsageRefreshResponse) => {
      const parsed = parseKiroUsagePayload(res)
      // The refreshed state replaces the pill's query whatever it is, and BEFORE
      // any outcome is classified. A terminal state (no reading, API-key auth,
      // sign-in required) and the parked scrape's payload must land too: the
      // gateway just told us what it now holds for this account, and after an
      // account switch the reading on screen is the PREVIOUS account's email
      // and balance -- leaving it there until the pill's next poll would show
      // one account's numbers under another's session.
      queryClient.setQueryData(['kiro-usage'], parsed)
      void queryClient.invalidateQueries({ queryKey: ['kiro-usage'] })
      if (res.skipped === 'scrape_parked') {
        const secs = typeof res.retry_after === 'number' ? res.retry_after : 0
        setNotice({ kind: 'parked', minutes: Math.max(1, Math.ceil(secs / 60)) })
        return
      }
      if (!isUsageReading(parsed)) {
        // The refresh ran and still produced no plan.
        setNotice({ kind: 'failed' })
        return
      }
      if (parsed.stale) {
        // The gateway answered with its EARLIER reading, dimmed: this refresh
        // fetched nothing new. Stamping it as checked now would claim a
        // freshness the numbers do not have, so the time stays unset and the
        // outcome is reported instead, on the error surface: the refresh the
        // user asked for produced nothing new.
        setNotice({ kind: 'stale' })
        return
      }
      setCheckedAt(new Date())
    },
    onError: err => setNotice(classifyRefreshError(err)),
  })
  // A 409 means the gateway is refreshing already. Re-read the pill's query
  // once after that refresh has had time to settle, so its result shows here
  // without the user pressing again; the pill's 30 s poll covers a slower one.
  // Once that re-read has landed the "already running" notice has done its job
  // -- whatever the other refresh produced is on screen -- so it clears and
  // Refresh is offered again.
  const inFlight = notice?.kind === 'in_flight'
  useEffect(() => {
    if (!inFlight) return
    let cancelled = false
    const timer = window.setTimeout(() => {
      void queryClient.invalidateQueries({ queryKey: ['kiro-usage'] }).then(() => {
        if (!cancelled) setNotice(current => (current?.kind === 'in_flight' ? null : current))
      })
    }, IN_FLIGHT_RECHECK_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [inFlight, queryClient])
  // Refresh is held while pressing it could not help: the gateway is already
  // refreshing (the notice says so; the re-read brings that result), or
  // refreshes are parked (the notice says when to try again). Pressing into
  // either would only produce the same notice again.
  const held = notice?.kind === 'in_flight' || notice?.kind === 'parked'
  return { mutation, notice, checkedAt, held }
}

function RefreshButton({
  pending,
  held,
  onClick,
  compact,
}: {
  pending: boolean
  /** Disabled without the pending look: the notice beside it says why. */
  held: boolean
  onClick: () => void
  compact: boolean
}) {
  // One verb for the one action, wherever the button sits.
  const label = pending
    ? i18nT('components.kiroAccountModal.refreshing_balance')
    : i18nT('components.kiroAccountModal.refresh_balance')
  const shape = compact
    ? 'rounded-md border border-border bg-transparent px-2.5 py-1 text-[12px] font-medium text-muted hover:border-border-strong hover:text-text'
    : 'rounded-md bg-accent px-3 py-1.5 text-[13px] font-medium text-accent-fg hover:brightness-110'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending || held}
      aria-busy={pending || undefined}
      className={`inline-flex items-center gap-1.5 self-start transition-all disabled:cursor-default disabled:opacity-60 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-accent ${shape}`}
    >
      {pending
        ? <Loader2 className="lucide-inline animate-spin" aria-hidden="true" />
        : <RefreshCw className="lucide-inline" aria-hidden="true" />}
      {label}
    </button>
  )
}

/** The query the top bar reads the selected harness from (`agent.acp_backend`). */
const KIROCREW_CONFIG_QUERY = ['kirocrewConfig'] as const

function CreditUsage({ usage, onClose }: { usage: KiroAccountUsage; onClose: () => void }) {
  const queryClient = useQueryClient()
  const refresh = useUsageRefresh()
  // The config read failed and nothing else retries it: the shared client
  // never lets a query go stale on its own, so without this the pill would
  // say "could not read the backend setting" until a reload. Opening the
  // modal on that state is the user asking again, so ask again.
  const configUnreadable = usage === 'config-unreadable'
  useEffect(() => {
    if (configUnreadable) void queryClient.invalidateQueries({ queryKey: KIROCREW_CONFIG_QUERY })
  }, [configUnreadable, queryClient])
  const refreshButton = (compact: boolean) => (
    <RefreshButton
      pending={refresh.mutation.isPending}
      held={refresh.held}
      onClick={() => {
        // Pressing Refresh on the unreadable-config state retries BOTH reads:
        // the config that decides the surface and the balance it would show.
        if (configUnreadable) void queryClient.invalidateQueries({ queryKey: KIROCREW_CONFIG_QUERY })
        refresh.mutation.mutate()
      }}
      compact={compact}
    />
  )
  // Every refresh outcome that produced nothing new -- including the 409 the
  // gateway sends while its own refresh is running -- takes the shared error
  // surface with its agent hand-off, the same shape as the sign-in notice
  // below. The hand-off opens the chat this overlay sits over, so the modal
  // closes with it. The 409 notice additionally clears by itself once the
  // query re-read above has landed.
  const refreshNotice = refresh.notice === null
    ? null
    : <ErrorNotice message={refreshNoticeMessage(refresh.notice)} askAgent onHandoff={onClose} />
  // Only a cache that has not warmed yet is still loading. A failed fetch and an
  // account with no plan both have nothing pending, so they get the static
  // notice rather than a skeleton that never resolves.
  if (usage === null) return <UsageSkeleton />
  // The two FAILURE states take the shared error surface and its agent
  // hand-off: an expired sign-in (the one unreadable state the user can act
  // on) and a fetch that failed with nothing cached. That hand-off opens the
  // chat this overlay sits over, so the modal closes with it — a hand-off the
  // user cannot see reads as a dead button. The remaining states report a
  // configuration the account is in rather than a failure to recover from, so
  // they stay on the passive notice.
  if (usage === 'signin-required') {
    return (
      <ErrorNotice
        message={i18nT('components.kiroAccountModal.credit_usage_signin_required')}
        askAgent
        onHandoff={onClose}
      />
    )
  }
  if (usage === 'failed' || usage === 'config-unreadable') {
    // One notice at a time: a refresh outcome REPLACES the standing notice
    // instead of stacking under it, so the box never carries two alerts. The
    // standing notice returns once the outcome clears. `config-unreadable`
    // shares the shape: a read that failed, with the retry under it.
    const outcomeReplacesNotice = refresh.notice !== null
    return (
      <div className="flex flex-col gap-3">
        {outcomeReplacesNotice
          ? refreshNotice
          : (
            <ErrorNotice
              message={i18nT(usage === 'config-unreadable'
                ? 'components.kiroAccountModal.credit_usage_config_unreadable'
                : 'components.kiroAccountModal.credit_usage_unavailable')}
              askAgent
              onHandoff={onClose}
            />
          )}
        {/* Refresh sits directly under the failure it can retry. */}
        {refreshButton(false)}
        {!outcomeReplacesNotice && refreshNotice}
      </div>
    )
  }
  if (!isUsageReading(usage)) {
    // Same shape as `failed`: one notice, above the Refresh button. A refresh
    // outcome REPLACES the standing box while it shows, so the notice is in the
    // same place whichever no-reading state the modal opened in.
    const outcomeReplacesNotice = refresh.notice !== null
    return (
      <div className="flex flex-col gap-3">
        {outcomeReplacesNotice
          ? refreshNotice
          : (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 p-3.5 text-[13px] text-muted">
              <AlertCircle className="lucide-inline shrink-0" />{' '}
              {/* `none` reports that no reading exists yet, not that a read failed
                  (`failed`, above, says that), so its copy names the absence and
                  the Refresh under it is the way to fill it. */}
              {i18nT(usage === 'api-key'
                ? 'components.kiroAccountModal.credit_usage_api_key_auth'
                : 'components.kiroAccountModal.credit_usage_no_reading')}
            </div>
          )}
        {/* Refresh sits directly under the notice that says there is no reading. */}
        {canRefreshUsage(usage) && refreshButton(false)}
      </div>
    )
  }

  const pct = usage.limit > 0 ? (usage.used / usage.limit) * 100 : 0
  const remaining = Math.max(usage.limit - usage.used, 0)
  const progressNow = Math.min(Math.max(usage.used, 0), Math.max(usage.limit, 0))

  return (
    <div className="flex flex-col gap-3">
      {usage.plan && <DetailRow label={i18nT('app.plan')} value={usage.plan} />}
      <div>
        <div className="mb-2 flex items-baseline gap-2">
          <span className="text-2xl font-bold text-text">{fmtNumber(usage.used)}</span>
          <span className="text-sm text-muted">/ {fmtNumber(usage.limit)} {i18nT('app.credits')}</span>
          <span className="ml-auto rounded-md bg-accent px-2 py-0.5 text-[12px] font-medium text-accent-fg">
            {fmtPercent(pct / 100)}
          </span>
        </div>
        <div
          role="progressbar"
          aria-label={i18nT('components.kiroAccountModal.kiro_credit_usage')}
          aria-valuemin={0}
          aria-valuemax={Math.max(usage.limit, 0)}
          aria-valuenow={progressNow}
          className="h-2 w-full overflow-hidden rounded-full bg-border"
        >
          <div
            className="h-full rounded-full bg-accent transition-all"
            style={{ width: `${Math.min(Math.max(pct, 0), 100)}%` }}
          />
        </div>
        <div className="mt-2 flex items-center justify-between gap-4 text-[12px] text-muted">
          <span>
            {i18nT('components.kiroAccountModal.remaining_credit_balance', {
              count: fmtNumber(remaining),
            })}
          </span>
          {usage.resets && <span>{i18nT('app.resets')} {formatResetDate(usage.resets)}</span>}
        </div>
      </div>
      <div className="rounded-lg border border-border px-3">
        <DetailRow label={i18nT('app.overage_used')} value={`${fmtNumber(usage.overage)} ${i18nT('app.credits')}`} />
        {usage.overageRate != null && (
          <DetailRow
            label={i18nT('app.overage_rate')}
            value={i18nT('components.kiroAccountModal.overage_rate_value', {
              rate: fmtCurrency(usage.overageRate),
            })}
          />
        )}
        {usage.costUsd != null && (
          <DetailRow
            label={i18nT('components.kiroAccountModal.estimated_overage_cost')}
            value={fmtCurrency(usage.costUsd)}
          />
        )}
      </div>
      {usage.bonusCredits && <BonusCredits grants={usage.bonusCredits} />}
      {/* A compact Refresh beside every reading, with when this session last
          refreshed it. A dimmed (stale) reading says so plainly: the gateway is
          showing an earlier value because its latest refresh returned none.
          While the stale-refresh notice below states that same fact, this line
          yields to it -- one statement, not two. */}
      <div className="flex items-center justify-between gap-3 text-[12px] text-muted">
        <span>
          {refresh.checkedAt
            ? i18nT('components.kiroAccountModal.balance_checked_at', { time: fmtTime(refresh.checkedAt) })
            : usage.stale && refresh.notice?.kind !== 'stale'
              ? i18nT('components.kiroAccountModal.balance_may_be_stale')
              : null}
        </span>
        {refreshButton(true)}
      </div>
      {refreshNotice}
      <p className="text-[11px] leading-relaxed text-muted">
        {i18nT('components.kiroAccountModal.usage_scope')}
      </p>
    </div>
  )
}

export default function KiroAccountModal({ open, onClose, usage }: KiroAccountModalProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={<span className="flex items-center gap-2"><Coins className="lucide-inline" /> {i18nT('components.kiroAccountModal.kiro_account')}</span>}
      maxWidth={460}
    >
      <div className="flex flex-col gap-4">
        <AccountIdentity usage={usage} />
        <CreditUsage usage={usage} onClose={onClose} />
        <a
          href={KIRO_ACCOUNT_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 self-start text-[12px] text-accent hover:underline"
        >
          {i18nT('app.manage_account')} <ExternalLink className="lucide-inline" />
        </a>
      </div>
    </Modal>
  )
}
