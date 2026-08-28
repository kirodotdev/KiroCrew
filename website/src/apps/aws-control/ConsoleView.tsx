/**
 * AWS Control — the per-account console.
 *
 * Opened by clicking an account row on the Accounts page; a breadcrumb returns.
 * It is view state inside `AwsControlPage`, not a route of its own, because
 * `BuiltinAppRoute` resolves only single-segment routes.
 *
 * The console is a plain-language surface over the account's S3-backed cloud
 * drive (spec §3): grouped General + Connections header sections, a stats strip,
 * then either a setup card (when the bucket does not exist) or the drive's
 * Library / Drive / Backup / Access sections, plus the still-dashed Tasks and
 * Sites app ghosts. Every mutation here is confirmed before it runs and ends by
 * invalidating its react-query key. All AWS access runs through the gateway's
 * audited CLI chokepoint — this surface never talks to AWS from the browser.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ChevronLeft, ChevronRight, ChevronDown, RefreshCw, Copy, Check, HardDrive, Star, Link2, ShieldCheck, } from 'lucide-react'
import { Btn, Badge, StatCard, ContentSkeleton } from '../../components/ui'
import AwsConsentGate from '../../components/AwsConsentGate'
import { i18nT } from '../../i18n/t'
import { CopyBtn, SectionHeader } from './shared'
import type { LiveDrive } from './DrivePage'
import { fmtBytes, fmtCurrency, fmtDate } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import { api, type AwsConsentStatus } from '../../api/client'
import type {
  AwsAccount, AwsProfile, ProfileKind, ReconnectPlan, DriveStatus,
} from './types'

/** The name the console leads with: the backend name, or the "not connected" label. */
function accountName(account: AwsAccount): string {
  return account.name || i18nT('apps.awsControl.page.not_connected_yet')
}

/** Credential-kind badge label, keyed literally (dynamicKeys gate). */
const HEALTH_DOT: Record<string, string> = { ok: 'bg-ok', degraded: 'bg-warn', unknown: 'bg-muted' }
const CONNECTION_LABEL_KEY: Record<string, string> = {
  ok: 'apps.awsControl.console.connection_connected',
  degraded: 'apps.awsControl.console.connection_degraded',
  unknown: 'apps.awsControl.console.connection_unknown',
}
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
 */
export function ReconnectAction({ profile }: { profile: AwsProfile }) {
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
          {planQ.isError && (
            <div className="text-danger" data-testid="reconnect-error">
              {i18nT('apps.awsControl.page.reconnect_error')}
            </div>
          )}
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
function ConnectionRow({ profile }: { profile: AwsProfile }) {
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
      {!profile.identityOk && <ReconnectAction profile={profile} />}
    </div>
  )
}

/** The Connections card: one thin row per key, with inline Reconnect for failing ones. */
function ConnectionsSection({ account }: { account: AwsAccount }) {
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
            <ConnectionRow key={p.name} profile={p} />
          ))}
        </div>
      )}
    </section>
  )
}

/* ── Section 3: drive-missing setup card ─────────────────────────────────── */

function SetupCard({ account, region }: { account: string; region: string }) {
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

      {previewMut.isError && (
        <p className="mt-2 text-[13px] text-danger" data-testid="drive-preview-error">
          {i18nT('apps.awsControl.console.setup_error')}
        </p>
      )}

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

/* ── Console shell ───────────────────────────────────────────────────────── */

export default function ConsoleView({ account, onBack, onOpenDrive }: {
  account: AwsAccount
  onBack: () => void
  /** Open the drive page for this account. Owned by `AwsControlPage`, which
   *  holds the app's view state, so the console does not nest a second page
   *  inside itself. */
  onOpenDrive: (drive: LiveDrive) => void
}) {
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
  // The drive read's refusal states, named once so the receipt below and the ask
  // further down cannot drift apart.
  const driveErr = driveQ.error instanceof AwsControlError ? driveQ.error : null
  const drive409 = driveQ.isError && driveErr?.status === 409 ? driveErr : null
  const driveConsentRefused = drive409?.message === 'aws_consent_required'
  // A receipt belongs on THIS console only when the grant it shows was recorded
  // for THIS account. A grant is service-scoped and carries the account it was
  // confirmed for, so mounting it under every console would claim a scope it
  // does not have AND put a withdraw control for one account's drive on another
  // account's page - the withdraw is global, so that misfire is destructive, not
  // cosmetic.
  //
  // It is also suppressed while that service's own refusal is still on screen:
  // granting invalidates the consent query but not the drive or costs caches, so
  // for the renders between a grant and the next refetch the ask and the receipt
  // would both be visible, saying opposite things about the same service.
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
  // and the drive row reads a cached listing, so a grant change has to reach
  // them or the page keeps rendering the previous answer.
  const refetchGated = () => {
    qcTop.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })
    qcTop.invalidateQueries({ queryKey: ['aws-control', 'costs', id] })
  }
  // Fallback region for the setup preview, sourced the same way GeneralSection
  // sources the one it displays: the default key's region, else the first key's.
  // The Payments row bills through this same key, so it reads its region and
  // credential kind from the one profile.
  const defaultProfile = account.profiles.find((p) => p.default) ?? account.profiles[0]
  const setupRegion = defaultProfile?.region ?? ''

  return (
    <div className="flex h-full flex-col">
      {/* Crumb + header: name + full account id (mono). */}
      <div className="px-4 pt-2 pb-3 md:px-6">
        <button
          onClick={onBack}
          className="mb-1 inline-flex items-center gap-1 text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
          data-testid="console-crumb"
        >
          <ChevronLeft size={14} />
          {i18nT('apps.awsControl.console.crumb_accounts')} / <span className="text-text">{accountName(account)}</span>
        </button>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {/* Three-state, not two. The deleted General card carried the full
              Connected / Degraded / Unknown label, and collapsing it into a
              binary `health === 'ok'` dot would announce "not connected" for a
              degraded account whose keys still partly work - a misdiagnosis for
              whoever is triaging a flaky key. The dot's colour keeps the same
              three states its title and aria-label now name. */}
          <span
            className={`h-2.5 w-2.5 rounded-full ${HEALTH_DOT[account.health] ?? 'bg-muted'}`}
            data-testid="console-health"
            role="img"
            title={i18nT(CONNECTION_LABEL_KEY[account.health] ?? 'apps.awsControl.console.connection_unknown')}
            aria-label={i18nT(CONNECTION_LABEL_KEY[account.health] ?? 'apps.awsControl.console.connection_unknown')}
          />
          <span className="text-lg font-semibold text-text-strong">{accountName(account)}</span>
          {id && (
            <>
              <span className="font-mono text-[13px] text-muted" data-testid="console-account-id">{id}</span>
              {/* The copy button came off the deleted General card; a 12-digit
                  account id is the field most often pasted elsewhere. */}
              <CopyBtn text={id} testId="console-copy-id" ariaLabel={i18nT('apps.awsControl.console.copy_id')} />
            </>
          )}
        </div>

        {/* No Payments control. Paid-service consent is SERVICE-scoped -- the
            endpoint takes a service and derives the connection from that
            service's own configuration -- so it cannot honestly sit on a
            per-account page, and it stays where its gate was designed to live.
            With consent gone, a panel here would have held only facts this same
            screen already states: the credential kind and region are the
            connection row's, the account id is the title's, and the
            month-to-date figure is a tile. Restating them behind a button is
            the duplication this change exists to remove. */}
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
        {/* Connections only. The General card that used to sit above it carried
            no field of its own: name (crumb + title), account id (title),
            region and key count (the connection rows below), and a connection
            state the title dot already shows — at a different precision, which
            was its own small lie. */}
        <ConnectionsSection account={account} />

        {/* Two tiles, both of which have a data source. SITES and TASKS were
            hardcoded em-dashes with no query behind them, and they said the
            same "connects later" as the ghost cards that used to close the
            page — four elements for two features that do not exist yet. */}
        {/* One stat, because there is one figure this page alone can state: the
            month-to-date bill. Stored bytes and object count used to sit beside
            it AND on the Cloud drive row below, which is the same fact twice on
            one screen - and the row is the better home, since that is the thing
            the number describes. */}
        <div className="mt-6 grid grid-cols-2 gap-3" data-testid="console-stats">
          {costs?.consentMissing ? (
            <StatCard label={i18nT('apps.awsControl.console.stat_this_month')} value="—" title={i18nT('apps.awsControl.console.costs_consent_missing')} />
          ) : costsQ.isError ? (
            // A failed bill read (Cost Explorer not enabled on the account,
            // network, throttle) must not skeleton forever — say "no number".
            <StatCard label={i18nT('apps.awsControl.console.stat_this_month')} value="—" title={i18nT('apps.awsControl.console.costs_unavailable')} />
          ) : (
            <StatCard
              label={i18nT('apps.awsControl.console.stat_this_month')}
              value={costs ? fmtCurrency(costs.monthToDate, costs.currency) : undefined}
              title={costs && !costs.fresh ? i18nT('apps.awsControl.console.costs_as_of', { date: fmtDate(costs.fetchedAt) }) : undefined}
            />
          )}
        </div>

        {driveQ.isLoading && <div className="mt-6"><ContentSkeleton rows={3} /></div>}

        {/* A 409 is not one condition: storage-not-confirmed renders the
            confirmation card (the fix is right here), while a dead
            connection points back at Reconnect on the Accounts page. */}
        {drive409 && (
          driveConsentRefused ? (
            <div className="mt-6" data-testid="console-storage-consent">
              <p className="mb-2 text-[13px] text-muted">{i18nT('apps.awsControl.console.storage_consent_needed')}</p>
              <AwsConsentGate service="s3" onConsentChange={refetchGated} />
              <div className="mt-2">
                <Btn onClick={() => qcTop.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })} data-testid="console-consent-recheck">
                  <RefreshCw size={13} />{i18nT('apps.awsControl.page.refresh')}
                </Btn>
              </div>
            </div>
          ) : (
            <p className="mt-6 text-[13px] text-muted" data-testid="console-unavailable">{i18nT('apps.awsControl.console.account_unavailable')}</p>
          )
        )}

        {/* The account's capabilities, one row each. Today there is exactly one,
            and a row appears only when the thing it names exists - the page used
            to close with two dashed "connects later" cards for features that did
            not, which is what this app is being cleaned of.

            The row is NAVIGATION, not a disclosure: the drive's contents (the
            artifact library, the files, the backups and the share ledger) were
            four sections stacked on this page, and they are a page of their own
            now. What belongs here is the one line that says the drive exists and
            how much is in it. */}
        {drive && (
          <div className="mt-6 overflow-hidden rounded-lg border border-border bg-card" data-testid="console-capabilities">
            {drive.exists ? (
              <button
                type="button"
                onClick={() => onOpenDrive(drive)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left cursor-pointer bg-transparent border-none hover:bg-bg-hover"
                data-testid="capability-drive"
              >
                <HardDrive size={15} className="shrink-0 text-accent" />
                <span className="text-[13px] font-medium text-text-strong">{i18nT('apps.awsControl.console.drive_title')}</span>
                <span className="min-w-0 truncate font-mono text-[12px] text-muted">{drive.bucket}</span>
                <span className="flex-1" />
                <span className="shrink-0 text-[12px] text-muted" data-testid="capability-drive-usage">
                  {i18nT('apps.awsControl.console.stat_stored_value', { size: fmtBytes(drive.usage.bytes), objects: drive.usage.objects })}
                </span>
                <ChevronRight size={14} className="shrink-0 text-muted" />
              </button>
            ) : (
              /* No bucket yet, so the row carries the one action that changes
                 that. The account's own default-profile region, not "" -- the
                 preview panel falls back to this when a backend response omits
                 its region, and a hardcoded empty string made that fallback
                 dead. */
              <div className="px-4 py-3" data-testid="capability-drive-setup">
                <SetupCard account={id} region={setupRegion} />
              </div>
            )}
          </div>
        )}

        {/* No app ghosts. Tasks and Sites were dashed "connects later" cards
            with no feature behind them, duplicating the two placeholder tiles
            that used to sit in the stats strip. A capability appears on this
            page when it exists. */}

        {/* Cost Explorer ask, driven by the CONSENT state rather than by
            `costs.consentMissing`. That field only arrives when the backend has
            a cached cost reading to attach it to; with no cache - the state a
            never-confirmed account is always in - the costs request is a bare
            409 and the field never exists, so keying the ask on it left Cost
            Explorer with no confirmation control anywhere in the product. */}
        {ceConsentQ.data?.granted === false && (
          <div className="mt-6" data-testid="costs-consent-gate">
            <AwsConsentGate service="ce" onConsentChange={refetchGated} />
          </div>
        )}

        {/* The confirmations recorded for THIS account, once each is granted and
            its ask has cleared. Each card is mounted on its own condition rather
            than the section's, because the two services are granted separately
            and a receipt for one must not be implied by the other. Withdrawing
            here revokes the one grant this account's drive and cost figure run
            on - which is why a grant recorded for a DIFFERENT account never
            renders here, and why this is the only surface that calls revoke for
            s3 and ce.

            `onConsentChange` is what makes a withdraw recoverable: the ask above
            is decided by a CACHED drive 409 and a cached `consentMissing`, so
            without invalidating them the receipt would unmount and no ask would
            take its place - a mistaken withdraw with nothing on screen offering
            the confirm back. */}
        {(s3Receipt || ceReceipt) && (
          <section className="mt-8" data-testid="paid-services">
            <h2 className="text-sm font-semibold text-text-strong">
              {i18nT('apps.awsControl.page.paid_services_title')}
            </h2>
            <div className="mt-3 flex flex-col gap-3">
              {s3Receipt && <AwsConsentGate service="s3" onConsentChange={refetchGated} />}
              {ceReceipt && <AwsConsentGate service="ce" onConsentChange={refetchGated} />}
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
