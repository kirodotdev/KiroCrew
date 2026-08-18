import { useState } from 'react'
import { AlertCircle, Coins, ExternalLink, Eye, EyeOff, Gift, Loader2, UserRound } from 'lucide-react'

import type { KiroBonusCreditGrant, KiroCreditUsage } from '../api/client'
import { fmtCurrency, fmtDateFields, fmtNumber, fmtPercent } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import Clickable from './Clickable'
import Modal from './Modal'

// The usage view model is owned by `api/client.ts` next to the wire payload it
// is normalized from, so this panel and the credits pill cannot drift apart.
export type { KiroBonusCreditGrant, KiroCreditUsage }

/**
 * What the modal can be handed: a reading, `null` while the gateway's usage
 * cache warms, `'none'` when the account has no credit plan, or `'failed'` when
 * the fetch itself failed with nothing cached. `null` is the ONLY value that
 * means "still loading" — the other two have nothing more to wait for, so
 * spinning on them would repeat the defect this distinction exists to remove.
 */
export type KiroAccountUsage = KiroCreditUsage | null | 'none' | 'failed'

/** True only for an actual reading, so the sentinels cannot reach a field access. */
const isUsageReading = (usage: KiroAccountUsage): usage is KiroCreditUsage =>
  typeof usage === 'object' && usage !== null

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
                  className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-border text-muted transition-colors hover:border-accent/35 hover:bg-accent/10 hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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

function CreditUsage({ usage }: { usage: KiroAccountUsage }) {
  // Only a cache that has not warmed yet is still loading. A failed fetch and an
  // account with no plan both have nothing pending, so they get the static
  // notice rather than a skeleton that never resolves.
  if (usage === null) return <UsageSkeleton />
  if (!isUsageReading(usage)) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated/40 p-3.5 text-[13px] text-muted">
        <AlertCircle className="lucide-inline shrink-0" /> {i18nT('components.kiroAccountModal.credit_usage_unavailable')}
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
          <span className="ml-auto rounded-md bg-accent px-2 py-0.5 text-[12px] font-medium text-white">
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
        <CreditUsage usage={usage} />
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
