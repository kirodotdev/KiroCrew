/**
 * AWS Control — the flat-rail shell.
 *
 * The drive IS the product, so its four sections (Files, Library, Backup,
 * Share links) are the app's own first-level navigation, laid out as a rail
 * beside the content pane. The account is a card at the top of that rail with
 * a switcher; account management and the money facts sink to the rail's foot
 * (Accounts & credentials, Usage & costs) the way settings do. Opening the app
 * lands on Files — the most used surface — with no account picking in the way.
 *
 * Everything here is view state, not routes: `BuiltinAppRoute` resolves only
 * single-segment routes, so the active pane and the selected account are this
 * component's state. The selected account persists across visits so a
 * single-account operator never sees a chooser.
 *
 * The surface stays read-only over accounts (spec §2.3): every mutation lives
 * in the crew or a dashboard confirmation card. The only writes are the
 * paid-service consent gates, which are their own durable-state components.
 */
import { useEffect, useState, useMemo } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Cloud, RefreshCw, ChevronDown, ChevronRight, ChevronsUpDown, Search, Check,
  FolderClosed, Library, Archive, Share2, Users, Wallet,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Btn, EmptyState, ContentSkeleton, Input } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import AwsConsentGate from '../../components/AwsConsentGate'
import { NavBackBar } from '../../components/NavBackBar'
import { COARSE_TOUCH_TARGET, SUBNAV_PUSH_STATE, parsePathSegments } from '../../components/subNavParams'
import { useIsNarrowViewport } from '../../hooks/useIsMobile'
import { usePersistedString } from '../../hooks/usePersistedString'
import { api, type AwsConsentStatus } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtNumber } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import UsagePane, { ConnectionsSection, ReconnectAction, SetupCard } from './ConsoleView'
import { DriveSectionView, LibrarySection, BackupSection, AccessSection } from './DrivePage'
import { PaneHeader, AwsErrorNotice } from './shared'
import type { AwsAccount, AccountHealth, DriveStatus } from './types'

/** Tailwind token for each health light, keyed as an `as const` map (literal-safe). */
const HEALTH_DOT: Record<AccountHealth, string> = {
  ok: 'bg-ok',
  degraded: 'bg-warn',
  unknown: 'bg-muted',
}

const HEALTH_LABEL_KEY: Record<AccountHealth, string> = {
  ok: 'apps.awsControl.page.health_ok',
  degraded: 'apps.awsControl.page.health_degraded',
  unknown: 'apps.awsControl.page.health_unknown',
}

/** The name a row leads with: the backend name, or the "not connected" label. */
function accountName(account: AwsAccount): string {
  return account.name || i18nT('apps.awsControl.page.not_connected_yet')
}

/* ── the rail ────────────────────────────────────────────────────────────── */

/** The panes the rail can show. The four drive sections lead; the two
 *  management panes sink to the rail's foot. */
type RailPane = 'files' | 'library' | 'backup' | 'shares' | 'accounts' | 'usage'

/* Literal-key maps from pane → catalog key, so no i18nT() call assembles a key
 * by interpolation (dynamicKeys gate). The four drive panes reuse the section
 * names their own headers already render, so the rail item and the pane title
 * cannot drift to different names. */
const PANE_LABEL_KEY: Record<RailPane, string> = {
  files: 'apps.awsControl.console.section_files',
  library: 'apps.awsControl.console.section_library',
  backup: 'apps.awsControl.console.section_backup',
  shares: 'apps.awsControl.console.access_title',
  accounts: 'apps.awsControl.rail.accounts',
  usage: 'apps.awsControl.rail.usage',
}

const PANE_ICON: Record<RailPane, LucideIcon> = {
  files: FolderClosed,
  library: Library,
  backup: Archive,
  shares: Share2,
  accounts: Users,
  usage: Wallet,
}

const DRIVE_PANES: RailPane[] = ['files', 'library', 'backup', 'shares']
const FOOT_PANES: RailPane[] = ['accounts', 'usage']

/** One rail navigation item: icon, label, and an optional count on the right. */
function RailItem({ pane, count, active, onClick }: {
  pane: RailPane
  count?: number
  active: boolean
  onClick: () => void
}) {
  const Icon = PANE_ICON[pane]
  return (
    <button
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      data-testid={`rail-${pane}`}
      className={`flex w-full shrink-0 items-center gap-2.5 rounded-md border-none px-2.5 py-2 text-left text-[13px] cursor-pointer focus-ring md:shrink ${
        active ? 'bg-accent-subtle text-text-strong' : 'bg-transparent text-text hover:bg-bg-hover'
      }`}
    >
      <Icon size={15} className={`shrink-0 ${active ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate">{i18nT(PANE_LABEL_KEY[pane])}</span>
      {count !== undefined && (
        <span className="shrink-0 font-mono text-[11px] text-muted" data-testid={`rail-${pane}-count`}>
          {fmtNumber(count)}
        </span>
      )}
    </button>
  )
}

/**
 * The account card at the rail's top: health dot, name, id, and a switcher.
 *
 * The dropdown lists every RESOLVED account — an unresolved profile has no
 * account to select and is reached through Accounts & credentials, which is
 * the menu's last item. With one account the card still renders (it is the
 * pane's context, not a chooser), the menu just has one entry.
 */
function AccountSwitcher({ accounts, selected, onSelect, onManage }: {
  accounts: AwsAccount[]
  selected: AwsAccount
  onSelect: (id: string) => void
  onManage: () => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          className="mb-3 flex w-full items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2.5 text-left cursor-pointer hover:bg-bg-hover focus-ring"
          data-testid="account-switcher"
          aria-label={i18nT('apps.awsControl.rail.switch_account')}
        >
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${HEALTH_DOT[selected.health]}`}
            data-testid="switcher-health"
            data-health={selected.health}
            role="img"
            aria-label={i18nT(HEALTH_LABEL_KEY[selected.health])}
          />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] font-semibold text-text-strong" data-testid="switcher-name">
              {accountName(selected)}
            </span>
            <span className="block truncate font-mono text-[11px] text-muted" data-testid="switcher-id">
              {selected.account}
            </span>
          </span>
          <ChevronsUpDown size={14} className="shrink-0 text-muted" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        {accounts.map((a) => (
          <DropdownMenuItem
            key={a.account}
            onSelect={() => onSelect(a.account)}
            data-testid="switcher-option"
            data-account={a.account}
          >
            <span className={`h-2 w-2 shrink-0 rounded-full ${HEALTH_DOT[a.health]}`} aria-hidden="true" />
            <span className="min-w-0 truncate">{accountName(a)}</span>
            <span className="font-mono text-[11px] text-muted">{a.account}</span>
            {a.account === selected.account && <Check size={13} className="text-accent" aria-hidden="true" />}
          </DropdownMenuItem>
        ))}
        <DropdownMenuItem onSelect={onManage} data-testid="switcher-manage">
          <Users size={13} aria-hidden="true" />
          {i18nT('apps.awsControl.rail.accounts')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/* ── Accounts & credentials pane ─────────────────────────────────────────── */

/**
 * One thin account row (~40px). Leads with a health dot and the account name,
 * then the full 12-digit id (mono, muted), and on the right a keys summary. A
 * resolved row SELECTS that account for the whole app (rail card, drive panes,
 * usage). An UNRESOLVED row cannot be selected (there is no account behind it),
 * so its click toggles the inline Reconnect guidance instead — a red row must
 * always offer a way back to green.
 */
function AccountRow({ account, current, onUse, askAgent }: {
  account: AwsAccount
  current: boolean
  onUse: () => void
  /** Whether this row's Reconnect notice may hand off to the agent; the pane decides. */
  askAgent: boolean
}) {
  const keys = account.profiles.length
  const resolved = Boolean(account.account)
  const [showReconnect, setShowReconnect] = useState(false)
  return (
    <div>
      <button
        onClick={resolved ? onUse : () => setShowReconnect((v) => !v)}
        className="flex w-full items-center gap-3 px-3 py-2 text-left cursor-pointer bg-transparent border-none hover:bg-bg-hover focus-ring"
        data-testid="account-card"
        data-current={current || undefined}
        aria-label={i18nT(resolved ? 'apps.awsControl.rail.use_account' : 'apps.awsControl.page.reconnect')}
        aria-expanded={resolved ? undefined : showReconnect}
      >
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${HEALTH_DOT[account.health]}`}
          data-testid="health-dot"
          data-health={account.health}
          role="img"
          aria-label={i18nT(HEALTH_LABEL_KEY[account.health])}
        />
        <span className="min-w-0 shrink-0 max-w-[45%] truncate text-[13px] font-semibold text-text-strong" data-testid="account-name">
          {accountName(account)}
        </span>
        {/* A word, not just a colour: the dot alone made a degraded account
            distinguishable only by hue. Healthy rows stay quiet — the word
            appears exactly when something needs attention. min-w-0 + truncate,
            not shrink-0: a fixed-width label at 320px pushes the keys count
            off the clipped row (longest German label measured). */}
        {account.health !== 'ok' && (
          <span className="min-w-0 shrink truncate text-[12px] text-warn" data-testid="account-health-word">
            {i18nT(HEALTH_LABEL_KEY[account.health])}
          </span>
        )}
        {account.account && (
          <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted" data-testid="account-id">
            {account.account}
          </span>
        )}
        {!account.account && <span className="flex-1" />}
        <span className="shrink-0 text-[12px] text-muted" data-testid="account-keys">
          {i18nT('apps.awsControl.page.keys_summary', { count: keys })}
        </span>
        {/* The row's affordance: the check marks the account the app is
            currently on; other resolved rows show nothing and select on
            click; unresolved rows disclose Reconnect. */}
        {current ? (
          <Check size={14} className="shrink-0 text-accent" aria-hidden="true" data-testid="account-current" />
        ) : !resolved ? (
          <ChevronDown size={14} className={`shrink-0 text-muted transition-transform ${showReconnect ? 'rotate-180' : ''}`} aria-hidden="true" />
        ) : null}
      </button>
      {!resolved && showReconnect && account.profiles[0] && (
        <div className="px-3 pb-2" data-testid="row-reconnect">
          <ReconnectAction profile={account.profiles[0]} askAgent={askAgent} />
        </div>
      )}
    </div>
  )
}

/**
 * An "Add accounts" disclosure: lists the LOCAL profiles the CLI knows but the
 * portal has not registered, each with a checkbox, and registers the checked
 * set. It stays collapsed by default so the account list remains the pane's
 * primary content. On success it invalidates the accounts query so a newly
 * registered profile appears without a manual refresh.
 */
function AddAccounts({ onDraftChange }: {
  /**
   * Fires with `true` while at least one profile is ticked and not yet
   * registered, `false` once the selection is empty again. The ticks live only
   * in this component's state, so anything on the pane that navigates away —
   * an agent hand-off on a sibling notice — would drop them; the pane uses this
   * to withhold those hand-offs while a selection is open.
   */
  onDraftChange: (hasDraft: boolean) => void
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  // The set of profile NAMES the operator has ticked. Names, not indices, so a
  // list refetch that reorders rows can't silently move a checkmark to another
  // profile — registering the wrong profile is a trust error, not a UI glitch.
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const hasDraft = checked.size > 0
  useEffect(() => {
    onDraftChange(hasDraft)
  }, [hasDraft, onDraftChange])

  const availableQ = useAvailableProfilesQuery()

  const registerM = useMutation({
    mutationFn: (names: string[]) => awsControlApi.registerProfiles(names),
    onSuccess: () => {
      // The account list is keyed ['aws-control','accounts']; invalidating it is
      // what makes the just-registered profile show up without a manual refresh.
      queryClient.invalidateQueries({ queryKey: ['aws-control', 'accounts'] })
      queryClient.invalidateQueries({ queryKey: ['aws-control', 'profiles-available'] })
      setChecked(new Set())
    },
  })

  const data = availableQ.data
  const unregistered = (data?.profiles ?? []).filter((p) => !p.registered)
  const capReached = data ? data.registeredCount >= data.max : false
  // Disabled unless at least one box is ticked AND there is still headroom under
  // the registry cap — the backend enforces the cap too, but the button should
  // not invite a request it will only partially honour.
  const canRegister = checked.size > 0 && !capReached && !registerM.isPending

  const toggle = (name: string) =>
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  // Unsupported platform (Windows): an empty list means "can't tell", so say so
  // rather than rendering a picker that would imply the operator has no profiles.
  if (data && !data.supported) {
    return (
      <section className="mt-8" data-testid="add-accounts">
        <h2 className="text-sm font-semibold text-text-strong">
          {i18nT('apps.awsControl.page.add_accounts_title')}
        </h2>
        <p className="mt-1 text-[13px] text-muted" data-testid="add-accounts-unsupported">
          {i18nT('apps.awsControl.page.add_accounts_unsupported')}
        </p>
      </section>
    )
  }

  return (
    <section className="mt-8" data-testid="add-accounts">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 bg-transparent border-none p-0 text-left cursor-pointer focus-ring"
        data-testid="add-accounts-toggle"
        aria-expanded={open}
      >
        <ChevronDown size={14} className={`shrink-0 text-muted transition-transform ${open ? '' : '-rotate-90'}`} aria-hidden="true" />
        <span className="text-sm font-semibold text-text-strong">
          {i18nT('apps.awsControl.page.add_accounts_title')}
        </span>
        <span className="text-[12px] text-muted">
          {i18nT('apps.awsControl.page.add_accounts_summary')}
        </span>
      </button>

      {open && (
        <div className="mt-3" data-testid="add-accounts-body">
          {/* A failed profile scan is not "no profiles to add": without this the
              disclosure opened onto the none-left sentence, which asserts the
              opposite of what happened. */}
          <AwsErrorNotice
            askAgent={!hasDraft}
            error={availableQ.error}
            message={availableQ.isError ? i18nT('apps.awsControl.page.add_accounts_load_error') : null}
            onRetry={() => availableQ.refetch()}
            className="mb-2"
            testId="add-accounts-load-error"
          />
          {data && (
            <p className="mb-2 text-[12px] text-muted" data-testid="add-accounts-count">
              {i18nT('apps.awsControl.page.add_accounts_count', {
                count: data.registeredCount,
                max: data.max,
              })}
            </p>
          )}

          {availableQ.isError ? null : unregistered.length === 0 ? (
            <p className="text-[13px] text-muted" data-testid="add-accounts-none">
              {i18nT('apps.awsControl.page.add_accounts_none')}
            </p>
          ) : (
            <>
              <p className="mb-2 text-[13px] text-muted">
                {i18nT('apps.awsControl.page.add_accounts_intro')}
              </p>
              <ul className="flex flex-col gap-1" data-testid="add-accounts-list">
                {unregistered.map((p) => (
                  <li key={p.name}>
                    <label className="flex items-center gap-2 text-[13px] text-text-strong cursor-pointer">
                      <input
                        type="checkbox"
                        checked={checked.has(p.name)}
                        onChange={() => toggle(p.name)}
                        aria-label={p.name}
                        data-testid="add-accounts-checkbox"
                        data-name={p.name}
                      />
                      <span className="font-mono">{p.name}</span>
                    </label>
                  </li>
                ))}
              </ul>

              {capReached && (
                <p className="mt-2 text-[12px] text-warn" data-testid="add-accounts-cap">
                  {i18nT('apps.awsControl.page.add_accounts_cap_reached', { max: data?.max ?? 0 })}
                </p>
              )}

              {/* Never fail silently: a rejected register keeps its message on
                  screen so the operator knows nothing was added. No hand-off:
                  the ticked profiles are unsaved input, and the hand-off would
                  navigate away from them. */}
              <AwsErrorNotice
                error={registerM.error}
                message={registerM.isError ? i18nT('apps.awsControl.page.add_accounts_error') : null}
                className="mt-2"
                testId="add-accounts-error"
              />

              <Btn
                onClick={() => registerM.mutate([...checked])}
                disabled={!canRegister}
                primary
                className="mt-3"
                data-testid="add-accounts-register"
              >
                {registerM.isPending
                  ? i18nT('apps.awsControl.page.add_accounts_registering')
                  : i18nT('apps.awsControl.page.add_accounts_register')}
              </Btn>
            </>
          )}
        </div>
      )}
    </section>
  )
}

/**
 * The Accounts & credentials pane: every registered account as a selectable
 * row with a client-side search, the selected account's connection keys, the
 * orphaned-consent rescue, and the Add-accounts disclosure.
 */
function AccountsPane({ accountsQ, selected, onUse }: {
  accountsQ: ReturnType<typeof useAccountsQuery>
  selected: AwsAccount | null
  onUse: (account: AwsAccount) => void
}) {
  const [query, setQuery] = useState('')
  const data = accountsQ.data
  // Every hand-off on this pane is withheld while the Add-accounts disclosure
  // holds ticked-but-unregistered profiles: "Ask the agent" navigates to chat,
  // which unmounts the disclosure and drops the selection. The reconnect and
  // orphaned-consent notices are the sites; the register notice beside the
  // checkboxes never hands off. Same rule the Files pane applies to an open
  // folder-name field.
  const [registrationDraft, setRegistrationDraft] = useState(false)
  const handOff = !registrationDraft

  // The empty state's remedy is the Add-accounts disclosure further down this
  // same pane, so the two must agree about whether that disclosure can serve
  // this platform. On Windows profile discovery is unavailable and the
  // disclosure says so, which leaves a Windows operator permanently at zero
  // accounts -- an empty state still naming the disclosure would send them to a
  // paragraph that refuses. There the subtitle is DROPPED rather than replaced:
  // `empty_title` already says nothing is here, the disclosure carries the WSL
  // constraint once, and a replacement subtitle would only restate the title in
  // 12 catalogs. Undefined (still loading) reads as "can", so the ordinary
  // platform never waits on this to render its own copy.
  const canAddHere = useAvailableProfilesQuery().data?.supported !== false

  // Client-side filter over name + id; harmless when few accounts.
  const filtered = useMemo(() => {
    const rows = data?.accounts ?? []
    const q = query.trim().toLowerCase()
    if (!q) return rows
    return rows.filter(
      (a) => a.account.toLowerCase().includes(q) || a.name.toLowerCase().includes(q),
    )
  }, [data, query])

  // A grant is keyed on the SERVICE, so it outlives the account it was recorded
  // for. The usage pane only shows a receipt whose grant matches the SELECTED
  // account, which means a grant matching NO registered account has no surface
  // to live on and `revokeAwsConsent` has no caller anywhere — money confirmed
  // with no way to unconfirm it. Zero registered accounts is only one way to
  // reach that; deregistering the account a grant was recorded for while others
  // remain is another, so the condition is the general one rather than an empty
  // list. This mounts nothing whenever some registered account owns the grant,
  // which is the ordinary case.
  const s3ConsentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', 's3'],
    queryFn: () => api.awsConsent('s3'),
  })
  const ceConsentQ = useQuery<AwsConsentStatus>({
    queryKey: ['awsConsent', 'ce'],
    queryFn: () => api.awsConsent('ce'),
  })
  const orphaned = (c: AwsConsentStatus | undefined) => {
    const owner = c?.grant?.account
    if (c?.granted !== true || !owner) return false
    // Only once the LIST is known. An in-flight accounts query leaves `data`
    // undefined, and treating that as "no account owns this grant" would flash
    // a withdraw control onto the ordinary accounts pane on every load where
    // the consent read lands first — a destructive control offered by mistake.
    if (!accountsQ.isSuccess) return false
    return !(data?.accounts ?? []).some((a) => a.account === owner)
  }
  const s3Orphan = orphaned(s3ConsentQ.data)
  const ceOrphan = orphaned(ceConsentQ.data)

  return (
    <section data-testid="accounts-pane">
      <PaneHeader
        icon={<Users size={18} />}
        title={i18nT('apps.awsControl.rail.accounts')}
        actions={
          <Btn onClick={() => accountsQ.refetch()} disabled={accountsQ.isFetching} data-testid="refresh">
            <RefreshCw size={13} className={accountsQ.isFetching ? 'animate-spin' : ''} />
            {i18nT('apps.awsControl.page.refresh')}
          </Btn>
        }
      />

      {/* Accounts and a client-side search over them. The strip on the left
          answers "how much is connected and is it healthy" at a glance —
          counts the backend already sends — while the list below stays the
          pane's primary content. */}
      <div className="flex flex-wrap items-center justify-between gap-2" data-testid="accounts-aggregate">
        {data?.totals ? (
          <p className="text-[13px] text-muted" data-testid="accounts-totals">
            {i18nT('apps.awsControl.page.totals_summary', {
              accounts: fmtNumber(data.totals.accounts),
              keys: fmtNumber(data.totals.profiles),
              healthy: fmtNumber(data.totals.profilesHealthy),
            })}
          </p>
        ) : <span />}
        <div className="relative">
          <Search size={13} className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted" aria-hidden="true" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={i18nT('apps.awsControl.page.search_placeholder')}
            aria-label={i18nT('apps.awsControl.page.search_placeholder')}
            className="w-48 pl-7"
            data-testid="accounts-search"
          />
        </div>
      </div>

      {accountsQ.isLoading && (
        <div className="mt-4" data-testid="accounts-loading">
          <ContentSkeleton rows={3} />
        </div>
      )}

      {data && data.accounts.length === 0 && (
        <div className="mt-4" data-testid="accounts-empty">
          <EmptyState
            testId="aws-control-empty"
            icon={<Cloud />}
            title={i18nT('apps.awsControl.page.empty_title')}
            subtitle={canAddHere ? i18nT('apps.awsControl.page.empty_body') : undefined}
          />
        </div>
      )}

      {data && data.accounts.length > 0 && filtered.length === 0 && (
        <p className="mt-4 text-[13px] text-muted" data-testid="accounts-search-empty">
          {i18nT('apps.awsControl.page.search_none', { query: query.trim() })}
        </p>
      )}

      {data && filtered.length > 0 && (
        <div
          className="mt-4 overflow-hidden rounded-lg border border-border bg-card divide-y divide-border"
          data-testid="accounts-list"
        >
          {filtered.map((a, i) => (
            <AccountRow
              key={a.account || `unresolved-${i}`}
              account={a}
              current={Boolean(selected && a.account === selected.account)}
              onUse={() => onUse(a)}
              askAgent={handOff}
            />
          ))}
        </div>
      )}

      {/* The selected account's connection keys, with Reconnect on a failing
          one. Credentials are this pane's subject, so the section lives here
          rather than on a page of its own. */}
      {selected && (
        <div className="mt-8" data-testid="accounts-connections">
          <ConnectionsSection account={selected} askAgent={handOff} />
        </div>
      )}

      {/* Not a section but a rescue: a grant whose recorded account is not
          registered here has no usage pane to appear on, so without this it
          could never be withdrawn. It renders only in that state. */}
      {(s3Orphan || ceOrphan) && (
        <div className="mt-6 flex flex-col gap-3" data-testid="orphan-consent">
          {/* This state needs its sentence more than any other surface here:
              the card names an AWS account that matches nothing in the list
              above it, and its only control is destructive. */}
          <p className="text-[13px] text-text" data-testid="orphan-consent-note">
            {i18nT('apps.awsControl.page.orphan_consent')}
          </p>
          {s3Orphan && <AwsConsentGate service="s3" askAgent={handOff} />}
          {ceOrphan && <AwsConsentGate service="ce" askAgent={handOff} />}
        </div>
      )}

      <AddAccounts onDraftChange={setRegistrationDraft} />
    </section>
  )
}

/* ── the shell ───────────────────────────────────────────────────────────── */

/** The accounts query, named so `AccountsPane` can type its prop off it. */
function useAccountsQuery() {
  return useQuery({
    queryKey: ['aws-control', 'accounts'],
    queryFn: () => awsControlApi.accounts(),
  })
}

/**
 * The local-profile scan, shared by the Add-accounts disclosure and by the
 * accounts empty state.
 *
 * One hook rather than a `useQuery` at each site, because the two have to agree
 * about `supported`: the empty state's copy names an action whose ONLY home is
 * that disclosure, so an empty state that names it while the disclosure reports
 * the platform cannot serve it is a promise the next paragraph refuses. Sharing
 * the key already shares React Query's cache entry, so the second reader costs
 * no request.
 */
function useAvailableProfilesQuery() {
  return useQuery({
    queryKey: ['aws-control', 'profiles-available'],
    queryFn: () => awsControlApi.availableProfiles(),
  })
}

/**
 * A drive-backed pane, gated on the drive actually existing.
 *
 * Loading skeletons, the storage-consent ask (a 409 whose fix is right here),
 * the dead-connection notice, and the setup card all render under the pane's
 * own title, so the rail selection and the pane header always agree about
 * where the reader is even when the drive is not there yet.
 */
function DrivePaneGate({ pane, account, drive, driveQ, children }: {
  pane: RailPane
  account: AwsAccount
  drive: DriveStatus | undefined
  driveQ: { isLoading: boolean; isError: boolean; error: unknown }
  children: (bucket: string) => React.ReactNode
}) {
  const qc = useQueryClient()
  const id = account.account
  const Icon = PANE_ICON[pane]
  const driveErr = driveQ.error instanceof AwsControlError ? driveQ.error : null
  const drive409 = driveQ.isError && driveErr?.status === 409 ? driveErr : null
  const driveConsentRefused = drive409?.message === 'aws_consent_required'
  // Fallback region for the setup preview: the default key's region, else the
  // first key's — the same way the connections section sources the one it shows.
  const defaultProfile = account.profiles.find((p) => p.default) ?? account.profiles[0]
  const setupRegion = defaultProfile?.region ?? ''

  if (drive?.exists) return <>{children(drive.bucket)}</>

  return (
    <section data-testid={`gate-${pane}`}>
      <PaneHeader icon={<Icon size={18} />} title={i18nT(PANE_LABEL_KEY[pane])} />
      {driveQ.isLoading && <ContentSkeleton rows={3} />}
      {/* A 409 is not one condition: storage-not-confirmed renders the
          confirmation card (the fix is right here), while a dead connection
          points back at Reconnect on the accounts pane. */}
      {drive409 && (
        driveConsentRefused ? (
          <div data-testid="console-storage-consent">
            <p className="mb-2 text-[13px] text-muted">{i18nT('apps.awsControl.console.storage_consent_needed')}</p>
            <AwsConsentGate
              askAgent
              service="s3"
              onConsentChange={() => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })}
            />
            <div className="mt-2">
              <Btn onClick={() => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })} data-testid="console-consent-recheck">
                <RefreshCw size={13} />{i18nT('apps.awsControl.page.refresh')}
              </Btn>
            </div>
          </div>
        ) : (
          <AwsErrorNotice
            askAgent
            error={driveErr}
            message={i18nT('apps.awsControl.console.account_unavailable')}
            testId="console-unavailable"
          />
        )
      )}
      {/* Any other failure to read the drive. Left unrendered, a 5xx here showed
          the pane title over nothing at all — not loading, not empty, not
          broken — with no way to learn which. */}
      <AwsErrorNotice
        askAgent
        error={driveQ.error}
        message={driveQ.isError && !drive409 ? i18nT('apps.awsControl.console.drive_status_failed') : null}
        onRetry={() => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })}
        testId="drive-status-error"
      />
      {/* No bucket yet, so the pane carries the one action that changes that. */}
      {drive && !drive.exists && (
        <div className="rounded-lg border border-border bg-card px-4 py-3" data-testid="capability-drive-setup">
          <SetupCard account={id} region={setupRegion} />
        </div>
      )}
    </section>
  )
}

/** The app's own base path; pane routes hang off it (/aws-control/usage). */
const APP_PATH = '/aws-control'
const ALL_PANES: RailPane[] = [...DRIVE_PANES, ...FOOT_PANES]

/**
 * The pane named by the URL, or null on the bare app path.
 *
 * Read synchronously from the path (never normalized through an effect, which
 * would render the wrong pane for a frame before correcting itself), through
 * the SAME positional parser the settings path-nav uses — it already pins the
 * trailing-slash and empty-segment behavior (an empty segment stays in place
 * and matches no key) and guards the base path, so this app cannot re-derive
 * a divergent copy of those rules.
 */
function usePaneFromPath(): RailPane | null {
  const location = useLocation()
  const seg = parsePathSegments(APP_PATH, location.pathname)[0] ?? ''
  if ((ALL_PANES as string[]).includes(seg)) return seg as RailPane
  // Null means THE BARE PATH and nothing else. An unknown non-empty segment
  // falls back to Files on every width — mapping it to null would read the
  // same URL as Files on a desktop and as the root list on a phone, two
  // meanings for one address.
  return seg === '' ? null : 'files'
}

/**
 * One row of the narrow-viewport root list: icon, label, count, chevron.
 * iOS-style grouped list rows — the same navigation the settings root list
 * uses on a phone, so the two apps read as one product on small screens.
 */
function RootListRow({ pane, count, onOpen }: {
  pane: RailPane
  count?: number
  onOpen: () => void
}) {
  const Icon = PANE_ICON[pane]
  return (
    <button
      onClick={onOpen}
      data-testid={`root-${pane}`}
      className={`flex w-full items-center gap-3 px-3 py-2.5 ${COARSE_TOUCH_TARGET} text-left cursor-pointer bg-transparent border-none hover:bg-bg-hover focus-ring`}
    >
      <Icon size={16} className="shrink-0 text-accent" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate text-[14px] text-text-strong">{i18nT(PANE_LABEL_KEY[pane])}</span>
      {count !== undefined && (
        <span className="shrink-0 font-mono text-[12px] text-muted">{fmtNumber(count)}</span>
      )}
      <ChevronRight size={15} className="shrink-0 text-muted" aria-hidden="true" />
    </button>
  )
}

export default function AwsControlPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const paneFromPath = usePaneFromPath()
  const narrow = useIsNarrowViewport()
  // The selected account survives visits, so a single-account operator (and a
  // returning multi-account one) lands straight in their drive. An id that no
  // longer resolves falls back to the first resolved account rather than a
  // chooser.
  const [storedId, setStoredId] = usePersistedString('awsControl.selectedAccount', '')

  const accountsQ = useAccountsQuery()
  const data = accountsQ.data
  const accounts = data?.accounts ?? []
  const resolved = accounts.filter((a) => Boolean(a.account))
  const selected = resolved.find((a) => a.account === storedId) ?? resolved[0] ?? null
  const id = selected?.account ?? ''

  const driveQ = useQuery({
    queryKey: ['aws-control', 'drive', id],
    queryFn: () => awsControlApi.drive(id),
    enabled: Boolean(id),
  })
  const drive = driveQ.data
  // The share ledger's own query key, shared with `AccessSection`, so the rail
  // count and the pane listing can never disagree.
  const sharesQ = useQuery({
    queryKey: ['aws-control', 'shares', id],
    queryFn: () => awsControlApi.shares(id),
    enabled: Boolean(id),
  })

  // Narrow drill-in from the ROOT LIST is a PUSH carrying the same marker the
  // settings stack mints, so the platform back gesture pops one level exactly
  // like the on-screen back bar. Everything else (wide rail clicks, pane→pane
  // moves) REPLACES — walking every rail click on browser-back is not a
  // history the reader asked for. Mirrors SettingsSubNav's contract.
  const openPane = (p: RailPane) => {
    const drillIn = narrow && paneFromPath === null
    // A narrow pane->pane REPLACE must carry the current entry's push marker
    // forward: replacing a pushed entry with a marker-less one would make the
    // back bar replace-write a second root entry, and the next platform back
    // lands root->root — visibly inert. The marker describes the ENTRY's
    // provenance, and a replace keeps the entry.
    const keepMarker =
      narrow && !drillIn &&
      Boolean((location.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE])
    navigate(`${APP_PATH}/${p}`, {
      replace: !drillIn,
      state: drillIn || keepMarker ? { [SUBNAV_PUSH_STATE]: true } : undefined,
    })
  }
  const useAccount = (a: AwsAccount) => {
    setStoredId(a.account)
    openPane('files')
  }
  const paneCount = (p: RailPane): number | undefined =>
    p === 'shares'
      ? sharesQ.data?.shares.length
      : drive?.exists
        ? drive.usage.sections[p === 'files' ? 'drive' : p === 'library' ? 'library' : 'backup'].objects
        : undefined

  // A 403 app_disabled means the app was disabled after this bundle loaded (the
  // shell shows its own disabled state on first load). Show the standard
  // disabled-app copy rather than a raw error wall. Keyed on the CODE, not the
  // status: the same route answers 403 for a non-owner caller
  // (`dashboard_owner_required`), and that is an error to diagnose, not a
  // disabled app to wait out.
  if (
    accountsQ.isError &&
    accountsQ.error instanceof AwsControlError &&
    accountsQ.error.status === 403 &&
    accountsQ.error.message === 'app_disabled'
  ) {
    return (
      <div className="flex h-full flex-col">
        <div className="flex-1 overflow-y-auto px-4 py-6 md:px-6">
          <EmptyState
            testId="aws-control-disabled"
            icon={<Cloud />}
            title={i18nT('apps.awsControl.page.disabled_title')}
            subtitle={i18nT('apps.awsControl.page.disabled_body')}
          />
        </div>
      </div>
    )
  }

  if (accountsQ.isError) {
    // A 403 here is a permission answer (`dashboard_owner_required`), not a
    // transient read, so it gets copy that names the fix instead of the generic
    // "try again in a moment" — Retry only succeeds once the session is the
    // owner's, and the sentence must not promise otherwise.
    const forbidden = accountsQ.error instanceof AwsControlError && accountsQ.error.status === 403
    return (
      <div className="flex h-full flex-col">
        <div className="flex-1 overflow-y-auto px-4 py-6 md:px-6" data-testid="accounts-error">
          {/* The page's own error, not an EmptyState wearing red: an empty state
              says "nothing here yet", and a failed read says nothing of the
              sort. The notice carries the failure to the agent; Retry stays,
              because a transient read is the one case the reader can clear. */}
          <div className="mx-auto flex max-w-[480px] flex-col items-center gap-3 py-12">
            <AwsErrorNotice
              askAgent
              error={accountsQ.error}
              title={i18nT('apps.awsControl.page.error_title')}
              message={i18nT(forbidden
                ? 'apps.awsControl.page.error_forbidden_body'
                : 'apps.awsControl.page.error_body')}
              className="w-full"
              testId="aws-control-error"
            />
            <Btn onClick={() => accountsQ.refetch()} data-testid="error-retry">
              <RefreshCw size={13} />
              {i18nT('apps.awsControl.page.retry')}
            </Btn>
          </div>
        </div>
      </div>
    )
  }

  // No resolved account: there is nothing for the drive panes to show, so the
  // accounts pane IS the app until a key resolves — onboarding (zero accounts)
  // and all-red (unresolved rows with Reconnect) both land here, full width.
  if (accountsQ.isLoading || !selected) {
    return (
      <div className="flex h-full flex-col">
        <div className="flex-1 overflow-y-auto px-4 pt-4 pb-6 md:px-6">
          <AccountsPane accountsQ={accountsQ} selected={null} onUse={useAccount} />
        </div>
      </div>
    )
  }

  // Which pane the CONTENT area shows. On the bare path a wide viewport lands
  // straight on Files (the thesis: the drive is the product), while a narrow
  // one shows the root LIST — the same push-stack semantics as settings on a
  // phone, where the bare path is the list and a segment is a pushed detail.
  const pane: RailPane = paneFromPath ?? 'files'

  const paneContent = (
    <>
      {pane === 'files' && (
        <DrivePaneGate pane="files" account={selected} drive={drive} driveQ={driveQ}>
          {(bucket) => <DriveSectionView account={id} bucket={bucket} />}
        </DrivePaneGate>
      )}
      {pane === 'library' && (
        <DrivePaneGate pane="library" account={selected} drive={drive} driveQ={driveQ}>
          {(bucket) => <LibrarySection account={id} bucket={bucket} />}
        </DrivePaneGate>
      )}
      {pane === 'backup' && (
        <DrivePaneGate pane="backup" account={selected} drive={drive} driveQ={driveQ}>
          {() => <BackupSection account={id} />}
        </DrivePaneGate>
      )}
      {pane === 'shares' && (
        <DrivePaneGate pane="shares" account={selected} drive={drive} driveQ={driveQ}>
          {() => <AccessSection account={id} />}
        </DrivePaneGate>
      )}
      {pane === 'accounts' && (
        <AccountsPane accountsQ={accountsQ} selected={selected} onUse={useAccount} />
      )}
      {pane === 'usage' && <UsagePane account={selected} />}
    </>
  )

  if (narrow) {
    // Narrow viewport: iOS push-stack navigation, exactly like settings. The
    // bare path is the grouped root list; a pane segment is a pushed detail
    // with ONE back bar labelled with its parent (the app itself). The rail
    // never renders here — two navigation patterns on one screen is the
    // failure the settings redesign removed.
    if (!paneFromPath) {
      return (
        <div className="flex h-full flex-col" data-testid="aws-root-list">
          <div className="flex-1 overflow-y-auto px-4 pt-4 pb-6">
            <div className="mb-4">
              <AccountSwitcher
                accounts={resolved}
                selected={selected}
                onSelect={(nextId) => setStoredId(nextId)}
                onManage={() => openPane('accounts')}
              />
            </div>
            <div className="overflow-hidden rounded-lg border border-border bg-card divide-y divide-border">
              {DRIVE_PANES.map((p) => (
                <RootListRow key={p} pane={p} count={paneCount(p)} onOpen={() => openPane(p)} />
              ))}
            </div>
            <div className="mt-4 overflow-hidden rounded-lg border border-border bg-card divide-y divide-border">
              {FOOT_PANES.map((p) => (
                <RootListRow key={p} pane={p} onOpen={() => openPane(p)} />
              ))}
            </div>
            {drive?.exists && (
              <div className="mt-4 px-1 text-[11px] leading-relaxed text-muted" data-testid="rail-meta">
                <span className="block truncate font-mono">{drive.bucket}</span>
                <span className="block">
                  {i18nT('apps.awsControl.console.stat_stored_value', {
                    size: fmtBytes(drive.usage.bytes),
                    objects: fmtNumber(drive.usage.objects),
                  })}
                  {' \u00b7 '}
                  {drive.region}
                </span>
              </div>
            )}
          </div>
        </div>
      )
    }
    return (
      <div className="flex h-full flex-col" data-testid="aws-pane-detail">
        <div className="flex-1 overflow-y-auto px-4 pb-6">
          <NavBackBar
            label={i18nT('apps.awsControl.manifest.display_name')}
            onBack={() => {
              // Pop when this stack pushed the current entry (keeps push/pop
              // symmetric for the platform back gesture); replace-write on a
              // cold deep link, where back() would exit the app entirely.
              if ((location.state as Record<string, unknown> | null)?.[SUBNAV_PUSH_STATE]) {
                navigate(-1)
                return
              }
              navigate(APP_PATH, { replace: true })
            }}
            className="-mx-4"
          />
          {/* Same account-keyed remount as the wide layout: a confirm armed on
              one account must not survive onto another. */}
          <div key={id}>
            {paneContent}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-row">
      {/* The rail: wide viewports only. Narrow viewports use the push-stack
          root list above instead of squeezing this column. */}
      <nav
        className="flex w-56 shrink-0 flex-col items-stretch gap-1 border-r border-border px-3 py-3"
        aria-label={i18nT('apps.awsControl.rail.nav')}
        data-testid="aws-rail"
      >
        <AccountSwitcher
          accounts={resolved}
          selected={selected}
          onSelect={(nextId) => setStoredId(nextId)}
          onManage={() => openPane('accounts')}
        />
        {DRIVE_PANES.map((p) => (
          <RailItem key={p} pane={p} active={pane === p} onClick={() => openPane(p)} count={paneCount(p)} />
        ))}
        <div className="flex-1" />
        {FOOT_PANES.map((p) => (
          <RailItem key={p} pane={p} active={pane === p} onClick={() => openPane(p)} />
        ))}
        {/* The drive's identity, stated once at the rail's foot: bucket, size,
            and region — the facts every pane above shares. */}
        {drive?.exists && (
          <div className="border-t border-border px-2.5 pt-2 text-[11px] leading-relaxed text-muted" data-testid="rail-meta">
            <span className="block truncate font-mono">{drive.bucket}</span>
            <span className="block">
              {i18nT('apps.awsControl.console.stat_stored_value', {
                size: fmtBytes(drive.usage.bytes),
                objects: fmtNumber(drive.usage.objects),
              })}
              {' \u00b7 '}
              {drive.region}
            </span>
          </div>
        )}
      </nav>

      <div className="min-w-0 flex-1 overflow-y-auto px-4 pt-4 pb-6 md:px-6">
        {/* Keyed by the selected account: every pane holds account-BOUND
            transient state (an armed delete confirm, an open folder disclosure,
            a half-typed share note), and React would otherwise reuse the same
            component instances across a switch — a confirm armed on account A
            would stay armed and then fire its mutation against account B's
            same-named object. Remounting on switch is the reset that makes a
            switch mean "start clean on the other account". */}
        <div key={id}>
          {paneContent}
        </div>
      </div>
    </div>
  )
}
