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
  Cloud, RefreshCw, ChevronDown, ChevronRight, ChevronsUpDown, Check,
  FolderClosed, Library, Archive, Share2, Users, Wallet, MoreHorizontal, Trash2,
  LayoutDashboard, KeyRound, Plus, TriangleAlert,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  Btn, EmptyState, ContentSkeleton, IconButton, Card, CardTitle, PanelSectionHeader,
  SearchInput, Badge, FilteredEmpty, Skeleton, Checkbox,
} from '../../components/ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import Clickable from '../../components/Clickable'
import AwsConsentGate from '../../components/AwsConsentGate'
import { NavBackBar } from '../../components/NavBackBar'
import { COARSE_TOUCH_TARGET, SUBNAV_PUSH_STATE, parsePathSegments } from '../../components/subNavParams'
import { useIsNarrowViewport } from '../../hooks/useIsMobile'
import { usePersistedString } from '../../hooks/usePersistedString'
import { api, type AwsConsentStatus } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { fmtBytes, fmtNumber, fmtCurrency } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import UsagePane, { ConnectionsSection, ReconnectAction, SetupCard } from './ConsoleView'
import { DriveSectionView, LibrarySection, BackupSection, AccessSection, TileConfirm, SECTION_TILES } from './DrivePage'
import { PaneHeader, AwsErrorNotice, MetricCard, StorageBar, QuickTile } from './shared'
import type { AwsAccount, AccountHealth, DriveStatus, DriveSection } from './types'

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

/** Text tone for the health WORD, the same hue as its dot: one fact must not
 *  read as two states (a grey dot beside an amber "Unknown"). */
const HEALTH_TEXT: Record<AccountHealth, string> = {
  ok: 'text-ok',
  degraded: 'text-warn',
  unknown: 'text-muted',
}

/** The name a row leads with: the backend name, or the "not connected" label. */
function accountName(account: AwsAccount): string {
  return account.name || i18nT('apps.awsControl.page.not_connected_yet')
}

/* ── the rail ────────────────────────────────────────────────────────────── */

/** The panes the rail can show. Overview leads (it is the app's landing pane),
 *  then the four drive sections; the two management panes sink to the foot. */
type RailPane = 'overview' | 'files' | 'library' | 'backup' | 'shares' | 'accounts' | 'usage'

/* Literal-key maps from pane → catalog key, so no i18nT() call assembles a key
 * by interpolation (dynamicKeys gate). The four drive panes reuse the section
 * names their own headers already render, so the rail item and the pane title
 * cannot drift to different names. */
const PANE_LABEL_KEY: Record<RailPane, string> = {
  overview: 'apps.awsControl.overview.title',
  files: 'apps.awsControl.console.section_files',
  library: 'apps.awsControl.console.section_library',
  backup: 'apps.awsControl.console.section_backup',
  shares: 'apps.awsControl.console.access_title',
  accounts: 'apps.awsControl.rail.accounts',
  usage: 'apps.awsControl.rail.usage',
}

const PANE_ICON: Record<RailPane, LucideIcon> = {
  overview: LayoutDashboard,
  files: FolderClosed,
  library: Library,
  backup: Archive,
  shares: Share2,
  accounts: Users,
  usage: Wallet,
}

/** The rail's first group: the landing pane plus the four drive sections. */
const TOP_PANES: RailPane[] = ['overview', 'files', 'library', 'backup', 'shares']
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
    // `Clickable` rather than a bare <button>: it is the shared accessible
    // click surface (role=button, tabIndex, Enter/Space) and it forwards
    // `aria-current`, which is the one attribute this item cannot lose.
    <Clickable
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      data-testid={`rail-${pane}`}
      className={`flex w-full shrink-0 items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] cursor-pointer focus-ring md:shrink ${
        active ? 'bg-accent-subtle font-medium text-text' : 'text-text hover:bg-bg-hover'
      }`}
    >
      <Icon className={`h-4 w-4 shrink-0 ${active ? 'text-accent' : 'text-muted'}`} aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate">{i18nT(PANE_LABEL_KEY[pane])}</span>
      {count !== undefined && (
        <span className="shrink-0 font-mono text-[12px] tabular-nums text-muted" data-testid={`rail-${pane}-count`}>
          {fmtNumber(count)}
        </span>
      )}
    </Clickable>
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
            {/* A word beside the dot for anything not healthy: colour alone is
                the only cue otherwise, and the rows in Accounts & credentials
                already spell the state out the same way. */}
            {a.health !== 'ok' && (
              <span className={`shrink-0 text-[11px] ${HEALTH_TEXT[a.health]}`} data-testid="switcher-option-health">
                {i18nT(HEALTH_LABEL_KEY[a.health])}
              </span>
            )}
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
 * Where an `AccountRow` is rendered. ONE component serves both sites — the
 * accounts pane's full-width list and the Overview card's half-width one — so a
 * row's behaviour (select, reconnect, remove) cannot diverge between them. The
 * variant carries only what the CONTAINER's width decides: the row's density,
 * and the breakpoint at which the region and the keys count still fit beside
 * the health badge.
 */
type AccountRowVariant = 'pane' | 'overview'

/**
 * One account row: a health dot, the account name, and under it the full
 * 12-digit id with the account's region — then, on the right, the keys count,
 * the health word when something is wrong, a Reconnect action for a failing
 * connection, and an overflow menu.
 *
 * A resolved row SELECTS that account for the whole app (rail card, drive panes,
 * usage). An UNRESOLVED row cannot be selected (there is no account behind it),
 * so its click toggles the inline Reconnect guidance instead — a red row must
 * always offer a way back to green. A DEGRADED row is resolved but has at least
 * one dead key, so its click still selects and the guidance hangs off its own
 * Reconnect button: before that button existed the amber row named the problem
 * and offered nothing to do about it.
 *
 * Every row also carries an overflow menu whose one item removes the account
 * from AWS Control: a registration must be reversible from the same surface
 * that offered it, or a key added by mistake stays on the page for good.
 * Removal is registry-only — it forgets the account's keys HERE and withdraws
 * their paid-service consent, and changes nothing in AWS or in the operator's
 * AWS CLI configuration — which the confirm strip states before the reader
 * commits. The strip stays open until the request resolves because it is the
 * only place the outcome can render.
 */
function AccountRow({ account, current, onUse, askAgent, variant = 'pane' }: {
  account: AwsAccount
  current: boolean
  onUse: () => void
  /** Whether this row's Reconnect notice may hand off to the agent; the pane decides. */
  askAgent: boolean
  variant?: AccountRowVariant
}) {
  const keys = account.profiles.length
  const resolved = Boolean(account.account)
  // The key Reconnect acts on: the first one whose identity check FAILED, which
  // is what made the account degraded. Falls back to the default/first key so an
  // account marked unhealthy without a per-key reason still offers guidance.
  const failing = account.profiles.find((p) => !p.identityOk) ?? account.profiles[0]
  const defaultProfile = account.profiles.find((p) => p.default) ?? account.profiles[0]
  const region = defaultProfile?.region ?? ''
  const [showReconnect, setShowReconnect] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const qc = useQueryClient()
  const removeM = useMutation({
    mutationFn: () => awsControlApi.unregisterProfiles(account.profiles.map((p) => p.name)),
    onSuccess: () => {
      setConfirming(false)
      // The accounts list and the Add-accounts disclosure are two views of one
      // registry, and a withdrawn grant must leave the usage receipts too.
      void qc.invalidateQueries({ queryKey: ['aws-control', 'accounts'] })
      void qc.invalidateQueries({ queryKey: ['aws-control', 'profiles-available'] })
      void qc.invalidateQueries({ queryKey: ['awsConsent'] })
    },
  })
  // The unresolved pseudo-row has no account name to quote, so its confirm
  // asks about the key it will forget, in a sentence of its own: the row reads
  // "Not connected yet", and a confirm that suddenly named a key under the
  // account wording would read as removing the wrong thing.
  const confirmLabel = resolved
    ? i18nT('apps.awsControl.page.remove_account_confirm', { name: accountName(account) })
    : i18nT('apps.awsControl.page.forget_key_confirm', {
      name: account.profiles.map((p) => p.name).join(', '),
    })
  // The keys count competes with the health badge for the same right-hand
  // cluster, so on a row that HAS a health badge it waits for the width that
  // fits both. The Overview card is half the pane's width from `lg`, which is
  // why its threshold is one step wider.
  const keysAt = account.health === 'ok'
    ? ''
    : variant === 'overview' ? 'hidden' : 'hidden sm:inline-flex'
  // A row that needs attention carries the keys badge, the health word and the
  // menu on its right, which does not fit beside the account identity at 320px.
  // Rather than hide one — and the health word is the one thing that must never
  // be hidden, or the amber dot becomes the only cue — the cluster takes its
  // own line on a phone and sits inline from `sm` up.
  const needsAction = resolved && account.health !== 'ok'
  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 pr-1">
      <button
        onClick={resolved ? onUse : () => setShowReconnect((v) => !v)}
        className={`flex min-w-0 flex-1 items-center gap-3 rounded-md px-3 text-left cursor-pointer bg-transparent border-none hover:bg-bg-hover focus-ring ${
          variant === 'overview' ? 'py-2' : 'py-2.5'
        }`}
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
        {/* Two lines, not one: the name is the row's subject and the id/region
            are how it is identified, and at 320px a single row of all three
            clipped the id — the one string an operator needs verbatim. */}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium text-text-strong" data-testid="account-name">
            {accountName(account)}
          </span>
          <span className="mt-0.5 flex items-center gap-1.5 text-[12px] text-muted">
            {account.account ? (
              <span className="min-w-0 truncate font-mono tabular-nums" data-testid="account-id">
                {account.account}
              </span>
            ) : (
              // No account to name, so the meta names the KEY this row stands
              // for — otherwise the line just repeats the title above it.
              <span className="min-w-0 truncate font-mono" data-testid="account-key-names">
                {account.profiles.map((p) => p.name).join(', ')}
              </span>
            )}
            {region && variant !== 'overview' && (
              <>
                {/* The dot hides with the region it separates, or the id ends
                    in a stray "·" on a phone. */}
                <span aria-hidden="true" className="hidden sm:inline">{'\u00b7'}</span>
                <span
                  className="min-w-0 truncate hidden sm:inline"
                  data-testid="account-region"
                >
                  {region}
                </span>
              </>
            )}
          </span>
        </span>
        {/* The row's affordance: the check marks the account the app is
            currently on; other resolved rows show nothing and select on
            click; unresolved rows disclose Reconnect. */}
        {current ? (
          <Check className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" data-testid="account-current" />
        ) : !resolved ? (
          <ChevronDown className={`h-3.5 w-3.5 shrink-0 text-muted transition-transform ${showReconnect ? 'rotate-180' : ''}`} aria-hidden="true" />
        ) : null}
      </button>
      {/* Outside the select button: a button cannot nest a button, and the
          menu must not select the account it acts on. Two controls on the row
          — the select surface and this menu — and everything else, Reconnect
          included, is an item inside the menu. */}
      <div className={`flex shrink-0 items-center justify-end gap-1.5 ${
        needsAction ? (variant === 'overview' ? 'basis-full' : 'basis-full sm:basis-auto') : ''
      }`}>
        <Badge variant="muted" className={`shrink-0 ${keysAt}`} data-testid="account-keys">
          <KeyRound className="h-3 w-3" aria-hidden="true" />
          {i18nT('apps.awsControl.page.keys_summary', { count: keys })}
        </Badge>
        {/* A word, not just a colour: the dot alone made a degraded account
            distinguishable only by hue. Healthy rows stay quiet — the badge
            appears exactly when something needs attention, and it is never
            hidden at any width. */}
        {account.health !== 'ok' && (
          <Badge
            variant={account.health === 'degraded' ? 'warn' : 'muted'}
            className="shrink-0"
            data-testid="account-health-word"
          >
            {account.health === 'degraded' && <TriangleAlert className="h-3 w-3" aria-hidden="true" />}
            {i18nT(HEALTH_LABEL_KEY[account.health])}
          </Badge>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <IconButton
              aria-label={i18nT('apps.awsControl.page.account_actions')}
              className="shrink-0 text-muted"
              data-testid="account-more"
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </IconButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {/* Reconnect lives in the menu, not beside it: the row already
                carries the select surface and this trigger, and a third control
                is where the right-hand cluster stopped fitting. The health word
                on the row is the cue that the menu holds something to do. */}
            {needsAction && failing && (
              <DropdownMenuItem
                onSelect={() => setShowReconnect((v) => !v)}
                data-testid="account-reconnect"
              >
                <RefreshCw size={13} className="shrink-0" />
                {i18nT('apps.awsControl.page.reconnect')}
              </DropdownMenuItem>
            )}
            {/* The safety fact rides on the item itself: a trash-can item beside
                an account is what a cautious first-time reader refuses to click,
                and the strip that would have reassured them sits behind that
                click. The ellipsis says the item asks first. */}
            <DropdownMenuItem onSelect={() => setConfirming(true)} className="items-start" data-testid="account-remove">
              <Trash2 size={13} className="mt-0.5 shrink-0" />
              <span className="flex min-w-0 flex-col">
                <span>{i18nT('apps.awsControl.page.remove_account')}</span>
                <span className="text-[12px] text-muted" data-testid="account-remove-hint">
                  {i18nT('apps.awsControl.page.remove_account_hint')}
                </span>
              </span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      </div>
      {confirming && (
        <div className="px-3 pb-2">
          <TileConfirm
            testId="account-remove-confirm"
            label={confirmLabel}
            action={i18nT(resolved
              ? 'apps.awsControl.page.remove_account_action'
              : 'apps.awsControl.page.forget_key_action')}
            error={removeM.isError ? i18nT('apps.awsControl.page.remove_account_error') : ''}
            errorSource={removeM.error ?? undefined}
            askAgent={askAgent}
            pending={removeM.isPending}
            onCancel={() => { setConfirming(false); removeM.reset() }}
            onConfirm={() => removeM.mutate()}
          />
        </div>
      )}
      {showReconnect && failing && (
        <div className="px-3 pb-2" data-testid="row-reconnect">
          <ReconnectAction profile={failing} askAgent={askAgent} />
        </div>
      )}
    </div>
  )
}

/**
 * An "Add accounts" disclosure: lists the LOCAL profiles the CLI knows but the
 * portal has not registered, each with a checkbox, and registers the checked
 * set. It stays collapsed by default so the account list remains the pane's
 * primary content — except when that list is EMPTY: then this disclosure is
 * the pane's only useful action, and the empty state above it points here, so
 * it opens itself rather than making a first-run reader find a chevron. On
 * success it invalidates the accounts query so a newly registered profile
 * appears without a manual refresh.
 */
function AddAccounts({ onDraftChange, autoOpen = false }: {
  /**
   * Fires with `true` while at least one profile is ticked and not yet
   * registered, `false` once the selection is empty again. The ticks live only
   * in this component's state, so anything on the pane that navigates away —
   * an agent hand-off on a sibling notice — would drop them; the pane uses this
   * to withhold those hand-offs while a selection is open.
   */
  onDraftChange: (hasDraft: boolean) => void
  /** Start expanded — set while the account list above is empty. */
  autoOpen?: boolean
}) {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(autoOpen)
  // The list arrives after mount, so the empty-list signal can flip from
  // false to true later; open on that edge only, never force closed — a reader
  // who collapsed it by hand keeps their choice.
  useEffect(() => {
    if (autoOpen) setOpen(true)
  }, [autoOpen])
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
      <Clickable
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded-md p-0 text-left cursor-pointer focus-ring"
        data-testid="add-accounts-toggle"
        aria-expanded={open}
      >
        <ChevronDown className={`h-3.5 w-3.5 shrink-0 text-muted transition-transform ${open ? '' : '-rotate-90'}`} aria-hidden="true" />
        <span className="text-sm font-semibold text-text-strong">
          {i18nT('apps.awsControl.page.add_accounts_title')}
        </span>
        <span className="text-[12px] text-muted">
          {i18nT('apps.awsControl.page.add_accounts_summary')}
        </span>
      </Clickable>

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
                      <Checkbox
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
function AccountsPane({ accountsQ, selected, onUse, openAddAccounts = false }: {
  accountsQ: ReturnType<typeof useAccountsQuery>
  selected: AwsAccount | null
  onUse: (account: AwsAccount) => void
  /**
   * Arrive with the Add-accounts disclosure already open. Set by the Overview
   * card's "Add account" action, which is a request to register a profile — not
   * a request to read the list and then hunt for a chevron.
   */
  openAddAccounts?: boolean
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
        icon={<Users className="h-[18px] w-[18px]" />}
        title={i18nT('apps.awsControl.rail.accounts')}
        subtitle={data?.totals ? (
          // The totals ARE this pane's orientation sentence, so they belong in
          // the header's subtitle slot rather than as a strip competing with the
          // list below it. It keeps its own test id: the fact is the same one.
          // The account count is the LIST's count — every row, the unresolved
          // pseudo-row included — so the sentence and the card header under it
          // never name two different numbers.
          <span data-testid="accounts-totals">
            {i18nT('apps.awsControl.page.totals_summary', {
              accounts: fmtNumber(data.accounts.length),
              keys: fmtNumber(data.totals.profiles),
              healthy: fmtNumber(data.totals.profilesHealthy),
            })}
          </span>
        ) : undefined}
        actions={
          <Btn onClick={() => accountsQ.refetch()} disabled={accountsQ.isFetching} data-testid="refresh">
            <RefreshCw className={`h-3.5 w-3.5 ${accountsQ.isFetching ? 'animate-spin' : ''}`} />
            {i18nT('apps.awsControl.page.refresh')}
          </Btn>
        }
      />

      {accountsQ.isLoading && (
        // Mirrors the row box below (same card, same divider, same row height)
        // so the list does not jump when the answer lands.
        <Card className="px-2 py-4 md:px-4" data-testid="accounts-loading">
          <PanelSectionHeader label={i18nT('apps.awsControl.overview.accounts_title')} className="mb-2 px-1" />
          <div className="divide-y divide-border">
            {[0, 1, 2].map((i) => (
              <div key={i} className="flex items-center gap-3 px-3 py-2.5">
                <Skeleton className="h-2 w-2 rounded-full" />
                <div className="min-w-0 flex-1">
                  <Skeleton className="h-3.5 w-32" />
                  <Skeleton className="mt-1.5 h-3 w-40" />
                </div>
                <Skeleton className="h-5 w-16 rounded-full" />
              </div>
            ))}
          </div>
        </Card>
      )}

      {data && data.accounts.length === 0 && (
        <div data-testid="accounts-empty">
          <EmptyState
            testId="aws-control-empty"
            icon={<Cloud />}
            title={i18nT('apps.awsControl.page.empty_title')}
            subtitle={canAddHere ? i18nT('apps.awsControl.page.empty_body') : undefined}
          />
        </div>
      )}

      {data && data.accounts.length > 0 && (
        <Card className="px-2 py-4 md:px-4" data-testid="accounts-card">
          <PanelSectionHeader
            label={i18nT('apps.awsControl.overview.accounts_title')}
            count={data.accounts.length}
            className="mb-2 px-1"
            trailing={
              <SearchInput
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={i18nT('apps.awsControl.page.search_placeholder')}
                aria-label={i18nT('apps.awsControl.page.search_placeholder')}
                className="w-32 sm:w-48"
                data-testid="accounts-search"
              />
            }
          />
          {filtered.length > 0 ? (
            <div className="divide-y divide-border" data-testid="accounts-list">
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
          ) : (
            // "Your data exists, your filter hid it" — visually lighter than the
            // empty state above, and it offers the one action that undoes it.
            <FilteredEmpty
              query={query.trim()}
              onClear={() => setQuery('')}
              testId="accounts-search-empty"
            />
          )}
        </Card>
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

      {/* Opens itself when there is nothing above it to select: the empty
          state's sentence points here, so the section it names is already
          expanded when the eye arrives. Also opens when the reader ASKED for it
          from the Overview card. */}
      <AddAccounts
        onDraftChange={setRegistrationDraft}
        autoOpen={openAddAccounts || Boolean(data && data.accounts.length === 0)}
      />
    </section>
  )
}

/* ── Overview pane ───────────────────────────────────────────────────────── */

/**
 * The app's landing pane: what is connected, what it costs, and what needs
 * attention — read-only, on one screen.
 *
 * It exists because the rail's panes each answer ONE question and the app had no
 * surface that answered "is everything fine": an operator landing on Files could
 * not see that a second account had gone amber, that a share link expires
 * tomorrow, or that the month-to-date figure had stopped being read. Every
 * number here is a fact one of the panes already owns, restated once at a
 * glance; the pane that can act on it is one rail click away, so this pane adds
 * no mutation of its own beyond the two paid-service gates, which are the one
 * decision that has nowhere else to live at this level.
 *
 * A read that FAILS says so: each of the pane's own reads (drive, bill, share
 * links, backup schedule) renders an `AwsErrorNotice` with a retry when it
 * errors, and its card holds a dash rather than an empty state — a 5xx shown as
 * "no drive" or "$0" would be the pane lying about the one thing it exists to
 * answer.
 */
/** The rail pane that owns each drive section, for the Overview's tiles. */
const SECTION_PANE: Record<DriveSection, RailPane> = { drive: 'files', library: 'library', backup: 'backup' }

function OverviewPane({ accountsQ, selected, drive, driveQ, sharesQ, onUse, onOpenPane, onAddAccount }: {
  accountsQ: ReturnType<typeof useAccountsQuery>
  selected: AwsAccount
  drive: DriveStatus | undefined
  driveQ: { isLoading: boolean; isError: boolean; error: unknown }
  sharesQ: { data?: { shares: unknown[] }; isError: boolean; error: unknown }
  onUse: (account: AwsAccount) => void
  onOpenPane: (pane: RailPane) => void
  onAddAccount: () => void
}) {
  const qc = useQueryClient()
  const id = selected.account
  const data = accountsQ.data
  const accounts = data?.accounts ?? []
  // The rows that wear the "Needs attention" word, and only those: an
  // unresolved key says "Unknown", and a count that included it read as a
  // number the list below could not account for.
  const attention = accounts.filter((a) => a.health === 'degraded').length
  const live = drive?.exists ? drive : null
  // Only ONE 409 is "not set up yet": `aws_consent_required`, the reader's own
  // pending decision, which the Files pane's consent card owns — so the card
  // points there. Every other rejection, the 409s for a stale or mismatched
  // connection included, is a failed READ and renders as one; the code, not
  // the status, is what tells the two apart, same as the pane gate below.
  const driveErr = driveQ.error instanceof AwsControlError ? driveQ.error : null
  const driveNeedsConsent = driveErr?.status === 409 && driveErr.message === 'aws_consent_required'
  const driveFailed = driveQ.isError && !driveNeedsConsent

  const costsQ = useQuery({
    queryKey: ['aws-control', 'costs', id],
    queryFn: () => awsControlApi.costs(id),
    // A dead bill read (CE not enabled, throttled) should settle to the quiet
    // em-dash in seconds, not skeleton through three backoffs. Same as the
    // usage pane, which shares this cache entry.
    retry: 1,
  })
  const backupQ = useQuery({
    // Same key the Backup pane uses with its remote list CLOSED, so the two
    // share one cache entry rather than each paying for its own read.
    queryKey: ['aws-control', 'backup', id, false],
    queryFn: () => awsControlApi.backup(id, { remote: false }),
    // Nothing to schedule before the bucket exists, and the request would 409.
    enabled: Boolean(live),
  })

  const costs = costsQ.data
  // A consent-required 409 from the bill read is the reader's pending decision
  // (the CE gate on this same pane), not a failure; it reads as "consent
  // missing". Any other rejection — a stale connection's 409 among them — is a
  // failed read that earns the notice below.
  const costsErr = costsQ.error instanceof AwsControlError ? costsQ.error : null
  const spendNeedsConsent = Boolean(costs?.consentMissing)
    || (costsErr?.status === 409 && costsErr.message === 'aws_consent_required')
  const costsFailed = costsQ.isError && !spendNeedsConsent
  // A 200 can carry a STALE figure with `fetchError` set: the refresh failed
  // and the backend served its cache. The number stays (it is the last true
  // reading), and the failure is said next to it rather than swallowed.
  const costsStale = Boolean(costs?.fetchError)
  // WHY there is no figure, in the card's own sub-line rather than a tooltip: a
  // bare dash left a mouse-less reader with a blank they could not explain.
  const spend = spendNeedsConsent || costsFailed
    ? '—'
    : costs
      ? fmtCurrency(costs.monthToDate, costs.currency)
      : undefined
  // A FAILED read gets a dash and no caption: the notice under the strip is
  // the one surface that says it failed, with the context and the hand-off a
  // caption cannot carry, and a second sentence would compete with it.
  // With a figure, the sub-line names its SCOPE: "$41.20" beside "No drive
  // yet" read as a contradiction until the line said whose bill it is.
  const spendSub = spendNeedsConsent
    ? i18nT('apps.awsControl.overview.stat_spend_consent')
    : costs && !costsFailed
      ? i18nT('apps.awsControl.overview.stat_spend_scope')
      : undefined

  const shares = sharesQ.data?.shares.length
  const nightly = backupQ.data?.nightly

  /** Everything this pane reads, re-read at once. */
  const refreshAll = () => {
    void accountsQ.refetch()
    void qc.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })
    void qc.invalidateQueries({ queryKey: ['aws-control', 'costs', id] })
    void qc.invalidateQueries({ queryKey: ['aws-control', 'shares', id] })
    void qc.invalidateQueries({ queryKey: ['aws-control', 'backup', id] })
  }

  return (
    <section data-testid="overview-pane">
      <PaneHeader
        icon={<LayoutDashboard className="h-[18px] w-[18px]" />}
        title={i18nT('apps.awsControl.overview.title')}
        subtitle={i18nT('apps.awsControl.overview.subtitle')}
        actions={
          <Btn onClick={refreshAll} disabled={accountsQ.isFetching} data-testid="refresh">
            <RefreshCw className={`h-3.5 w-3.5 ${accountsQ.isFetching ? 'animate-spin' : ''}`} />
            {i18nT('apps.awsControl.page.refresh')}
          </Btn>
        }
      />

      {/* Six readings, narrow-first: two across on a phone, six on a wide
          desktop. Each carries the sub-line that makes its number mean
          something — a count with no context is a number, not a reading. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 2xl:grid-cols-6" data-testid="overview-stats">
        {/* ONE population for the card, its sub-line and the list below: every
            row, the unresolved pseudo-row included. `totals.accounts` counts
            resolved accounts only, and a "2" over a list headed "3" was the
            pane contradicting itself in its first two lines. */}
        <MetricCard
          testId="overview-stat-accounts"
          onClick={() => onOpenPane('accounts')}
          label={i18nT('apps.awsControl.overview.stat_accounts')}
          value={data ? fmtNumber(accounts.length) : undefined}
          // "All healthy" only when every row IS: an unresolved row is not
          // counted as needing attention (it says "Unknown"), but it is not
          // healthy either, so the caption stays silent rather than assert it.
          sub={data
            ? attention > 0
              ? i18nT('apps.awsControl.overview.stat_accounts_attention', { count: attention })
              : accounts.every((a) => a.health === 'ok')
                ? i18nT('apps.awsControl.overview.stat_accounts_all_ok')
                : undefined
            : undefined}
          delay={0}
        />
        <MetricCard
          testId="overview-stat-keys"
          onClick={() => onOpenPane('accounts')}
          label={i18nT('apps.awsControl.overview.stat_keys')}
          value={data?.totals
            ? `${fmtNumber(data.totals.profilesHealthy)}/${fmtNumber(data.totals.profiles)}`
            : undefined}
          sub={data?.totals
            ? i18nT('apps.awsControl.overview.stat_keys_sub', { count: data.totals.profiles })
            : undefined}
          delay={60}
        />
        <MetricCard
          testId="overview-stat-drive"
          onClick={() => onOpenPane('files')}
          label={i18nT('apps.awsControl.overview.stat_drive')}
          // Undefined while the drive read is in flight, so the card holds its
          // own skeleton instead of asserting "no drive" for a frame.
          value={live ? fmtBytes(live.usage.bytes) : driveQ.isLoading ? undefined : '—'}
          sub={live
            ? i18nT('apps.awsControl.console.root_section_objects', { count: live.usage.objects, objects: fmtNumber(live.usage.objects) })
            : driveQ.isLoading || driveFailed
              ? undefined
              : i18nT('apps.awsControl.overview.stat_drive_none')}
          delay={120}
        />
        <MetricCard
          testId="overview-stat-spend"
          onClick={() => onOpenPane('usage')}
          label={i18nT('apps.awsControl.overview.stat_spend')}
          value={spend}
          sub={spendSub}
          delay={180}
        />
        <MetricCard
          testId="overview-stat-shares"
          onClick={() => onOpenPane('shares')}
          label={i18nT('apps.awsControl.console.access_title')}
          value={sharesQ.isError ? '—' : shares === undefined ? undefined : fmtNumber(shares)}
          sub={sharesQ.isError || shares === undefined
            ? undefined
            : shares === 0
              ? i18nT('apps.awsControl.overview.stat_shares_none')
              : i18nT('apps.awsControl.overview.stat_shares_sub', { count: shares })}
          delay={240}
        />
        <MetricCard
          testId="overview-stat-backups"
          onClick={() => onOpenPane('backup')}
          label={i18nT('apps.awsControl.console.section_backup')}
          value={backupQ.isError
            ? '—'
            : nightly === undefined
            ? live ? undefined : '—'
            : nightly
              ? i18nT('apps.awsControl.overview.stat_backups_nightly')
              : i18nT('apps.awsControl.overview.stat_backups_manual')}
          // Accent marks the reading that means "protected"; a manual schedule
          // is not an error, so it stays in the ordinary tone.
          accent={nightly === true}
          sub={backupQ.isError
            ? undefined
            : nightly === undefined
            ? live ? undefined : i18nT('apps.awsControl.overview.stat_drive_none')
            : i18nT(nightly
              ? 'apps.awsControl.overview.stat_backups_sub_nightly'
              : 'apps.awsControl.overview.stat_backups_sub_manual')}
          delay={300}
        />
      </div>

      {/* Failed reads, each under the strip whose figure it emptied. */}
      <div className="mt-3 flex flex-col gap-2">
        <AwsErrorNotice
          askAgent
          error={costsQ.error}
          message={costsFailed
            ? i18nT('apps.awsControl.console.costs_unavailable')
            : costsStale
              ? i18nT('apps.awsControl.console.costs_refresh_failed')
              : null}
          onRetry={() => qc.invalidateQueries({ queryKey: ['aws-control', 'costs', id] })}
          testId="overview-costs-error"
        />
        <AwsErrorNotice
          askAgent
          error={sharesQ.error}
          message={sharesQ.isError ? i18nT('apps.awsControl.console.access_list_failed') : null}
          onRetry={() => qc.invalidateQueries({ queryKey: ['aws-control', 'shares', id] })}
          testId="overview-shares-error"
        />
        <AwsErrorNotice
          askAgent
          error={backupQ.error}
          message={backupQ.isError ? i18nT('apps.awsControl.console.backup_status_failed') : null}
          onRetry={() => qc.invalidateQueries({ queryKey: ['aws-control', 'backup', id] })}
          testId="overview-backup-error"
        />
      </div>

      {/* `items-start`: the two cards carry unrelated amounts of content, and
          stretching the shorter one leaves a tall empty box beside a full one. */}
      <div className="mt-4 grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
        <Card className="px-2 py-4 md:px-4" data-testid="overview-accounts">
          <PanelSectionHeader
            label={i18nT('apps.awsControl.overview.accounts_title')}
            count={accounts.length}
            className="mb-2 px-1"
            trailing={
              <Btn onClick={onAddAccount} data-testid="overview-add-account">
                <Plus className="h-3.5 w-3.5" />
                {i18nT('apps.awsControl.overview.add_account')}
              </Btn>
            }
          />
          <div className="divide-y divide-border">
            {accounts.map((a, i) => (
              <AccountRow
                key={a.account || `unresolved-${i}`}
                account={a}
                current={a.account === selected.account}
                onUse={() => onUse(a)}
                variant="overview"
                // This card shares its pane with no draft input, so the
                // hand-off never costs typed text here.
                askAgent
              />
            ))}
          </div>
          <p className="mt-3 px-1 text-[12px] leading-relaxed text-muted" data-testid="overview-accounts-note">
            {i18nT('apps.awsControl.overview.accounts_note')}
          </p>
        </Card>

        <Card className="px-2 py-4 md:px-4" data-testid="overview-drive">
          <CardTitle>
            <Cloud className="h-4 w-4 text-accent" aria-hidden="true" />
            {i18nT('apps.awsControl.overview.drive_title')}
          </CardTitle>
          {live ? (
            <>
              {/* No headline figure: the Drive used card in the strip above
                  already prints the bytes, and this card's job is the SPLIT. */}
              <div className="mt-3 px-1">
                <StorageBar usage={live.usage} testId="overview-storage" />
              </div>
              <div className="mt-4 grid grid-cols-1 gap-2 px-1 sm:grid-cols-3">
                {/* Each tile opens the pane that owns its section: the tile is
                    the split's one per-section reading, and the pane is where
                    the reader acts on it. */}
                {SECTION_TILES.map(({ section, icon: Icon, labelKey }) => (
                  <QuickTile
                    key={section}
                    testId={`overview-tile-${section}`}
                    onClick={() => onOpenPane(SECTION_PANE[section])}
                    icon={<Icon className="h-4 w-4" aria-hidden="true" />}
                    label={i18nT(labelKey)}
                    count={i18nT('apps.awsControl.console.root_section_objects', {
                      count: live.usage.sections[section].objects,
                      objects: fmtNumber(live.usage.sections[section].objects),
                    })}
                  />
                ))}
              </div>
              <p className="mt-3 flex flex-wrap items-baseline gap-x-2 px-1 text-[12px] leading-relaxed text-muted">
                <span>{i18nT('apps.awsControl.overview.drive_bucket_label')}</span>
                <span className="break-all font-mono text-text" data-testid="overview-drive-bucket">{live.bucket}</span>
                <span className="font-mono" data-testid="overview-drive-region">{live.region}</span>
              </p>
            </>
          ) : driveQ.isLoading ? (
            // Mirrors the box above so the card does not resize under the reader.
            <div className="px-1" data-testid="overview-drive-loading">
              <Skeleton className="mt-3 h-1.5 w-full rounded-full" />
              <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3">
                {SECTION_TILES.map(({ section }) => <Skeleton key={section} className="h-16 rounded-lg" />)}
              </div>
            </div>
          ) : driveFailed ? (
            <AwsErrorNotice
              askAgent
              error={driveQ.error}
              message={i18nT(driveErr?.status === 409
                ? 'apps.awsControl.console.account_unavailable'
                : 'apps.awsControl.console.drive_status_failed')}
              onRetry={() => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })}
              testId="overview-drive-error"
              className="mx-1"
            />
          ) : (
            // No bucket, or not set up yet: the card carries the one action
            // that changes that, and the Files pane owns the actual creation.
            <EmptyState
              testId="overview-drive-empty"
              icon={<Cloud />}
              title={i18nT('apps.awsControl.overview.drive_empty_title')}
              subtitle={i18nT('apps.awsControl.overview.drive_empty_body')}
              action={
                <Btn primary onClick={() => onOpenPane('files')} data-testid="overview-drive-setup">
                  <Cloud className="h-3.5 w-3.5" />
                  {i18nT('apps.awsControl.overview.drive_setup')}
                </Btn>
              }
            />
          )}
        </Card>
      </div>

      {/* The two paid services as one compact row each. They are the only
          mutations on this pane and they belong here: a reader deciding whether
          the agents may spend money is deciding it about the whole app, not
          about the pane they happen to be on. */}
      <Card className="px-2 py-4 md:px-4" data-testid="overview-paid-services">
        <PanelSectionHeader
          label={i18nT('apps.awsControl.page.paid_services_title')}
          count={2}
          className="mb-2 px-1"
        />
        <div className="divide-y divide-border">
          {/* A grant unblocks a read this pane already tried: the drive's
              consent 409 and the bill's. Re-issue them, or the pane keeps
              showing "set up" and a dash under a receipt that says Confirmed. */}
          <AwsConsentGate
            service="s3"
            compact
            askAgent
            onConsentChange={() => qc.invalidateQueries({ queryKey: ['aws-control', 'drive', id] })}
          />
          <AwsConsentGate
            service="ce"
            compact
            askAgent
            onConsentChange={() => qc.invalidateQueries({ queryKey: ['aws-control', 'costs', id] })}
          />
        </div>
      </Card>
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
        <div data-testid="capability-drive-setup">
          <SetupCard account={id} region={setupRegion} />
        </div>
      )}
    </section>
  )
}

/** The app's own base path; pane routes hang off it (/aws-control/usage). */
const APP_PATH = '/aws-control'
const ALL_PANES: RailPane[] = [...TOP_PANES, ...FOOT_PANES]

/**
 * The pane the app lands on with no segment in the path: the read-only room that
 * answers "is everything fine" before the reader has to pick a question.
 */
const DEFAULT_PANE: RailPane = 'overview'

/**
 * Router state key that asks the accounts pane to arrive with its Add-accounts
 * disclosure open. Carried on the navigation rather than held in the shell, so
 * the request expires with the next navigation instead of re-opening the
 * disclosure every later visit.
 */
const ADD_ACCOUNTS_STATE = 'awsControlAddAccounts'

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
  // falls back to the DEFAULT pane on every width — mapping it to null would
  // read the same URL as Overview on a desktop and as the root list on a phone,
  // two meanings for one address.
  return seg === '' ? null : DEFAULT_PANE
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
    <Clickable
      onClick={onOpen}
      data-testid={`root-${pane}`}
      className={`flex w-full items-center gap-3 px-3 py-2.5 ${COARSE_TOUCH_TARGET} text-left cursor-pointer hover:bg-bg-hover focus-ring`}
    >
      <Icon className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate text-[14px] text-text-strong">{i18nT(PANE_LABEL_KEY[pane])}</span>
      {count !== undefined && (
        <span className="shrink-0 font-mono text-[12px] tabular-nums text-muted">{fmtNumber(count)}</span>
      )}
      <ChevronRight className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
    </Clickable>
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
  // like the on-screen back bar. Wide rail clicks REPLACE — walking every rail
  // click on browser-back is not a history the reader asked for. Mirrors
  // SettingsSubNav's contract. On a narrow viewport every caller of this sits
  // on the root list (the rows and the switcher's manage entry), so a narrow
  // call is always the drill-in; a pane never navigates to another pane.
  const openPane = (p: RailPane, opts?: { withAddAccounts?: boolean }) => {
    const drillIn = narrow && paneFromPath === null
    const state = {
      ...(drillIn ? { [SUBNAV_PUSH_STATE]: true } : {}),
      ...(opts?.withAddAccounts ? { [ADD_ACCOUNTS_STATE]: true } : {}),
    }
    navigate(`${APP_PATH}/${p}`, {
      replace: !drillIn,
      // Undefined rather than an empty object: `location.state` is read
      // elsewhere for the push marker, and a bare {} is a value where the
      // reader of that code expects none.
      state: Object.keys(state).length > 0 ? state : undefined,
    })
  }
  // Selecting a row changes WHICH account the app is on and nothing else: the
  // reader stays on the Accounts pane, where the keys section below re-renders
  // for the account they just picked. The rail's switcher already stays put on
  // a select, so the two ways of selecting an account behave the same way;
  // jumping to Files from here made a row that looks informational teleport
  // the reader away from the keys they came to inspect.
  const useAccount = (a: AwsAccount) => {
    setStoredId(a.account)
  }
  const paneCount = (p: RailPane): number | undefined =>
    p === 'shares'
      ? sharesQ.data?.shares.length
      : p === 'files' || p === 'library' || p === 'backup'
        ? drive?.exists
          ? drive.usage.sections[p === 'files' ? 'drive' : p === 'library' ? 'library' : 'backup'].objects
          : undefined
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
  // on Overview — the read-only room that answers "is everything fine" before
  // the reader has to pick a question — while a narrow one shows the root LIST:
  // the same push-stack semantics as settings on a phone, where the bare path is
  // the list and a segment is a pushed detail.
  const pane: RailPane = paneFromPath ?? DEFAULT_PANE
  // A request carried on the navigation, not shell state: it must expire on the
  // next navigation rather than re-opening the disclosure on every later visit.
  const wantsAddAccounts = Boolean(
    (location.state as Record<string, unknown> | null)?.[ADD_ACCOUNTS_STATE],
  )

  const paneContent = (
    <>
      {pane === 'overview' && (
        <OverviewPane
          accountsQ={accountsQ}
          selected={selected}
          drive={drive}
          driveQ={driveQ}
          sharesQ={sharesQ}
          onUse={useAccount}
          onOpenPane={(p) => openPane(p)}
          onAddAccount={() => openPane('accounts', { withAddAccounts: true })}
        />
      )}
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
        <AccountsPane
          accountsQ={accountsQ}
          selected={selected}
          onUse={useAccount}
          openAddAccounts={wantsAddAccounts}
        />
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
              {TOP_PANES.map((p) => (
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
        {TOP_PANES.map((p) => (
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
