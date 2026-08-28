/**
 * AWS Control — Page 1, Accounts (P0).
 *
 * A dense, table-like list over the existing profile registry: one thin row per
 * AWS account leading with its name, a single health light and the full 12-digit
 * id, plus one quiet aggregate line ("N accounts · M keys · H healthy") with a
 * client-side search. Storage and cost are NOT measured in P0, so the aggregate
 * line only carries counts we actually have — it never invents a zero.
 *
 * The surface is read-only (spec §2.3): every mutation lives in the crew or a
 * dashboard confirmation card, not here. Rows carry no actions — they only
 * navigate to the per-account console, where Reconnect now lives. The only
 * writes on this page are the two paid-service consent gates mounted at the
 * bottom, which are their own durable-state components.
 */
import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Cloud, RefreshCw, ChevronRight, ChevronDown, Search } from 'lucide-react'
import { PageHeader, Btn, EmptyState, ContentSkeleton, Input } from '../../components/ui'
import AwsConsentGate from '../../components/AwsConsentGate'
import { api, type AwsConsentStatus } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { awsControlApi, AwsControlError } from './api'
import ConsoleView, { ReconnectAction } from './ConsoleView'
import DrivePage from './DrivePage'
import type { AwsAccount, AccountHealth } from './types'
import type { LiveDrive } from './DrivePage'

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

/**
 * One thin account row (~40px). Leads with a health dot and the account name,
 * then the full 12-digit id (mono, muted), and on the right a keys summary and a
 * chevron. A resolved row is the button that opens the console and carries no
 * other action. An UNRESOLVED row cannot open a console (there is no account to
 * show), so its click toggles the inline Reconnect guidance instead -- a red row
 * must always offer a way back to green.
 */
function AccountRow({ account, onOpen }: { account: AwsAccount; onOpen: () => void }) {
  const keys = account.profiles.length
  const resolved = Boolean(account.account)
  const [showReconnect, setShowReconnect] = useState(false)
  return (
    <div>
      <button
        onClick={resolved ? onOpen : () => setShowReconnect((v) => !v)}
        className="flex w-full items-center gap-3 px-3 py-2 text-left cursor-pointer bg-transparent border-none hover:bg-bg-hover focus-ring"
        data-testid="account-card"
        aria-label={i18nT(resolved ? 'apps.awsControl.page.open_console' : 'apps.awsControl.page.reconnect')}
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
        {account.account && (
          <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted" data-testid="account-id">
            {account.account}
          </span>
        )}
        {!account.account && <span className="flex-1" />}
        <span className="shrink-0 text-[12px] text-muted" data-testid="account-keys">
          {i18nT('apps.awsControl.page.keys_summary', { count: keys })}
        </span>
        {resolved ? (
          <ChevronRight size={14} className="shrink-0 text-muted" aria-hidden="true" />
        ) : (
          <ChevronDown size={14} className={`shrink-0 text-muted transition-transform ${showReconnect ? 'rotate-180' : ''}`} aria-hidden="true" />
        )}
      </button>
      {!resolved && showReconnect && account.profiles[0] && (
        <div className="px-3 pb-2" data-testid="row-reconnect">
          <ReconnectAction profile={account.profiles[0]} />
        </div>
      )}
    </div>
  )
}

/**
 * An "Add accounts" disclosure: lists the LOCAL profiles the CLI knows but the
 * portal has not registered, each with a checkbox, and registers the checked
 * set. It stays collapsed by default so the account list remains the page's
 * primary content. On success it invalidates the accounts query so a newly
 * registered profile appears without a manual refresh.
 */
function AddAccounts() {
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  // The set of profile NAMES the operator has ticked. Names, not indices, so a
  // list refetch that reorders rows can't silently move a checkmark to another
  // profile — registering the wrong profile is a trust error, not a UI glitch.
  const [checked, setChecked] = useState<Set<string>>(new Set())

  const availableQ = useQuery({
    queryKey: ['aws-control', 'profiles-available'],
    queryFn: () => awsControlApi.availableProfiles(),
  })

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
          {data && (
            <p className="mb-2 text-[12px] text-muted" data-testid="add-accounts-count">
              {i18nT('apps.awsControl.page.add_accounts_count', {
                count: data.registeredCount,
                max: data.max,
              })}
            </p>
          )}

          {unregistered.length === 0 ? (
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
                  screen so the operator knows nothing was added. */}
              {registerM.isError && (
                <p className="mt-2 text-[12px] text-danger" data-testid="add-accounts-error" role="alert">
                  {i18nT('apps.awsControl.page.add_accounts_error')}
                </p>
              )}

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

export default function AwsControlPage() {
  // The Console is view state INSIDE this page, not a route: BuiltinAppRoute
  // resolves single-segment routes only. Selecting an account row opens it; the
  // breadcrumb inside ConsoleView clears the selection to return here.
  const [selected, setSelected] = useState<AwsAccount | null>(null)
  const [drive, setDrive] = useState<LiveDrive | null>(null)
  const [query, setQuery] = useState('')

  const accountsQ = useQuery({
    queryKey: ['aws-control', 'accounts'],
    queryFn: () => awsControlApi.accounts(),
  })

  const refresh = () => accountsQ.refetch()

  const data = accountsQ.data

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
  // for. The console only shows a receipt whose grant matches that console's own
  // account, which means a grant matching NO registered account has no console
  // to live on and `revokeAwsConsent` has no caller anywhere - money confirmed
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
    // a withdraw control onto the ordinary accounts page on every load where the
    // consent read lands first - a destructive control offered by mistake, and
    // the exact section this page was cleaned of.
    if (!accountsQ.isSuccess) return false
    return !(data?.accounts ?? []).some((a) => a.account === owner)
  }
  const s3Orphan = orphaned(s3ConsentQ.data)
  const ceOrphan = orphaned(ceConsentQ.data)

  /* Three levels of view state, not routes: `BuiltinAppRoute` resolves only a
     single-segment route, so the accounts list, one account's console and that
     account's drive are all this component's state. The drive level is held HERE
     rather than inside the console so the console does not nest a second page
     within itself - each level renders exactly one surface. */
  if (selected && drive) {
    return (
      <DrivePage
        account={selected}
        drive={drive}
        onBack={() => setDrive(null)}
      />
    )
  }

  if (selected) {
    return (
      <ConsoleView
        account={selected}
        onBack={() => setSelected(null)}
        onOpenDrive={setDrive}
      />
    )
  }

  const header = (
    <PageHeader
      title={i18nT('apps.awsControl.page.title')}
      actions={
        <Btn onClick={refresh} disabled={accountsQ.isFetching} data-testid="refresh">
          <RefreshCw size={13} className={accountsQ.isFetching ? 'animate-spin' : ''} />
          {i18nT('apps.awsControl.page.refresh')}
        </Btn>
      }
    />
  )

  // A 403 app_disabled means the app was disabled after this bundle loaded (the
  // shell shows its own disabled state on first load). Show the standard
  // disabled-app copy rather than a raw error wall.
  if (accountsQ.isError && accountsQ.error instanceof AwsControlError && accountsQ.error.status === 403) {
    return (
      <div className="flex h-full flex-col">
        {header}
        <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
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

  return (
    <div className="flex h-full flex-col">
      {header}
      <div className="flex-1 overflow-y-auto px-4 pb-6 md:px-6">
        {/* Accounts and a client-side search over them. Nothing else lives on
            this page: the aggregate counts line was removed because the list it
            summarised is directly below it, and the paid-service consent gates
            moved onto the account they actually bill (see ConsoleView), where
            the connection they use is already on screen. */}
        <div className="flex flex-wrap items-center justify-end gap-2" data-testid="accounts-aggregate">
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

        {accountsQ.isError && !(accountsQ.error instanceof AwsControlError && accountsQ.error.status === 403) && (
          <div className="mt-4" data-testid="accounts-error">
            <EmptyState
              testId="aws-control-error"
              icon={<Cloud />}
              title={i18nT('apps.awsControl.page.error_title')}
              subtitle={i18nT('apps.awsControl.page.error_body')}
              action={
                <Btn onClick={refresh} data-testid="error-retry">
                  <RefreshCw size={13} />
                  {i18nT('apps.awsControl.page.retry')}
                </Btn>
              }
            />
          </div>
        )}

        {data && data.accounts.length === 0 && (
          <div className="mt-4" data-testid="accounts-empty">
            <EmptyState
              testId="aws-control-empty"
              icon={<Cloud />}
              title={i18nT('apps.awsControl.page.empty_title')}
              subtitle={i18nT('apps.awsControl.page.empty_body')}
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
              <AccountRow key={a.account || `unresolved-${i}`} account={a} onOpen={() => setSelected(a)} />
            ))}
          </div>
        )}

        {/* No paid-service section here. This page is the account list, and a
            confirmation is not an account: both paid services (s3 behind the
            drive, ce behind the cost figure) are reached from an account's
            console, so both the ask and the receipt live there. The earlier
            claim that this page was the only surface able to show a grant was
            wrong - the console already mounted both gates on refusal.

            The one exception below is not a section but a rescue: a grant whose
            recorded account is not registered here has no console to appear on,
            so without this it could never be withdrawn. It renders only in that
            state, so whenever an account owns the grant this page is accounts
            and nothing else. */}
        {(s3Orphan || ceOrphan) && (
          <div className="mt-6 flex flex-col gap-3" data-testid="orphan-consent">
            {/* This state needs its sentence more than any other surface here: the
                card names an AWS account that matches nothing in the list above
                it, and its only control is destructive. */}
            <p className="text-[13px] text-text" data-testid="orphan-consent-note">
              {i18nT('apps.awsControl.page.orphan_consent')}
            </p>
            {s3Orphan && <AwsConsentGate service="s3" />}
            {ceOrphan && <AwsConsentGate service="ce" />}
          </div>
        )}

        <AddAccounts />
      </div>
    </div>
  )
}
