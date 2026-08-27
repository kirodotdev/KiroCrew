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
import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ChevronLeft, ChevronDown, RefreshCw, Copy, Check, HardDrive, Library,
  Archive, Share2, Download, Trash2, Upload, ShieldCheck, LayoutGrid, Globe,
  FolderClosed, FileText, X, MoreHorizontal, Star, Info, Link2, Code,
} from 'lucide-react'
import { Btn, Badge, StatCard, Toggle, Input, ContentSkeleton, IconButton } from '../../components/ui'
import AwsConsentGate from '../../components/AwsConsentGate'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtCurrency, fmtRelative, fmtDate } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import type {
  AwsAccount, AwsProfile, ProfileKind, ReconnectPlan, DriveSection, DriveStatus,
  ArtifactKind, LibraryArtifact, BackupKind, Share,
} from './types'

/** The name the console leads with: the backend name, or the "not connected" label. */
function accountName(account: AwsAccount): string {
  return account.name || i18nT('apps.awsControl.page.not_connected_yet')
}

/** Health-light token + label key for the account connection state. */
const HEALTH_DOT: Record<string, string> = { ok: 'bg-ok', degraded: 'bg-warn', unknown: 'bg-muted' }
const CONNECTION_LABEL_KEY: Record<string, string> = {
  ok: 'apps.awsControl.console.connection_connected',
  degraded: 'apps.awsControl.console.connection_degraded',
  unknown: 'apps.awsControl.console.connection_unknown',
}

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

/* Literal-key maps from enum → full catalog key, so no i18nT() call assembles a
 * key by interpolation (dynamicKeys gate): extractors and unused-key tooling
 * can then see every key, and a missing one fails the parity gate rather than
 * rendering raw. Mirrors UPDATE_ERROR_KEYS in pages/settings/AboutPanel.tsx. */
const KIND_LABEL_KEY: Record<ArtifactKind, string> = {
  widget: 'apps.awsControl.console.kind_widget',
  markdown: 'apps.awsControl.console.kind_markdown',
  html: 'apps.awsControl.console.kind_html',
  json: 'apps.awsControl.console.kind_json',
  webapp: 'apps.awsControl.console.kind_webapp',
  image: 'apps.awsControl.console.kind_image',
}

const EXPIRY_LABEL_KEY: Record<string, string> = {
  '1h': 'apps.awsControl.console.expiry_1h',
  '1d': 'apps.awsControl.console.expiry_1d',
  '7d': 'apps.awsControl.console.expiry_7d',
}

const SECTION_LABEL_KEY: Record<DriveSection, string> = {
  drive: 'apps.awsControl.console.section_drive',
  library: 'apps.awsControl.console.section_library',
  backup: 'apps.awsControl.console.section_backup',
}

const BACKUP_KIND_LABEL_KEY: Record<BackupKind, string> = {
  snapshot: 'apps.awsControl.console.backup_kind_snapshot',
  sessions: 'apps.awsControl.console.backup_kind_sessions',
}

/** Copy-to-clipboard button that flips to a check for ~1.5s. */
function CopyBtn({ text, testId }: { text: string; testId?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard unavailable — the text is still selectable by hand */ }
  }
  return (
    <Btn onClick={copy} data-testid={testId}>
      {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
      {copied ? i18nT('apps.awsControl.console.copied') : i18nT('apps.awsControl.console.copy')}
    </Btn>
  )
}

/* ── Section: General ────────────────────────────────────────────────────── */

/** One label/value pair in the General grid. */
function DetailRow({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="flex flex-col gap-0.5" data-testid={testId}>
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="text-[13px] text-text">{children}</dd>
    </div>
  )
}

/**
 * The General card: the account's identity at a glance. Name, full account id
 * (mono, with a copy button), the default profile's region, the connection
 * state with a health dot, and the number of keys.
 */
function GeneralSection({ account }: { account: AwsAccount }) {
  const defaultProfile = account.profiles.find((p) => p.default) ?? account.profiles[0]
  const region = defaultProfile?.region ?? ''
  return (
    <section
      className="rounded-lg border border-border bg-card px-4 py-4 shadow-sm"
      data-testid="general-section"
    >
      <SectionHeader icon={<Info size={15} />} title={i18nT('apps.awsControl.console.general')} />
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <DetailRow label={i18nT('apps.awsControl.console.name')} testId="general-name">
          <span className="font-medium text-text-strong">{accountName(account)}</span>
        </DetailRow>
        <DetailRow label={i18nT('apps.awsControl.console.account_id')} testId="general-account-id">
          {account.account ? (
            <span className="flex items-center gap-1.5">
              <span className="font-mono">{account.account}</span>
              <IconButton
                aria-label={i18nT('apps.awsControl.console.copy_id')}
                onClick={() => { void navigator.clipboard?.writeText(account.account).catch(() => {}) }}
                data-testid="general-copy-id"
              >
                <Copy size={13} />
              </IconButton>
            </span>
          ) : (
            <span className="text-muted">{i18nT('apps.awsControl.page.not_connected_yet')}</span>
          )}
        </DetailRow>
        <DetailRow label={i18nT('apps.awsControl.console.region')} testId="general-region">
          {region ? <span className="font-mono">{region}</span> : <span className="text-muted">—</span>}
        </DetailRow>
        <DetailRow label={i18nT('apps.awsControl.console.connection')} testId="general-connection">
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${HEALTH_DOT[account.health]}`} role="img" aria-label={i18nT(CONNECTION_LABEL_KEY[account.health])} />
            {i18nT(CONNECTION_LABEL_KEY[account.health])}
          </span>
        </DetailRow>
        <DetailRow label={i18nT('apps.awsControl.console.keys')} testId="general-keys">
          {i18nT('apps.awsControl.console.keys_count', { count: account.profiles.length })}
        </DetailRow>
      </dl>
    </section>
  )
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

/** A collapsible `</>` drawer: the bucket, a prefix, and a generic CLI line. */
function CliDrawer({ bucket, prefix }: { bucket: string; prefix: string }) {
  const [open, setOpen] = useState(false)
  const line = `aws s3 ls s3://${bucket}/${prefix}`
  return (
    <div className="mt-2" data-testid="cli-drawer">
      <button
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
        aria-expanded={open}
        data-testid="cli-drawer-toggle"
      >
        <Code size={12} />
        {i18nT('apps.awsControl.console.cli_drawer_label')}
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="mt-1.5 rounded-md border border-border bg-bg-elevated p-2.5 text-[12px]" data-testid="cli-drawer-body">
          <div className="text-muted mb-1">
            {i18nT('apps.awsControl.console.cli_drawer_hint', { bucket, prefix })}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">
              {line}
            </code>
            <CopyBtn text={line} />
          </div>
        </div>
      )}
    </div>
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

/* ── Section 4: Library ──────────────────────────────────────────────────── */

const KIND_KEYS: ArtifactKind[] = ['widget', 'markdown', 'html', 'json', 'webapp', 'image']

function LibrarySection({ account, bucket }: { account: string; bucket: string }) {
  const qc = useQueryClient()
  const [kind, setKind] = useState<ArtifactKind | 'all'>('all')
  const libQ = useQuery({
    queryKey: ['aws-control', 'library', account],
    queryFn: () => awsControlApi.library(account),
  })
  const pushMut = useMutation({
    mutationFn: (slug: string) => awsControlApi.libraryPush(account, slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'library', account] }),
  })

  const artifacts = libQ.data?.artifacts ?? []
  const counts: Record<string, number> = { all: artifacts.length }
  for (const k of KIND_KEYS) counts[k] = artifacts.filter((a) => a.kind === k).length
  const shown = kind === 'all' ? artifacts : artifacts.filter((a) => a.kind === kind)

  return (
    <section data-testid="library-section">
      <SectionHeader icon={<Library size={15} />} title={i18nT('apps.awsControl.console.library_title')} />
      <div className="mb-3 flex flex-wrap gap-1.5" data-testid="library-chips">
        {(['all', ...KIND_KEYS] as const).map((k) => (
          <button
            key={k}
            onClick={() => setKind(k)}
            className={`rounded-full border px-2.5 py-1 text-[12px] cursor-pointer transition-colors ${
              kind === k ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-transparent text-muted hover:text-text'
            }`}
            data-testid={`library-chip-${k}`}
          >
            {k === 'all' ? i18nT('apps.awsControl.console.library_all') : i18nT(KIND_LABEL_KEY[k])}{' '}
            <span className="font-mono opacity-70">{counts[k] ?? 0}</span>
          </button>
        ))}
      </div>

      {libQ.isLoading && <ContentSkeleton rows={2} />}

      {libQ.data && shown.length === 0 && (
        <p className="text-[13px] text-muted" data-testid="library-empty">
          {i18nT('apps.awsControl.console.library_empty')}
        </p>
      )}

      {shown.length > 0 && (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" data-testid="library-tiles">
          {shown.map((a) => (
            <LibraryTile key={a.slug} artifact={a} onPush={() => pushMut.mutate(a.slug)} pushing={pushMut.isPending && pushMut.variables === a.slug} />
          ))}
        </div>
      )}

      <CliDrawer bucket={bucket} prefix="artifacts/" />
    </section>
  )
}

function LibraryTile({ artifact, onPush, pushing }: { artifact: LibraryArtifact; onPush: () => void; pushing: boolean }) {
  const synced = artifact.pushedVersion !== null
  const upToDate = artifact.pushedVersion === artifact.version
  const notPushable = artifact.kind === 'image'
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2.5" data-testid="library-tile">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-medium text-text">{artifact.name}</span>
            <Badge variant="muted">{i18nT(KIND_LABEL_KEY[artifact.kind])}</Badge>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 text-[12px] text-muted">
            <span className="font-mono">v{artifact.version}</span>
            <span>{fmtRelative(artifact.updatedAt)}</span>
            <span className={synced ? 'text-ok' : 'text-muted'}>
              {synced
                ? i18nT('apps.awsControl.console.library_synced', { version: artifact.pushedVersion })
                : i18nT('apps.awsControl.console.library_not_synced')}
            </span>
          </div>
        </div>
        <Btn
          onClick={onPush}
          disabled={pushing || upToDate || notPushable}
          data-testid="library-push"
          title={notPushable ? i18nT('apps.awsControl.console.library_not_pushable') : undefined}
        >
          <Upload size={13} />
          {upToDate
            ? i18nT('apps.awsControl.console.library_up_to_date')
            : i18nT('apps.awsControl.console.library_push')}
        </Btn>
      </div>
    </div>
  )
}

/* ── Section 5: Drive (folder browser) ───────────────────────────────────── */

/** Client-side key-segment validation, matching the backend's charset rule. */
const KEY_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9 ._()+@=-]*$/

function DriveSectionView({ account, bucket }: { account: string; bucket: string }) {
  const qc = useQueryClient()
  const [path, setPath] = useState('')
  const [token, setToken] = useState('')
  const [share, setShare] = useState<{ key: string } | null>(null)
  const [uploadError, setUploadError] = useState('')
  const [downloadError, setDownloadError] = useState('')
  const [menuFor, setMenuFor] = useState<string | null>(null)
  const [crumbMenu, setCrumbMenu] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const listQ = useQuery({
    queryKey: ['aws-control', 'drive', account, 'list', path, token],
    queryFn: () => awsControlApi.driveList(account, 'drive', path, token),
  })
  const invalidate = () => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', account] })

  const uploadMut = useMutation({
    mutationFn: (file: File) =>
      awsControlApi.driveUpload(account, 'drive', path ? `${path}/${file.name}` : file.name, file),
    onSuccess: invalidate,
  })
  const deleteMut = useMutation({
    mutationFn: (key: string) => awsControlApi.driveDelete(account, 'drive', key),
    onSuccess: invalidate,
  })

  const onPick = (file: File | undefined) => {
    if (!file) return
    setUploadError('')
    if (!KEY_SEGMENT.test(file.name)) {
      setUploadError(i18nT('apps.awsControl.console.drive_bad_name'))
      return
    }
    uploadMut.mutate(file)
  }

  const download = async (key: string) => {
    // Open the tab SYNCHRONOUSLY, inside the click's user activation, then
    // navigate it once the presign returns. Awaiting first and calling
    // window.open afterwards spends the activation on the await, and Safari
    // (and Chrome, with popups restricted) blocks the resulting window - the
    // Download button silently does nothing.
    //
    // Deliberately NO 'noopener' feature here: per the HTML standard a
    // window.open carrying it returns NULL, which made the earlier version of
    // this fix a no-op -- the handle was always null, so every download fell
    // through to the post-await open it was written to avoid, and the test that
    // covered it passed only because it MOCKED window.open into returning a
    // tab. The isolation noopener buys is restored on the next line by nulling
    // `opener` on the window we just got: same guarantee, handle kept.
    setDownloadError('')
    const tab = window.open('', '_blank')
    if (tab) tab.opener = null
    try {
      const { url } = await awsControlApi.driveDownload(account, 'drive', key)
      if (tab) tab.location.href = url
      else window.open(url, '_blank', 'noopener')
    } catch {
      // Never leave an orphaned blank tab behind, and never rethrow: this runs
      // from an onClick with no catch, so a rethrow becomes an unhandled
      // rejection that tells the USER nothing. Report it in the row instead.
      tab?.close()
      setDownloadError(i18nT('apps.awsControl.console.download_failed'))
    }
  }

  const crumbs = path.split('/').filter(Boolean)

  return (
    <section data-testid="drive-section">
      <SectionHeader icon={<HardDrive size={15} />} title={i18nT('apps.awsControl.console.drive_title')} actions={
        <Btn onClick={() => fileRef.current?.click()} disabled={uploadMut.isPending} data-testid="drive-upload-btn">
          <Upload size={13} />
          {uploadMut.isPending ? i18nT('apps.awsControl.console.drive_uploading') : i18nT('apps.awsControl.console.drive_upload')}
        </Btn>
      } />
      <input
        ref={fileRef}
        type="file"
        className="hidden"
        aria-label={i18nT('apps.awsControl.console.drive_upload')}
        data-testid="drive-file-input"
        onChange={(e) => onPick(e.target.files?.[0])}
      />

      {uploadError && <p className="mb-2 text-[12px] text-danger" data-testid="drive-upload-error">{uploadError}</p>}
      {downloadError && <p className="mb-2 text-[12px] text-danger" data-testid="drive-download-error">{downloadError}</p>}

      {/* Breadcrumb within the section. Root plus one overflow is two sibling
          controls; the folder you are IN is text, not a third button. The
          ancestors go into the same inline overflow the file rows use, which
          keeps the jump-to-an-ancestor navigation that rendering the whole path
          as flat text would have removed. */}
      <div className="mb-2 flex flex-wrap items-center gap-1 text-[12px] text-muted" data-testid="drive-crumbs">
        <button className="hover:text-text cursor-pointer bg-transparent border-none p-0" onClick={() => { setPath(''); setToken('') }}>
          {i18nT('apps.awsControl.console.drive_root')}
        </button>
        {crumbs.length > 1 && (
          <span className="relative flex items-center gap-1">
            {' / '}
            <IconButton
              aria-label={i18nT('apps.awsControl.console.parent_folders')}
              onClick={() => setCrumbMenu((v) => !v)}
              data-testid="drive-crumb-more"
            >
              <MoreHorizontal size={14} />
            </IconButton>
            {crumbMenu && (
              <div className="absolute left-0 top-full z-10 mt-1 flex flex-col gap-1 rounded-md border border-border bg-card p-1 shadow-md" data-testid="drive-crumb-menu">
                {crumbs.slice(0, -1).map((c, i) => (
                  <Btn
                    key={i}
                    onClick={() => {
                      setCrumbMenu(false)
                      setPath(crumbs.slice(0, i + 1).join('/'))
                      setToken('')
                    }}
                  >
                    {c}
                  </Btn>
                ))}
              </div>
            )}
          </span>
        )}
        {crumbs.length > 0 && (
          <span data-testid="drive-crumb-current">{' / '}{crumbs[crumbs.length - 1]}</span>
        )}
      </div>

      {listQ.isLoading && <ContentSkeleton rows={2} />}

      {listQ.data && (
        <div className="rounded-md border border-border bg-card divide-y divide-border" data-testid="drive-listing">
          {listQ.data.folders.map((name) => (
            <button
              key={`f-${name}`}
              onClick={() => { setPath(`${name}`); setToken('') }}
              className="flex w-full items-center gap-2 px-3 py-2 text-left text-[13px] text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none"
              data-testid="drive-folder"
            >
              <FolderClosed size={14} className="text-muted" />
              <span className="truncate">{name}</span>
            </button>
          ))}
          {listQ.data.files.map((f) => (
            <div key={`o-${f.key}`} data-testid="drive-file">
              <div className="flex items-center gap-2 px-3 py-2 text-[13px]">
              <FileText size={14} className="text-muted shrink-0" />
              <span className="min-w-0 flex-1 truncate text-text">{f.key.split('/').pop()}</span>
              <span className="hidden shrink-0 text-muted sm:inline">{fmtBytes(f.size)}</span>
              <span className="hidden shrink-0 text-muted md:inline">{fmtRelative(f.modified)}</span>
              <div className="relative flex shrink-0 items-center gap-1">
                <Btn onClick={() => download(f.key)} data-testid="drive-download"><Download size={13} />{i18nT('apps.awsControl.console.download')}</Btn>
                {/* One primary action inline; everything else lives in the
                    overflow so a row never exceeds two sibling controls. */}
                <IconButton
                  aria-label={i18nT('apps.awsControl.console.more_actions')}
                  onClick={() => setMenuFor(menuFor === f.key ? null : f.key)}
                  data-testid="drive-more"
                >
                  <MoreHorizontal size={14} />
                </IconButton>
                {menuFor === f.key && (
                  <div className="absolute right-0 top-full z-10 mt-1 flex flex-col gap-1 rounded-md border border-border bg-card p-1 shadow-md" data-testid="drive-more-menu">
                    <Btn
                      onClick={() => { setMenuFor(null); setShare({ key: f.key }) }}
                      data-testid="drive-share"
                    >
                      <Share2 size={13} />{i18nT('apps.awsControl.console.share')}
                    </Btn>
                    <Btn
                      danger
                      onClick={() => { setMenuFor(null); setConfirmDelete(f.key) }}
                      data-testid="drive-delete"
                    >
                      <Trash2 size={13} />{i18nT('apps.awsControl.console.delete')}
                    </Btn>
                  </div>
                )}
              </div>
              </div>
              {confirmDelete === f.key && (
                <div className="flex flex-wrap items-center gap-2 border-t border-border bg-bg-elevated px-3 py-2 text-[13px]" data-testid="drive-delete-confirm">
                  <span className="min-w-0 flex-1 text-text">
                    {i18nT('apps.awsControl.console.delete_confirm', { name: f.key.split('/').pop() ?? f.key })}
                  </span>
                  {deleteMut.isError && (
                    <span className="text-danger" data-testid="drive-delete-error">
                      {i18nT('apps.awsControl.console.delete_failed')}
                    </span>
                  )}
                  <Btn onClick={() => setConfirmDelete(null)} data-testid="drive-delete-cancel">
                    {i18nT('apps.awsControl.console.cancel')}
                  </Btn>
                  <Btn
                    danger
                    disabled={deleteMut.isPending}
                    onClick={() => deleteMut.mutate(f.key, { onSuccess: () => setConfirmDelete(null) })}
                    data-testid="drive-delete-confirm-action"
                  >
                    <Trash2 size={13} />{i18nT('apps.awsControl.console.delete_confirm_action')}
                  </Btn>
                </div>
              )}
            </div>
          ))}
          {listQ.data.folders.length === 0 && listQ.data.files.length === 0 && (
            <p className="px-3 py-3 text-[13px] text-muted" data-testid="drive-empty">{i18nT('apps.awsControl.console.drive_empty')}</p>
          )}
        </div>
      )}

      {listQ.data?.nextToken && (
        <div className="mt-2">
          <Btn onClick={() => setToken(listQ.data!.nextToken!)} data-testid="drive-load-more">{i18nT('apps.awsControl.console.load_more')}</Btn>
        </div>
      )}

      <CliDrawer bucket={bucket} prefix="drive/" />

      {share && (
        <ShareDialog account={account} section="drive" fileKey={share.key} onClose={() => setShare(null)} />
      )}
    </section>
  )
}

/* ── Share dialog ────────────────────────────────────────────────────────── */

const EXPIRY_OPTIONS: Array<{ key: string; secs: number }> = [
  { key: '1h', secs: 3600 },
  { key: '1d', secs: 86400 },
  { key: '7d', secs: 604800 },
]

function ShareDialog({ account, section, fileKey, onClose }: { account: string; section: DriveSection; fileKey: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [secs, setSecs] = useState(3600)
  const [note, setNote] = useState('')
  const shareMut = useMutation({
    mutationFn: () => awsControlApi.driveShare(account, section, fileKey, secs, note),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'shares', account] }),
  })
  const url = shareMut.data?.url

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" data-testid="share-dialog" role="dialog" aria-modal="true">
      <div className="w-full max-w-md rounded-lg border border-border bg-card p-4 shadow-lg">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-text-strong">{i18nT('apps.awsControl.console.share_title')}</h3>
          <button onClick={onClose} className="text-muted hover:text-text cursor-pointer bg-transparent border-none p-0" aria-label={i18nT('apps.awsControl.console.close')} data-testid="share-close"><X size={16} /></button>
        </div>

        {!url ? (
          <>
            <span className="mb-1 block text-[12px] text-muted">{i18nT('apps.awsControl.console.share_expiry')}</span>
            <div className="mb-3 flex gap-1.5" data-testid="share-expiry" role="group" aria-label={i18nT('apps.awsControl.console.share_expiry')}>
              {EXPIRY_OPTIONS.map((o) => (
                <button
                  key={o.key}
                  onClick={() => setSecs(o.secs)}
                  aria-pressed={secs === o.secs}
                  className={`rounded-md border px-2.5 py-1 text-[13px] cursor-pointer transition-colors ${secs === o.secs ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-transparent text-muted hover:text-text'}`}
                  data-testid={`share-expiry-${o.key}`}
                >
                  {i18nT(EXPIRY_LABEL_KEY[o.key])}
                </button>
              ))}
            </div>
            {/* eslint-disable-next-line jsx-a11y/label-has-for -- deprecated rule can't see the htmlFor→id link to the custom Input control; label-has-associated-control is satisfied. */}
            <label htmlFor="aws-share-note" className="mb-1 block text-[12px] text-muted">{i18nT('apps.awsControl.console.share_note')}</label>
            <Input id="aws-share-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={i18nT('apps.awsControl.console.share_note_placeholder')} className="mb-3 w-full" data-testid="share-note" />
            <Btn primary onClick={() => shareMut.mutate()} disabled={shareMut.isPending} data-testid="share-create">
              {shareMut.isPending ? i18nT('apps.awsControl.console.share_creating') : i18nT('apps.awsControl.console.share_create')}
            </Btn>
          </>
        ) : (
          <div data-testid="share-result">
            <div className="mb-2 flex items-center gap-2">
              <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">{url}</code>
              <CopyBtn text={url} testId="share-copy" />
            </div>
            <p className="text-[12px] text-muted">{i18nT('apps.awsControl.console.share_expires_note')}</p>
            <p className="mt-1 text-[12px] text-muted">{i18nT('apps.awsControl.console.share_credentials_caveat')}</p>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Section 6: Backup ───────────────────────────────────────────────────── */

const BACKUP_KINDS: BackupKind[] = ['snapshot', 'sessions']

function BackupSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const [showRemote, setShowRemote] = useState(false)
  const backupQ = useQuery({
    queryKey: ['aws-control', 'backup', account],
    queryFn: () => awsControlApi.backup(account),
  })
  const invalidate = () => qc.invalidateQueries({ queryKey: ['aws-control', 'backup', account] })
  const runMut = useMutation({
    mutationFn: (kind: BackupKind) => awsControlApi.backupRun(account, kind),
    onSuccess: invalidate,
  })
  const nightlyMut = useMutation({
    mutationFn: (enabled: boolean) => awsControlApi.backupNightly(account, enabled),
    onSuccess: invalidate,
  })
  const restoreMut = useMutation({
    mutationFn: (key: string) => awsControlApi.backupRestore(account, key),
  })

  const data = backupQ.data

  return (
    <section data-testid="backup-section">
      <SectionHeader icon={<Archive size={15} />} title={i18nT('apps.awsControl.console.backup_title')} />
      {backupQ.isLoading && <ContentSkeleton rows={2} />}
      {data && (
        <div className="rounded-md border border-border bg-card divide-y divide-border">
          {BACKUP_KINDS.map((kind) => {
            const run = data.runs[kind]
            const running = runMut.isPending && runMut.variables === kind
            return (
              <div key={kind} className="flex items-center gap-3 px-3 py-2.5" data-testid={`backup-row-${kind}`}>
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] font-medium text-text">{i18nT(BACKUP_KIND_LABEL_KEY[kind])}</div>
                  <div className="text-[12px] text-muted">
                    {run
                      ? i18nT('apps.awsControl.console.backup_last_run', { when: fmtRelative(run.at), size: fmtBytes(run.bytes) })
                      : i18nT('apps.awsControl.console.backup_never')}
                  </div>
                  {kind === 'sessions' && (
                    // The archive takes BOTH halves of a session, and the CLI
                    // half lives in a directory shared with any kiro-cli chat
                    // started outside Kiro Crew. Say so where the button is:
                    // the owner is choosing what leaves their machine.
                    <div className="text-[12px] text-muted" data-testid="backup-sessions-scope">
                      {i18nT('apps.awsControl.console.backup_sessions_scope')}
                    </div>
                  )}
                </div>
                <Btn onClick={() => runMut.mutate(kind)} disabled={running} data-testid={`backup-run-${kind}`}>
                  <RefreshCw size={13} className={running ? 'animate-spin' : ''} />
                  {running ? i18nT('apps.awsControl.console.backup_running') : i18nT('apps.awsControl.console.backup_run_now')}
                </Btn>
              </div>
            )
          })}
          <div className="flex items-center justify-between px-3 py-2.5" data-testid="backup-nightly">
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-text">{i18nT('apps.awsControl.console.backup_nightly')}</div>
              <div className="text-[12px] text-muted">{i18nT('apps.awsControl.console.backup_nightly_hint')}</div>
            </div>
            <Toggle checked={data.nightly} onChange={(v) => nightlyMut.mutate(v)} label={i18nT('apps.awsControl.console.backup_nightly')} />
          </div>
        </div>
      )}

      {data?.remoteError && (
        <p className="mt-2 text-[12px] text-muted" data-testid="backup-remote-error">{i18nT('apps.awsControl.console.backup_remote_error')}</p>
      )}

      {data?.remote && (
        <div className="mt-2">
          <button
            onClick={() => setShowRemote((v) => !v)}
            className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
            aria-expanded={showRemote}
            data-testid="backup-remote-toggle"
          >
            {i18nT('apps.awsControl.console.backup_archive')}
            <ChevronDown size={12} className={`transition-transform ${showRemote ? 'rotate-180' : ''}`} />
          </button>
          {showRemote && (
            <div className="mt-1.5 rounded-md border border-border bg-card divide-y divide-border" data-testid="backup-archive">
              {BACKUP_KINDS.flatMap((kind) => (data.remote?.[kind] ?? []).slice(0, 5).map((f) => (
                <div key={f.key} className="flex items-center gap-2 px-3 py-2 text-[12px]" data-testid="backup-archive-row">
                  <span className="min-w-0 flex-1 truncate font-mono text-text">{f.key}</span>
                  <span className="hidden shrink-0 text-muted sm:inline">{fmtBytes(f.size)}</span>
                  <Btn onClick={() => restoreMut.mutate(f.key)} disabled={restoreMut.isPending} data-testid="backup-restore"><Download size={13} />{i18nT('apps.awsControl.console.backup_restore')}</Btn>
                </div>
              )))}
            </div>
          )}
          {showRemote && (
            // The recommended least-privilege policy makes the backup prefix
            // write-only on purpose, so Restore is denied for anyone who pasted
            // exactly that tier. Say so where the button is instead of letting
            // them discover it as an AccessDenied.
            <p className="mt-1.5 text-[12px] text-muted" data-testid="backup-restore-caveat">
              {i18nT('apps.awsControl.console.backup_restore_caveat')}
            </p>
          )}
        </div>
      )}

      {restoreMut.data && (
        <div className="mt-2 rounded-md border border-border bg-bg-elevated p-2.5 text-[12px]" data-testid="backup-restored">
          <div className="mb-1 text-muted">{i18nT('apps.awsControl.console.backup_restored_note')}</div>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">{restoreMut.data.path}</code>
            <CopyBtn text={restoreMut.data.path} />
          </div>
        </div>
      )}
    </section>
  )
}

/* ── Section 7: Access (shares ledger) ───────────────────────────────────── */

function AccessSection({ account }: { account: string }) {
  const qc = useQueryClient()
  const sharesQ = useQuery({
    queryKey: ['aws-control', 'shares', account],
    queryFn: () => awsControlApi.shares(account),
  })
  const forgetMut = useMutation({
    mutationFn: (id: string) => awsControlApi.shareForget(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['aws-control', 'shares', account] }),
  })
  const shares = sharesQ.data?.shares ?? []

  return (
    <section data-testid="access-section">
      <SectionHeader icon={<Share2 size={15} />} title={i18nT('apps.awsControl.console.access_title')} />
      {sharesQ.isLoading && <ContentSkeleton rows={1} />}
      {sharesQ.data && shares.length === 0 && (
        <p className="text-[13px] text-muted" data-testid="access-empty">{i18nT('apps.awsControl.console.access_empty')}</p>
      )}
      {shares.length > 0 && (
        <div className="rounded-md border border-border bg-card divide-y divide-border" data-testid="access-list">
          {shares.map((s: Share) => (
            <div key={s.id} className="flex items-center gap-3 px-3 py-2.5 text-[13px]" data-testid="access-row">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate font-mono text-text">{s.key}</span>
                  <Badge variant="muted">{i18nT(SECTION_LABEL_KEY[s.section])}</Badge>
                </div>
                <div className="text-[12px] text-muted">
                  {s.note ? `${s.note} · ` : ''}
                  {i18nT('apps.awsControl.console.access_expires_in', { when: fmtRelative(s.expiresAt) })}
                </div>
              </div>
              <Btn onClick={() => forgetMut.mutate(s.id)} disabled={forgetMut.isPending} data-testid="access-forget">{i18nT('apps.awsControl.console.access_forget')}</Btn>
            </div>
          ))}
        </div>
      )}
      <p className="mt-2 text-[12px] text-muted">{i18nT('apps.awsControl.console.access_footer')}</p>
    </section>
  )
}

/* ── Section 8: app ghosts ───────────────────────────────────────────────── */

function AppGhost({ icon, title }: { icon: ReactNode; title: string }) {
  return (
    <div className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-5 text-center" data-testid="app-ghost">
      <div className="mb-1 flex justify-center text-muted opacity-40">{icon}</div>
      <div className="text-[13px] font-medium text-muted">{title}</div>
      <span className="mt-1.5 inline-block rounded-full border border-border px-2 py-0.5 text-[11px] text-muted">
        {i18nT('apps.awsControl.console.connects_later')}
      </span>
    </div>
  )
}

/* ── shared section header ───────────────────────────────────────────────── */

function SectionHeader({ icon, title, actions }: { icon: ReactNode; title: string; actions?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <h2 className="flex items-center gap-1.5 text-sm font-semibold text-text-strong">
        <span className="text-accent">{icon}</span>
        {title}
      </h2>
      {actions}
    </div>
  )
}

/* ── Console shell ───────────────────────────────────────────────────────── */

export default function ConsoleView({ account, onBack }: { account: AwsAccount; onBack: () => void }) {
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
  const connected = account.health === 'ok'
  // Fallback region for the setup preview, sourced the same way GeneralSection
  // sources the one it displays: the default key's region, else the first key's.
  const setupRegion =
    (account.profiles.find((p) => p.default) ?? account.profiles[0])?.region ?? ''

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
          <span className={`h-2.5 w-2.5 rounded-full ${connected ? 'bg-ok' : 'bg-warn'}`} data-testid="console-health" role="img" aria-label={connected ? i18nT('apps.awsControl.console.connected') : i18nT('apps.awsControl.console.not_connected')} />
          <span className="text-lg font-semibold text-text-strong">{accountName(account)}</span>
          {id && <span className="font-mono text-[13px] text-muted" data-testid="console-account-id">{id}</span>}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
        {/* Grouped detail: General + Connections (Reconnect lives here now). */}
        <div className="flex flex-col gap-6">
          <GeneralSection account={account} />
          <ConnectionsSection account={account} />
        </div>

        {/* Section 2: stats */}
        <div className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="console-stats">
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
          <StatCard
            label={i18nT('apps.awsControl.console.stat_stored')}
            value={drive?.exists ? i18nT('apps.awsControl.console.stat_stored_value', { size: fmtBytes(drive.usage.bytes), objects: drive.usage.objects }) : '—'}
          />
          <StatCard label={i18nT('apps.awsControl.console.stat_sites')} value="—" title={i18nT('apps.awsControl.console.connects_later')} />
          <StatCard label={i18nT('apps.awsControl.console.stat_tasks')} value="—" title={i18nT('apps.awsControl.console.connects_later')} />
        </div>
        <p className="mt-2 text-[12px] text-muted" data-testid="console-guard">{i18nT('apps.awsControl.console.guard_line')}</p>

        {driveQ.isLoading && <div className="mt-6"><ContentSkeleton rows={3} /></div>}

        {/* A 409 is not one condition: storage-not-confirmed renders the
            confirmation card (the fix is right here), while a dead
            connection points back at Reconnect on the Accounts page. */}
        {driveQ.isError && driveQ.error instanceof AwsControlError && driveQ.error.status === 409 && (
          driveQ.error.message === 'aws_consent_required' ? (
            <div className="mt-6" data-testid="console-storage-consent">
              <p className="mb-2 text-[13px] text-muted">{i18nT('apps.awsControl.console.storage_consent_needed')}</p>
              <AwsConsentGate service="s3" />
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

        {/* Drive-missing setup replaces sections 4-7 */}
        {drive && !drive.exists && (
          <div className="mt-6">
            {/* The account's own default-profile region, not "" -- the preview
                panel falls back to this when a backend response omits its
                region, and a hardcoded empty string made that fallback dead. */}
            <SetupCard account={id} region={setupRegion} />
          </div>
        )}

        {/* Drive present: the four sections */}
        {drive?.exists && (
          <div className="mt-6 flex flex-col gap-8">
            <LibrarySection account={id} bucket={drive.bucket} />
            <DriveSectionView account={id} bucket={drive.bucket} />
            <BackupSection account={id} />
            <AccessSection account={id} />
          </div>
        )}

        {/* Section 8: app ghosts (always shown) */}
        <div className="mt-8 grid grid-cols-1 gap-3 sm:grid-cols-2" data-testid="console-ghosts">
          <AppGhost icon={<LayoutGrid size={22} />} title={i18nT('apps.awsControl.console.ghost_tasks')} />
          <AppGhost icon={<Globe size={22} />} title={i18nT('apps.awsControl.console.ghost_sites')} />
        </div>

        {/* Cost Explorer consent nudge when the gate is missing. */}
        {costs?.consentMissing && (
          <div className="mt-6" data-testid="costs-consent-gate">
            <AwsConsentGate service="ce" />
          </div>
        )}
      </div>
    </div>
  )
}
