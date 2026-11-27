/**
 * The Kiro Space account this page loaded under, and the full reload when it changes.
 *
 * The page keeps the account each store was signed in as when it first read
 * that store's status (`''` when signed out). When a later status read, or a
 * listing or detail answer, names another account, the page reloads, so
 * nothing read for one account outlives a switch to another: rows, counts,
 * the listing page and an open Install dialog all go with the old page. A
 * second change within 10 s of such a reload holds the store instead (no
 * rows, no Install, the status line) until the next read that differs, which
 * reloads, or a status read that names the loaded account again, which shows
 * the store again. So a status that flips faster than 10 s cannot loop, and one that
 * flips more slowly (say once per poll) reloads once per flip. The statuses
 * are read again when the tab is shown, every 30 seconds while it is
 * visible, and after the page's own sign-in or sign-out.
 */
import { useMemo, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type {
  MarketListingResponse, MarketListingsResponse, MarketRegistriesResponse, MarketRegistryStatus,
} from './types'

export const MARKET_REGISTRIES_QUERY_KEY = ['market-registries'] as const
/** How often a visible page re-reads the store statuses. */
export const MARKET_STATUS_POLL_MS = 30_000

/** The reload this module runs; an object so tests can replace it. */
export const marketPage = { reload: () => window.location.reload() }

/** `sessionStorage` key of the time of this tab's last account-change reload. */
export const MARKET_RELOAD_AT_KEY = 'kirocrew.marketReloadAt'
/** A second account change this soon after a reload holds the store instead of reloading. */
export const MARKET_RELOAD_GAP_MS = 10_000

// One page load: a reload starts this module, and so these, again.
const loadedUnder = new Map<string, string>()
/**
 * The held stores, and the statuses of the last shared status read that threw
 * (react-query then keeps the read before it, which can name the old account).
 */
interface HeldView {
  stores: ReadonlySet<string>
  status: Record<string, MarketRegistryStatus> | null
}
let heldView: HeldView = { stores: new Set(), status: null }
const heldListeners = new Set<() => void>()

function publish(next: Partial<HeldView>): void {
  heldView = { ...heldView, ...next }
  for (const listener of heldListeners) listener()
}

/** For tests: forget the accounts and the held stores, as a reload would. */
export function resetMarketAccountsForTests(): void {
  loadedUnder.clear()
  heldView = { stores: new Set(), status: null }
}

/** The account the page recorded for *registry*: what the Install dialog sends with prepare. */
export function pageAccount(registry: string): string | undefined {
  return loadedUnder.get(registry)
}

/** The account a status names: the signed-in one, `''` when signed out, `null` when unknown. */
export function knownAccount(status: MarketRegistryStatus | undefined): string | null {
  if (status?.state === 'signed_in') return status.account
  return status?.state === 'needs_sign_in' || status?.state === 'no_access' ? '' : null
}

/** Thrown in place of an answer read for another account than the page's. */
export class MarketAccountChanged extends Error {
  constructor() {
    super('market_account_changed')
  }
}

/**
 * Reload for an account change, unless this tab already did within
 * `MARKET_RELOAD_GAP_MS`: then hold every store in *registries* (no rows, the
 * status line). The next differing read after the gap reloads, so this stops
 * only a loop faster than the gap; a status that flips once per poll reloads
 * once per poll.
 */
function reloadOrHold(registries: readonly string[]): void {
  const now = Date.now()
  let last = 0
  try {
    last = Number(window.sessionStorage.getItem(MARKET_RELOAD_AT_KEY)) || 0
  } catch {
    // shortcut: no sessionStorage means no backstop; every change reloads.
  }
  if (last && now - last >= 0 && now - last < MARKET_RELOAD_GAP_MS) {
    const fresh = registries.filter(id => !heldView.stores.has(id))
    if (fresh.length) publish({ stores: new Set([...heldView.stores, ...fresh]) })
    return
  }
  try {
    window.sessionStorage.setItem(MARKET_RELOAD_AT_KEY, String(now))
  } catch {
    // As above.
  }
  marketPage.reload()
}

/**
 * Record the first known account of each store in *statuses*. When any later
 * one differs, reload (or hold every store that differs), and throw so the
 * answer is never stored. Every store is checked before the throw.
 */
function keepAccounts(
  statuses: Record<string, MarketRegistryStatus | undefined>,
  canRelease = false,
): void {
  const moved: string[] = []
  const back: string[] = []
  for (const [registry, status] of Object.entries(statuses)) {
    const account = knownAccount(status)
    if (account === null) continue
    const loaded = loadedUnder.get(registry)
    if (loaded === undefined) loadedUnder.set(registry, account)
    else if (loaded !== account) moved.push(registry)
    else if (heldView.stores.has(registry)) back.push(registry)
  }
  // A held store whose loaded account is back shows its rows again, but only
  // on a fresh shared status read: a late listing or detail answer read before
  // the switch must not release it.
  if (back.length && canRelease) {
    publish({ stores: new Set([...heldView.stores].filter(id => !back.includes(id))) })
  }
  if (moved.length === 0) return
  reloadOrHold(moved)
  throw new MarketAccountChanged()
}

function keepAccount(registry: string, status: MarketRegistryStatus | undefined): void {
  keepAccounts({ [registry]: status })
}

export async function readMarketRegistries(): Promise<MarketRegistriesResponse> {
  const data = await api.getMarketRegistries()
  try {
    keepAccounts(data.status ?? {}, true)
  } catch (err) {
    publish({ status: data.status ?? {} })
    throw err
  }
  if (heldView.status !== null) publish({ status: null })
  return data
}

export async function readMarketListings(registry: string): Promise<MarketListingsResponse> {
  const data = await api.listMarketListings(registry)
  keepAccount(registry, data.status)
  return data
}

export async function readMarketListing(registry: string, ref: string): Promise<MarketListingResponse> {
  const data = await api.getMarketListing(registry, ref)
  keepAccount(registry, data.status)
  return data
}

/** The shared store-status read: on tab focus, every 30 s while visible, and on demand. */
export function useMarketRegistriesQuery() {
  return useQuery({
    queryKey: MARKET_REGISTRIES_QUERY_KEY,
    queryFn: readMarketRegistries,
    staleTime: MARKET_STATUS_POLL_MS,
    refetchInterval: MARKET_STATUS_POLL_MS,
    refetchOnWindowFocus: 'always',
  })
}

/** The shared store-status read's answer. */
export function useMarketRegistries(): MarketRegistriesResponse | undefined {
  return useMarketRegistriesQuery().data
}

/**
 * Why the latest shared status read failed: its error message, also when an
 * earlier answer is still shown, unless it threw for an account change (the
 * page reloads or holds then).
 */
export function registriesReadError(error: unknown): string | null {
  if (!error || error instanceof MarketAccountChanged) return null
  return (error as Error).message || null
}

function subscribeHeld(listener: () => void): () => void {
  heldListeners.add(listener)
  return () => { heldListeners.delete(listener) }
}

function useHeldView(): HeldView {
  return useSyncExternalStore(subscribeHeld, () => heldView)
}

/**
 * Each store's latest status: the shared read's answer, or, when that read
 * threw for an account change, what it read. So a held store's status line
 * and Sign out never name the old account.
 */
export function useMarketStatuses(): Record<string, MarketRegistryStatus> | undefined {
  const data = useMarketRegistries()
  const { status } = useHeldView()
  return status ?? data?.status
}

/**
 * The stores whose rows and Install the page hides: held ones, and those the
 * shared status read finds `unknown` (it names no account, so a switch made
 * meanwhile could not be seen).
 */
export function useHiddenStores(): ReadonlySet<string> {
  const statuses = useMarketStatuses()
  const { stores } = useHeldView()
  return useMemo(() => {
    const out = new Set(stores)
    for (const [id, status] of Object.entries(statuses ?? {})) {
      if (status.state === 'unknown') out.add(id)
    }
    return out
  }, [statuses, stores])
}
