/**
 * AWS Control — the Usage & costs pane, plus the account plumbing the other
 * rail panes share (`ReconnectAction`, `ConnectionsSection`, `SetupCard`).
 *
 * The per-account console this file used to be dissolved into the flat-rail
 * layout in `AwsControlPage`: the drive's sections are rail items of their own,
 * connections live on the Accounts & credentials pane, and the money-shaped
 * facts (bill, storage meter, consent receipts) landed here. Every mutation is
 * confirmed before it runs and ends by invalidating its react-query key. All
 * AWS access runs through the gateway's audited CLI chokepoint — this surface
 * never talks to AWS from the browser.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ChevronDown, RefreshCw, Copy, Check, HardDrive, Star, Link2, ShieldCheck, Wallet, } from 'lucide-react'
import { Btn, Badge, ContentSkeleton } from '../../components/ui'
import AwsConsentGate from '../../components/AwsConsentGate'
import { i18nT } from '../../i18n/t'
import { CopyBtn, SectionHeader, PaneHeader, AwsErrorNotice } from './shared'
import { StorageMeter } from './DrivePage'
import { fmtCurrency, fmtDate } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import { api, type AwsConsentStatus } from '../../api/client'
import type {
  AwsAccount, AwsProfile, ProfileKind, ReconnectPlan, DriveStatus,
} from './types'

/** Credential-kind badge label, keyed literally (dynamicKeys gate). */
const PROFILE_KIND_LABEL_KEY: Record<ProfileKind, string> = {
  sso: 'apps.awsControl.page.kind_sso',
  'credential-process': 'apps.awsControl.page.kind_credential_process',
  other: 'apps.awsControl.page.kind_other',
}

/** One plain sentence of Reconnect guidance per credential kind. */
const RECONNECT_HINT_KEY: Record<ProfileKind, string> = {
  sso: 'apps.awsControl.page.reconnect_hint_sso',
  'credential-process': 'apps.awsControl.page.reconnect_hint_credential_process',
  other: 'apps.awsControl.page.reconnect_hint_other',
}

/* ── Section: Connections ────────────────────────────────────────────────── */

/**
 * Inline Reconnect for a failing key, moved here from the Accounts list. Fetches
 * the profile's reconnect-plan on demand and shows the command in a mono block
 * with a copy button plus a one-sentence hint for its credential kind.
 *
 * `askAgent` is the host's call, not this component's: the same Reconnect
 * renders on the accounts pane next to the Add-accounts checkboxes, and a
 * hand-off there navigates away from a ticked-but-unregistered selection.
 */
export function ReconnectAction({ profile, askAgent }: { profile: AwsProfile; askAgent: boolean }) {
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const planQ = useQuery<ReconnectPlan>({
    queryKey: ['aws-control', 'reconnect-plan', profile.name],
    queryFn: () => awsControlApi.reconnectPlan(profile.name),
    enabled: open,
  })

  const copy = async () => {
    if (!planQ.data) return
    try {
      await navigator.clipboard.writeText(planQ.data.command)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable — the command is still visible to copy by hand */ }
  }

  return (
    <div className="mt-2" data-testid="reconnect">
      <Btn onClick={() => setOpen((v) => !v)} data-testid="reconnect-toggle" aria-expanded={open}>
        <RefreshCw size={13} />
        {i18nT('apps.awsControl.page.reconnect')}
        <ChevronDown size={13} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </Btn>
      {open && (
        <div className="mt-2 rounded-md border border-border bg-bg-elevated p-3 text-[13px]" data-testid="reconnect-panel">
          {planQ.isLoading && (
            <div className="text-muted" data-testid="reconnect-loading">
              {i18nT('apps.awsControl.page.reconnect_loading')}
            </div>
          )}
          <AwsErrorNotice
            askAgent={askAgent}
            error={planQ.error}
            message={planQ.isError ? i18nT('apps.awsControl.page.reconnect_error') : null}
            onRetry={() => planQ.refetch()}
            testId="reconnect-error"
          />
          {planQ.data && (
            <>
              <p className="text-muted mb-2">{i18nT(RECONNECT_HINT_KEY[planQ.data.kind])}</p>
              <div className="flex items-center gap-2">
                <code
                  className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text"
                  data-testid="reconnect-command"
                >
                  {planQ.data.command}
                </code>
                <Btn onClick={copy} data-testid="reconnect-copy">
                  {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
                  {copied ? i18nT('apps.awsControl.page.copied') : i18nT('apps.awsControl.page.copy')}
                </Btn>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** One thin row per profile/key: name, kind, region, health + Reconnect if failing. */
function ConnectionRow({ profile, askAgent }: { profile: AwsProfile; askAgent: boolean }) {
  return (
    <div className="px-3 py-2.5" data-testid="connection-row">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-mono text-[13px] text-text" data-testid="connection-name">{profile.name}</span>
        {profile.default && (
          <Star size={11} className="text-accent fill-accent" aria-label={i18nT('apps.awsControl.page.default_profile')} />
        )}
        <Badge variant="muted">{i18nT(PROFILE_KIND_LABEL_KEY[profile.kind])}</Badge>
        <span className="font-mono text-[12px] text-muted">{profile.region}</span>
        <span className="ml-auto flex items-center gap-1.5 text-[12px]">
          <span className={`h-2 w-2 rounded-full ${profile.identityOk ? 'bg-ok' : 'bg-warn'}`} role="img" aria-label={profile.identityOk ? i18nT('apps.awsControl.console.key_healthy') : i18nT('apps.awsControl.console.key_failed')} data-testid="connection-health" data-ok={profile.identityOk} />
          <span className={profile.identityOk ? 'text-ok' : 'text-warn'}>
            {profile.identityOk ? i18nT('apps.awsControl.console.key_healthy') : i18nT('apps.awsControl.console.key_failed')}
          </span>
        </span>
      </div>
      {!profile.identityOk && <ReconnectAction profile={profile} askAgent={askAgent} />}
    </div>
  )
}

/**
 * The Connections card: one thin row per key, with inline Reconnect for failing
 * ones. `askAgent` flows down to those Reconnect notices; the accounts pane
 * that hosts this card decides it from whether a registration draft is open.
 */
export function ConnectionsSection({ account, askAgent }: { account: AwsAccount; askAgent: boolean }) {
  return (
    <section data-testid="connections-section">
      <SectionHeader icon={<Link2 size={15} />} title={i18nT('apps.awsControl.console.connections')} />
      {account.profiles.length === 0 ? (
        <p className="text-[13px] text-muted" data-testid="connections-empty">
          {i18nT('apps.awsControl.page.not_connected_yet')}
        </p>
      ) : (
        <div className="rounded-md border border-border bg-card divide-y divide-border" data-testid="connections-list">
          {account.profiles.map((p) => (
            <ConnectionRow key={p.name} profile={p} askAgent={askAgent} />
          ))}
        </div>
      )}
    </section>
  )
}

/* ── Section 3: drive-missing setup card ─────────────────────────────────── */

export function SetupCard({ account, region }: { account: string; region: string }) {
  const qc = useQueryClient()
  const [showPolicy, setShowPolicy] = useState(false)
  const previewMut = useMutation({
    mutationFn: () => awsControlApi.driveBootstrapPreview(account),
  })
  const confirmMut = useMutation({
    mutationFn: () => awsControlApi.driveBootstrapConfirm(account),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] }),
  })
  const policyQ = useQuery({
    queryKey: ['aws-control', 'iam-policy'],
    queryFn: () => awsControlApi.iamPolicy(),
    enabled: showPolicy,
  })

  const preview = previewMut.data
  const busy = previewMut.isPending || confirmMut.isPending

  return (
    <div className="rounded-lg border border-border bg-card px-4 py-4 shadow-sm" data-testid="drive-setup">
      <div className="flex items-center gap-2 mb-1">
        <HardDrive size={16} className="text-accent" />
        <h2 className="text-sm font-semibold text-text-strong">
          {i18nT('apps.awsControl.console.setup_title')}
        </h2>
      </div>
      <p className="text-[13px] text-muted mb-1">{i18nT('apps.awsControl.console.setup_body')}</p>
      <p className="text-[13px] text-muted mb-3">{i18nT('apps.awsControl.console.setup_costs_note')}</p>

      {preview && !confirmMut.isSuccess && (
        <div className="mb-3 rounded-md border border-border bg-bg-elevated p-3 text-[13px]" data-testid="drive-preview">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-muted">
            <dt>{i18nT('apps.awsControl.console.setup_preview_region')}</dt>
            <dd className="text-text font-mono">{preview.region || region}</dd>
            <dt>{i18nT('apps.awsControl.console.setup_preview_resource')}</dt>
            <dd className="text-text font-mono break-all">{preview.resource}</dd>
          </dl>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {!preview && (
          <Btn primary onClick={() => previewMut.mutate()} disabled={busy} data-testid="drive-preview-btn">
            {i18nT('apps.awsControl.console.setup_preview_btn')}
          </Btn>
        )}
        {preview && !confirmMut.isSuccess && (
          <Btn primary onClick={() => confirmMut.mutate()} disabled={busy} data-testid="drive-confirm-btn">
            {confirmMut.isPending
              ? i18nT('apps.awsControl.console.setup_creating')
              : i18nT('apps.awsControl.console.setup_confirm_btn')}
          </Btn>
        )}
      </div>

      <AwsErrorNotice
        askAgent
        error={previewMut.error}
        message={previewMut.isError ? i18nT('apps.awsControl.console.setup_error') : null}
        className="mt-2"
        testId="drive-preview-error"
      />
      {/* The CONFIRM can fail too — AccessDenied on CreateBucket is the common
          case — and it used to fail silently: the button just came back. The
          permissions drawer below is the fix, so this line sits right above it. */}
      <AwsErrorNotice
        askAgent
        error={confirmMut.error}
        message={confirmMut.isError ? i18nT('apps.awsControl.console.setup_confirm_error') : null}
        className="mt-2"
        testId="drive-confirm-error"
      />

      {/* Collapsed "show the exact permissions to paste" drawer for AccessDenied setups. */}
      <div className="mt-3">
        <button
          onClick={() => setShowPolicy((v) => !v)}
          className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
          aria-expanded={showPolicy}
          data-testid="policy-toggle"
        >
          <ShieldCheck size={12} />
          {i18nT('apps.awsControl.console.setup_policy_label')}
          <ChevronDown size={12} className={`transition-transform ${showPolicy ? 'rotate-180' : ''}`} />
        </button>
        {showPolicy && (
          <div className="mt-2" data-testid="policy-drawer">
            {policyQ.isLoading && <div className="text-muted text-[12px]">{i18nT('apps.awsControl.console.loading')}</div>}
            <AwsErrorNotice
              askAgent
              error={policyQ.error}
              message={policyQ.isError ? i18nT('apps.awsControl.console.setup_policy_error') : null}
              onRetry={() => policyQ.refetch()}
              testId="policy-error"
            />
            {policyQ.data && (
              <div className="flex flex-col gap-2">
                <pre className="max-h-64 overflow-auto rounded-md bg-bg px-3 py-2 font-mono text-[11px] text-text whitespace-pre-wrap break-all">
                  {policyQ.data.policy}
                </pre>
                <div><CopyBtn text={policyQ.data.policy} testId="policy-copy" /></div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/* ── The Usage & costs pane ──────────────────────────────────────────────── */

/**
 * Usage & costs for the selected account: the month-to-date bill, the storage
 * meter split by drive section, and the paid-service consent receipts. This is
 * a rail pane in `AwsControlPage`, not a page of its own — the per-account
 * console this file used to export dissolved into the flat-rail layout, and
 * this pane is where its money-shaped facts landed.
 */
export default function UsagePane({ account }: { account: AwsAccount }) {
  const id = account.account
  const qcTop = useQueryClient()

  const driveQ = useQuery({
    queryKey: ['aws-control', 'drive', id],
    queryFn: () => awsControlApi.drive(id),
  })
  const costsQ = useQuery({
    queryKey: ['aws-control', 'costs', id],
    queryFn: () => awsControlApi.costs(id),
    // A dead bill read (CE not enabled, throttled) should settle to the
    // quiet em-dash in seconds, not skeleton through three backoffs.
    retry: 1,
  })

  const drive: DriveStatus | undefined = driveQ.data
  const costs = costsQ.data

  // A receipt belongs on THIS pane only when the grant it shows was recorded
  // for the SELECTED account. A grant is service-scoped and carries the account
  // it was confirmed for; a withdraw is global, so showing another account's
  // receipt here would put a destructive control on the wrong account.
  //
  // It is also suppressed while that service's own refusal is still on screen:
  // granting invalidates the consent query but not the drive or costs caches, so
  // for the renders between a grant and the next refetch the ask and the receipt
  // would both be visible, saying opposite things about the same service.
  const driveErr = driveQ.error instanceof AwsControlError ? driveQ.error : null
  const driveConsentRefused =
    driveQ.isError && driveErr?.status === 409 && driveErr.message === 'aws_consent_required'
  // A bill read that failed for a reason OTHER than the consent gate. The gate's
  // own 409 is not an error to diagnose — the ask below is its answer — so it
  // keeps the quiet em-dash alone; everything else (Cost Explorer not enabled,
  // a throttle, a dead key) gets a notice the agent can read.
  const costsErr = costsQ.error instanceof AwsControlError ? costsQ.error : null
  const costsFailed = costsQ.isError && costsErr?.message !== 'aws_consent_required'
  const s3ConsentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', 's3'],
    queryFn: () => api.awsConsent('s3'),
  })
  const ceConsentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', 'ce'],
    queryFn: () => api.awsConsent('ce'),
  })
  const confirmedHere = (c: AwsConsentStatus | undefined) =>
    c?.granted === true && c.grant?.account === id
  const s3Receipt = confirmedHere(s3ConsentQ.data) && !driveConsentRefused
  const ceReceipt = confirmedHere(ceConsentQ.data) && !costs?.consentMissing
  // Both surfaces whose content a grant decides. The ask reads a cached refusal
  // and the meter reads a cached listing, so a grant change has to reach them
  // or the pane keeps rendering the previous answer.
  const refetchGated = () => {
    qcTop.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })
    qcTop.invalidateQueries({ queryKey: ['aws-control', 'costs', id] })
  }

  return (
    <section data-testid="usage-pane">
      <PaneHeader icon={<Wallet size={18} />} title={i18nT('apps.awsControl.rail.usage')} />

      {/* One figure this pane alone can state: the month-to-date bill. Label
          left, amount right, in the same row language as the meter below. */}
      <div className="overflow-hidden rounded-lg border border-border bg-card" data-testid="console-stats">
        <div className="flex flex-wrap items-center gap-3 px-4 py-3">
          <Wallet size={15} className="shrink-0 text-accent" aria-hidden="true" />
          <span className="text-[13px] font-medium text-text-strong">{i18nT('apps.awsControl.console.stat_this_month')}</span>
          {costs && !costs.consentMissing && !costsQ.isError && !costs.fresh && (
            <span className="text-[12px] text-muted">{i18nT('apps.awsControl.console.costs_as_of', { date: fmtDate(costs.fetchedAt) })}</span>
          )}
          <span className="flex-1" />
          {costs?.consentMissing ? (
            <span className="text-[13px] text-muted" title={i18nT('apps.awsControl.console.costs_consent_missing')}>—</span>
          ) : costsQ.isError ? (
            // A failed bill read (Cost Explorer not enabled on the account,
            // network, throttle) must not skeleton forever — say "no number".
            <span className="text-[13px] text-muted" title={i18nT('apps.awsControl.console.costs_unavailable')}>—</span>
          ) : (
            <span className="text-[15px] font-semibold text-text-strong" data-testid="console-cost-value">
              {costs ? fmtCurrency(costs.monthToDate, costs.currency) : '…'}
            </span>
          )}
        </div>
      </div>
      <AwsErrorNotice
        askAgent
        error={costsQ.error}
        message={costsFailed ? i18nT('apps.awsControl.console.costs_unavailable') : null}
        onRetry={() => costsQ.refetch()}
        className="mt-2"
        testId="costs-error"
      />

      {/* Cost Explorer ask, driven by the CONSENT state rather than by
          `costs.consentMissing`. That field only arrives when the backend has
          a cached cost reading to attach it to; with no cache — the state a
          never-confirmed account is always in — the costs request is a bare
          409 and the field never exists, so keying the ask on it would leave
          Cost Explorer with no confirmation control anywhere in the product. */}
      {ceConsentQ.data?.granted === false && (
        <div className="mt-6" data-testid="costs-consent-gate">
          <AwsConsentGate service="ce" onConsentChange={refetchGated} askAgent />
        </div>
      )}

      {/* Storage: the meter split by section, headed by the bucket it reports.
          The sections themselves are the rail's own items, so this pane states
          sizes only and links nowhere. */}
      {driveQ.isLoading && <div className="mt-6"><ContentSkeleton rows={3} /></div>}
      {/* The storage meter's read failing rendered no meter and no explanation.
          A dead connection (409) points back at Reconnect; anything else is a
          read to diagnose. The consent 409 is excluded because its ask lives on
          the Files pane, not here. */}
      <AwsErrorNotice
        askAgent
        error={driveQ.error}
        message={
          driveQ.isError && !driveConsentRefused
            ? i18nT(driveErr?.status === 409
              ? 'apps.awsControl.console.account_unavailable'
              : 'apps.awsControl.console.drive_status_failed')
            : null
        }
        onRetry={() => driveQ.refetch()}
        className="mt-6"
        testId="usage-drive-error"
      />
      {drive?.exists && (
        <div className="mt-6" data-testid="usage-storage">
          <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
            <HardDrive size={15} className="shrink-0 text-accent" aria-hidden="true" />
            <span className="text-[13px] font-medium text-text-strong">{i18nT('apps.awsControl.console.drive_title')}</span>
            <span className="min-w-0 truncate font-mono text-[12px] text-muted" data-testid="drive-bucket">{drive.bucket}</span>
            <CopyBtn text={drive.bucket} testId="drive-copy-bucket" />
            <span className="text-[12px] text-muted">{drive.region}</span>
          </div>
          <StorageMeter usage={drive.usage} />
        </div>
      )}

      {/* The confirmations recorded for THIS account, once each is granted and
          its ask has cleared. Each card is mounted on its own condition rather
          than the section's, because the two services are granted separately
          and a receipt for one must not be implied by the other. Withdrawing
          here revokes the one grant this account's drive and cost figure run
          on. `onConsentChange` is what makes a withdraw recoverable: the asks
          are decided by cached refusals, so without invalidating them the
          receipt would unmount with no ask taking its place. */}
      {(s3Receipt || ceReceipt) && (
        <section className="mt-8" data-testid="paid-services">
          <h2 className="text-sm font-semibold text-text-strong">
            {i18nT('apps.awsControl.page.paid_services_title')}
          </h2>
          <div className="mt-3 overflow-hidden rounded-md border border-border bg-card divide-y divide-border">
            {s3Receipt && <AwsConsentGate service="s3" compact onConsentChange={refetchGated} askAgent />}
            {ceReceipt && <AwsConsentGate service="ce" compact onConsentChange={refetchGated} askAgent />}
          </div>
        </section>
      )}
    </section>
  )
}
