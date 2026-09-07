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
  ChevronDown, RefreshCw, Copy, Check, HardDrive, Star, KeyRound, ShieldCheck, Wallet, Receipt, Cloud,
} from 'lucide-react'
import { Btn, Badge, Card, CardTitle, StatCard, Skeleton } from '../../components/ui'
import AwsConsentGate from '../../components/AwsConsentGate'
import { i18nT } from '../../i18n/t'
import { CopyBtn, PaneHeader, AwsErrorNotice } from './shared'
import { StorageMeter } from './DrivePage'
import { fmtBytes, fmtCurrency, fmtDate, fmtNumber } from '../../i18n/format'
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
              <p className="mb-2 text-[12px] text-muted">{i18nT(RECONNECT_HINT_KEY[planQ.data.kind])}</p>
              {/* The command owns its own line and the copy button sits under it,
                  right-aligned. Sharing one row made the command the loser of
                  every width contest: it is the long, unbreakable, mono string,
                  so at 390px it was the part that got squeezed. */}
              <code
                className="block min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text"
                data-testid="reconnect-command"
              >
                {planQ.data.command}
              </code>
              <div className="mt-2 flex justify-end">
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

/** One row per profile/key: name, kind, region, health + Reconnect if failing. */
function ConnectionRow({ profile, askAgent }: { profile: AwsProfile; askAgent: boolean }) {
  return (
    <div className="px-1 py-2.5 md:px-3" data-testid="connection-row">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-[11rem]">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="min-w-0 truncate font-mono text-[13px] text-text-strong" data-testid="connection-name">{profile.name}</span>
            {/* The star alone was a glyph a first-time reader could not name;
                the word rides with it. */}
            {profile.default && (
              <Badge variant="muted" className="shrink-0" data-testid="connection-default">
                <Star className="h-3 w-3 fill-accent text-accent" aria-hidden="true" />
                {i18nT('apps.awsControl.page.default_profile')}
              </Badge>
            )}
            <Badge variant="muted">{i18nT(PROFILE_KIND_LABEL_KEY[profile.kind])}</Badge>
          </div>
          <div className="mt-0.5 font-mono text-[12px] text-muted">{profile.region}</div>
        </div>
        <div className="flex items-center gap-2">
          {/* Dot plus word, not dot plus coloured text: the badge carries the
              state for a reader who cannot see the hue, so the dot is
              decoration and is hidden from assistive technology rather than
              announcing the same fact twice. */}
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${profile.identityOk ? 'bg-ok' : 'bg-warn'}`}
            aria-hidden="true"
            data-testid="connection-health"
            data-ok={profile.identityOk}
          />
          <Badge variant={profile.identityOk ? 'ok' : 'warn'}>
            {profile.identityOk
              ? i18nT('apps.awsControl.console.key_healthy')
              : i18nT('apps.awsControl.console.key_needs_attention')}
          </Badge>
        </div>
      </div>
      {!profile.identityOk && <ReconnectAction profile={profile} askAgent={askAgent} />}
    </div>
  )
}

/**
 * The keys card for ONE account: a row per key, with inline Reconnect for
 * failing ones. The title names the account, because this card sits under a
 * list of several accounts and a bare heading read as a global list — a reader
 * concluded the other accounts had no keys. `askAgent` flows down to the
 * Reconnect notices; the accounts pane that hosts this card decides it from
 * whether a registration draft is open.
 */
export function ConnectionsSection({ account, askAgent }: { account: AwsAccount; askAgent: boolean }) {
  return (
    <Card data-testid="connections-section">
      <CardTitle>
        <KeyRound className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
        {i18nT('apps.awsControl.console.keys_for_account', { name: account.name || account.account })}
      </CardTitle>
      {account.profiles.length === 0 ? (
        // One line, not `EmptyState`: its 96px well and 40px glyph are taller
        // than this card's whole populated state (a single ~44px row), so an
        // account with no keys would draw more of the eye than one with keys.
        <p className="text-[13px] text-muted" data-testid="connections-empty">
          {i18nT('apps.awsControl.page.not_connected_yet')}
        </p>
      ) : (
        <div className="divide-y divide-border" data-testid="connections-list">
          {account.profiles.map((p) => (
            <ConnectionRow key={p.name} profile={p} askAgent={askAgent} />
          ))}
        </div>
      )}
    </Card>
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
    <Card className="mb-0" data-testid="drive-setup">
      <CardTitle>
        <Cloud className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
        {i18nT('apps.awsControl.console.setup_title')}
      </CardTitle>
      <p className="mb-1 text-[13px] text-muted">{i18nT('apps.awsControl.console.setup_body')}</p>
      <p className="mb-3 text-[13px] text-muted">{i18nT('apps.awsControl.console.setup_costs_note')}</p>

      {preview && !confirmMut.isSuccess && (
        <div className="mb-3 rounded-md border border-border bg-bg-elevated p-3" data-testid="drive-preview">
          <dl className="grid grid-cols-1 gap-x-3 gap-y-1 sm:grid-cols-[auto_1fr]">
            <dt className="text-[12px] text-muted">{i18nT('apps.awsControl.console.setup_preview_region')}</dt>
            <dd className="mb-1 min-w-0 break-all font-mono text-[13px] text-text sm:mb-0">{preview.region || region}</dd>
            <dt className="text-[12px] text-muted">{i18nT('apps.awsControl.console.setup_preview_resource')}</dt>
            <dd className="min-w-0 break-all font-mono text-[13px] text-text">{preview.resource}</dd>
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
        <Btn onClick={() => setShowPolicy((v) => !v)} aria-expanded={showPolicy} data-testid="policy-toggle">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
          {i18nT('apps.awsControl.console.setup_policy_label')}
          <ChevronDown className={`h-3.5 w-3.5 transition-transform ${showPolicy ? 'rotate-180' : ''}`} aria-hidden="true" />
        </Btn>
        {showPolicy && (
          <div className="mt-2" data-testid="policy-drawer">
            {policyQ.isLoading && <div className="text-[12px] text-muted">{i18nT('apps.awsControl.console.loading')}</div>}
            <AwsErrorNotice
              askAgent
              error={policyQ.error}
              message={policyQ.isError ? i18nT('apps.awsControl.console.setup_policy_error') : null}
              onRetry={() => policyQ.refetch()}
              testId="policy-error"
            />
            {policyQ.data && (
              // The copy button sits ON the block, top-right, so it is where the
              // eye already is when the policy is what you came for. `pr-24`
              // keeps the longest ARN clear of it instead of running underneath.
              <div className="relative rounded-md border border-border bg-bg-elevated p-3">
                <div className="absolute right-2 top-2">
                  <CopyBtn text={policyQ.data.policy} testId="policy-copy" />
                </div>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all pr-24 font-mono text-[11px] text-text">
                  {policyQ.data.policy}
                </pre>
              </div>
            )}
          </div>
        )}
      </div>
    </Card>
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
  // A 200 carrying a stale figure with `fetchError` is a failed refresh served
  // from cache: keep the number, say the failure.
  const costsStale = Boolean(costs?.fetchError)
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
  // The Cost Explorer ask, driven by the CONSENT state rather than by
  // `costs.consentMissing`. That field only arrives when the backend has a
  // cached cost reading to attach it to; with no cache — the state a
  // never-confirmed account is always in — the costs request is a bare 409 and
  // the field never exists, so keying the ask on it would leave Cost Explorer
  // with no confirmation control anywhere in the product.
  const ceAsk = ceConsentQ.data?.granted === false
  // Both surfaces whose content a grant decides. The ask reads a cached refusal
  // and the meter reads a cached listing, so a grant change has to reach them
  // or the pane keeps rendering the previous answer.
  const refetchGated = () => {
    qcTop.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })
    qcTop.invalidateQueries({ queryKey: ['aws-control', 'costs', id] })
  }

  // The three figures this pane states. Each is `undefined` while its read is
  // in flight, which is what makes `StatCard` draw its own skeleton in place —
  // the strip never changes height between loading and loaded. A read that
  // settled with no number renders the em dash, and the sub-lines below say
  // why, on the row rather than behind a hover.
  const costFigure = costs && !costs.consentMissing && !costsQ.isError
  const mtdValue = costsQ.isLoading
    ? undefined
    : costFigure ? fmtCurrency(costs.monthToDate, costs.currency) : '—'
  const storageValue = driveQ.isLoading
    ? undefined
    : drive?.exists ? fmtBytes(drive.usage.bytes) : '—'
  const objectsValue = driveQ.isLoading
    ? undefined
    : drive?.exists ? fmtNumber(drive.usage.objects) : '—'

  return (
    <section data-testid="usage-pane">
      <PaneHeader icon={<Wallet size={18} />} title={i18nT('apps.awsControl.rail.usage')} />

      {/* The pane's three figures, with the bill in accent because it is the one
          this pane alone can state. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3" data-testid="console-stats">
        <StatCard
          label={i18nT('apps.awsControl.console.stat_this_month')}
          value={mtdValue}
          accent
          className="tabular-nums"
          data-testid="console-cost-stat"
        />
        <StatCard
          label={i18nT('apps.awsControl.console.stat_storage')}
          value={storageValue}
          className="tabular-nums"
          data-testid="console-storage-stat"
        />
        <StatCard
          label={i18nT('apps.awsControl.console.stat_objects')}
          value={objectsValue}
          className="tabular-nums"
          data-testid="console-objects-stat"
        />
      </div>
      {/* The bill's context sits under the whole strip rather than under its own
          card: a note inside one grid cell made that card a different height
          from its two neighbours. WHY there is no figure is a visible line, not
          a `title` attribute: a dash with its reason on hover left a mouse-less
          reader with a blank they could not explain. Only the consent case gets
          the full sentence; a failed READ is reported once by the notice below,
          so its line says only that the number is missing. */}
      <div className="mb-4 mt-1.5 min-h-[1.25rem] text-[12px] text-muted">
        {costs && costFigure && !costs.fresh && (
          <span>{i18nT('apps.awsControl.console.costs_as_of', { date: fmtDate(costs.fetchedAt) })}</span>
        )}
        {costs?.consentMissing && (
          <span data-testid="console-cost-reason">
            {i18nT('apps.awsControl.console.costs_consent_missing')}
          </span>
        )}
      </div>
      <AwsErrorNotice
        askAgent
        error={costsQ.error}
        message={costsFailed
          ? i18nT('apps.awsControl.console.costs_unavailable')
          : costsStale
            ? i18nT('apps.awsControl.console.costs_refresh_failed')
            : null}
        onRetry={() => costsQ.refetch()}
        className="mb-4"
        testId="costs-error"
      />

      {/* Storage: the meter split by section, headed by the bucket it reports.
          The sections themselves are the rail's own items, so this pane states
          sizes only and links nowhere. */}
      {/* A box the same size as the meter, so the pane does not jump when the
          listing lands. */}
      {driveQ.isLoading && <Skeleton className="mb-4 h-[104px] rounded-lg" data-testid="usage-storage-skeleton" />}
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
        className="mb-4"
        testId="usage-drive-error"
      />
      {drive?.exists && (
        <div data-testid="usage-storage">
          <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
            <HardDrive className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
            <span className="text-[13px] font-medium text-text-strong">{i18nT('apps.awsControl.console.drive_title')}</span>
            <span className="min-w-0 truncate font-mono text-[12px] text-muted" data-testid="drive-bucket">{drive.bucket}</span>
            <CopyBtn text={drive.bucket} testId="drive-copy-bucket" />
            <span className="text-[12px] text-muted">{drive.region}</span>
          </div>
          <StorageMeter usage={drive.usage} />
        </div>
      )}

      {/* The paid services this account bills through: a receipt once a grant is
          recorded for THIS account and its ask has cleared, and the Cost
          Explorer ask itself as a row in the same list. The ask belongs here
          rather than in a banner of its own for one reason — a reader comparing
          "what is enabled" against "what is not" is reading one list, and the
          two states are the same row with a different right-hand control.
          Each row is mounted on its own condition rather than the card's,
          because the two services are granted separately and a receipt for one
          must not be implied by the other. Withdrawing here revokes the one
          grant this account's drive and cost figure run on. `onConsentChange` is
          what makes a withdraw recoverable: the asks are decided by cached
          refusals, so without invalidating them the receipt would unmount with
          no ask taking its place. */}
      {(s3Receipt || ceReceipt || ceAsk) && (
        <Card data-testid="paid-services">
          <CardTitle>
            <Receipt className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
            {i18nT('apps.awsControl.page.paid_services_title')}
          </CardTitle>
          <div className="divide-y divide-border">
            {s3Receipt && <AwsConsentGate service="s3" compact onConsentChange={refetchGated} askAgent />}
            {ceReceipt && <AwsConsentGate service="ce" compact onConsentChange={refetchGated} askAgent />}
            {ceAsk && (
              <div data-testid="costs-consent-gate">
                <AwsConsentGate service="ce" compact onConsentChange={refetchGated} askAgent />
              </div>
            )}
          </div>
        </Card>
      )}
    </section>
  )
}
