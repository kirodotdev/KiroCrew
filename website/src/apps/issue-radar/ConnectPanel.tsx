// Shared "connect a source" panel — used by BOTH the first-run WelcomeCarousel
// (as its last slide) and the ConnectRepoModal (the "connect another repo"
// overlay), so the two flows can never drift apart.
//
// Layout, in two states:
//
//   1. Collapsed — a vertical stack of provider ROWS (horizontal bars), one per
//      source. Replaced the single large GitHub square: a row list scales to N
//      providers, a square does not.
//
//   2. Expanded (after picking GitHub) — the host card GROWS (see
//      `EXPANDED_CARD` / how ConnectRepoModal and WelcomeCarousel apply it) and
//      the body becomes two columns: the provider rows stay on the LEFT, and
//      the RIGHT column holds everything GitHub-specific — the user's
//      recently-pushed repos as a MULTI-select, plus the manual URL entry
//      beneath them. The URL field sits on the right (not under the provider
//      rows) because pasting a repo link is part of the GitHub flow, not a
//      footer of the provider list. Pasting a URL and ticking repos are
//      additive — the Connect action submits every selected target, so a user
//      can add a repo that isn't in the recent list without losing their ticks.
//
// Only GitHub is wired to a backend today (Issue Radar reads issues through the
// user's own `gh` CLI). GitLab/Jira/Linear render as rows with a "Soon" badge
// and are non-selectable — visible roadmap, not a dead end that 404s.
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, Check, Orbit, RefreshCw, SquareKanban } from 'lucide-react'
import {
  issueRadarApi, type GhSetupReason, type RecentRepo, type RepoRef, type SourceProvider,
} from './api'
import { providerTerms } from './lib/links'
import { relativeTimeOrDate } from './lib/format'
import type { ActiveRepo } from './lib/types'
import GithubLogo from '../../components/icons/GithubLogo'
import GitlabLogo from '../../components/icons/GitlabLogo'

export type ProviderId = 'github' | 'gitlab' | 'jira' | 'linear'

/** Tailwind classes for the host card in each state — exported so the carousel
 * and the modal stay dimensionally identical without duplicating the numbers.
 * FIXED heights (h-, not min-h-): the expanded card must not grow or shrink
 * when the async repo list resolves, so its box is reserved up front. The
 * expanded height is sized to fit header + list + URL row + nav footer; the
 * hosts additionally clip their body (min-h-0 + overflow-y-auto) so a content
 * overrun can never push the Back/Connect row past the card edge.
 *
 * `max-w-full max-h-full` bounds both boxes to the overlay container (the
 * Issue Radar app area, which is what the modal/carousel cover): the fixed
 * 860x540 exceeds a narrow or short app viewport, and without the cap the
 * dismiss and Connect controls get clipped off-screen with no way to reach
 * them. Under the cap the body scrolls internally instead. */
export const COLLAPSED_CARD = 'w-[480px] h-[420px] max-w-full max-h-full'
export const EXPANDED_CARD = 'w-[860px] h-[540px] max-w-full max-h-full'

/** Width of the provider list. Full-card-width rows read as oversized banners
 * (a 480px-wide row for the word "GitHub"), so the column is capped and
 * centred when collapsed.
 *
 * `w-full max-w-[300px]`, not a fixed `w-[300px]`: once the card is capped by
 * `max-w-full` on a narrow viewport, a rigid 300px column plus a gap consumed
 * the whole row and left the repo picker and URL field at zero width — i.e.
 * unusable. Capped instead, the two columns share what's actually available. */
const PROVIDER_COL = 'w-full max-w-[300px]'

/** Below this card width the two-column expanded layout stops working — a
 * 132px provider column plus a gap leaves the picker ~64px wide, i.e. unusable
 * — so the columns STACK and the body scrolls instead. A container query on the
 * card is not available here (the breakpoint must react to the card, not the
 * window, since the modal is scoped to the app area), so the panel measures its
 * own width. */
const STACK_BELOW_PX = 640

/** Only repos pushed to within this trailing window appear in the picker. The
 * server applies the cutoff (GitHub's user/repos can sort but not filter by
 * date), so this is passed through as the `days` query param. */
export const RECENT_WINDOW_DAYS = 30

/** Marks a ConnectTarget that came from the typed URL rather than a tick, so a
 * successful connect can clear the input the same way it clears a tick. */
const URL_TARGET_PREFIX = 'url:'

/** Case-folded `owner/repo` identity for a GitHub repo URL, or null when the
 * text isn't one. Used to dedupe a typed URL against the ticked repos.
 *
 * A literal string compare missed every spelling that resolves to the SAME
 * repo — `WWW.`/`www.` host, a `.git` suffix, a trailing slash, or different
 * casing (GitHub names are case-preserving but not case-sensitive). Each miss
 * submitted a second connect for a repo already in the list, which the server
 * then rejects as already connected — or, worse, stores under a second casing. */
export function repoIdentity(text: string): string | null {
  const parsed = parseRepoRef(text)
  return parsed && `${parsed.owner}/${parsed.repo}`.toLowerCase()
}

/** The owner/repo of a GitHub repo reference, with its ORIGINAL casing intact,
 * or null when the text isn't one.
 *
 * Case is preserved deliberately: the backend stores `owner`/`repo` verbatim,
 * so submitting a folded `acme/widget` for an already-connected `Acme/Widget`
 * appends a SECOND entry with its own caches and settings. Folding is for
 * comparison only — see repoIdentity. */
export function parseRepoRef(
  text: string,
  provider: ProviderId = 'github',
): { owner: string; repo: string } | null {
  const trimmed = text.trim()
  if (!trimmed) return null
  // Tolerate a bare `owner/repo` and a scheme-less host, which is what people
  // paste about as often as a full URL. A first segment with no dot cannot be a
  // hostname, so it is treated as an owner on github.com.
  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
  const looksHostless = !hasScheme && !trimmed.split('/')[0].includes('.')
  // A hostless shorthand resolves against the SELECTED provider's public host —
  // assuming github.com while the GitLab panel is open would connect a different
  // project entirely.
  const defaultHost = provider === 'gitlab' ? 'gitlab.com' : 'github.com'
  const withScheme = hasScheme
    ? trimmed
    : looksHostless ? `https://${defaultHost}/${trimmed}` : `https://${trimmed}`
  let host: string
  let path: string
  try {
    const u = new URL(withScheme)
    host = u.hostname.toLowerCase().replace(/^www\./, '')
    path = u.pathname
  } catch {
    return null
  }
  // Only the provider's PUBLIC host is recognised here. A self-managed GitLab is
  // deliberately not shorthand-parsed: the client has no view of the operator's
  // `dashboard.gitlab_hosts` allowlist, so guessing would produce a canonical URL
  // the server then rejects. Such a URL is submitted verbatim instead, and the
  // server's allowlist decision is the honest answer the user sees.
  if (host && host !== defaultHost) return null
  // Everything from GitLab's `/-/` routing marker onward is a page within the
  // project, not part of its path, so a pasted issues/MR tab still resolves.
  const marker = path.toLowerCase().indexOf('/-/')
  const projectPath = marker >= 0 ? path.slice(0, marker) : path
  // Trailing slashes come off FIRST: `.../repo.git/` would otherwise keep its
  // `.git` (the anchor never matches) and count as a second, distinct repo.
  const parts = projectPath.replace(/\/+$/, '').replace(/\.git$/i, '').split('/').filter(Boolean)
  if (parts.length < 2) return null
  // GitLab projects live in nested groups, so the namespace is EVERY segment but
  // the last — truncating to the first would address a different project.
  return provider === 'gitlab'
    ? { owner: parts.slice(0, -1).join('/'), repo: parts[parts.length - 1] }
    : { owner: parts[0], repo: parts[1] }
}

interface Provider {
  id: ProviderId
  label: string
  icon: React.ReactNode
  /** false → rendered with a "Soon" badge and not selectable. */
  available: boolean
}

// GitHub and GitLab reuse the repo's existing brand-mark components. Jira and
// Linear use lucide glyphs instead: the `use-lucide-icons` rule forbids adding
// hand-rolled SVG paths, and no brand marks for them exist in the repo yet.
// They are placeholders next to a "Soon" badge, so a representative glyph
// (board / orbit) reads acceptably until either provider is actually wired up.
const PROVIDERS: Provider[] = [
  { id: 'github', label: 'GitHub', icon: <GithubLogo size={18} />, available: true },
  { id: 'gitlab', label: 'GitLab', icon: <GitlabLogo size={18} />, available: true },
  { id: 'jira', label: 'Jira', icon: <SquareKanban size={18} />, available: false },
  { id: 'linear', label: 'Linear', icon: <Orbit size={18} />, available: false },
]

/** One connect target: either a ticked recent repo or the manually typed URL. */
export interface ConnectTarget {
  key: string
  /** What `POST /connect` receives — it parses owner/repo out of the URL. */
  url: string
  label: string
}

export interface ConnectFlow {
  provider: ProviderId | null
  selectProvider: (p: ProviderId) => void
  /** Clear the provider (drives the carousel's two-level Back). */
  clearProvider: () => void
  url: string
  setUrl: (u: string) => void
  /** `owner/repo` keys ticked in the recent-repos column. */
  picked: Set<string>
  togglePicked: (fullName: string) => void
  targets: ConnectTarget[]
  submit: () => void
  pending: boolean
  /** Aggregate error text (per-target failures are joined), or null. */
  error: string | null
  /** "Connecting 2 of 3…" progress while a multi-connect runs. */
  progress: { done: number; total: number } | null
  /** Drop every queued target (ticks + typed URL). Used when the panel learns
   * nothing is connectable, so Connect can't be armed for a certain failure. */
  reset: () => void
}

/** Owns every piece of connect state the host card's action button needs.
 * Lifted into a hook (not kept inside the panel) because both hosts render the
 * Connect button OUTSIDE the panel body — the carousel in its nav row, the
 * modal in its footer. */
export function useConnectFlow(onConnected: (repo: ActiveRepo) => void): ConnectFlow {
  const queryClient = useQueryClient()
  const [provider, setProvider] = useState<ProviderId | null>(null)
  const [url, setUrl] = useState('')
  const [picked, setPicked] = useState<Set<string>>(() => new Set())
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [errors, setErrors] = useState<string[]>([])

  // The connect loop is sequential and long-lived, so it outlives its host: an
  // SPA navigation (or any unmount the pending-dismissal guard can't intercept)
  // leaves it connecting the remaining repos with no visible progress and
  // nowhere to report a failure. This flag makes it stop at the next boundary.
  const cancelledRef = useRef(false)
  useEffect(() => {
    // Reset on SETUP, not just on teardown: React StrictMode runs
    // mount→cleanup→mount in development, so a teardown-only version left the
    // flag latched true and every subsequent connect bailed before its first
    // request.
    cancelledRef.current = false
    return () => { cancelledRef.current = true }
  }, [])
  /** Size of the batch handed to `submit`, so completion can be verified
   * against what was actually dispatched rather than what's left selected. */
  const targetCountRef = useRef(0)

  const publicHost = provider === 'gitlab' ? 'gitlab.com' : 'github.com'
  const targets = useMemo<ConnectTarget[]>(() => {
    const out: ConnectTarget[] = [...picked].map((fullName) => ({
      key: fullName,
      url: `https://${publicHost}/${fullName}`,
      label: fullName,
    }))
    const typed = url.trim()
    // A typed URL that duplicates a tick is submitted once, not twice — matched
    // on normalised owner/repo identity, not raw text (see repoIdentity).
    if (typed) {
      const typedId = repoIdentity(typed)
      const already = typedId !== null && out.some((t) => repoIdentity(t.url) === typedId)
      if (!already) {
        // Submit the CANONICAL url when the text parses — `parseRepoRef` accepts
        // shorthand the backend does not (a bare `owner/repo`, a scheme-less
        // host), which would come back as a 400 from its URL parser. Case is
        // taken from `parseRepoRef`, NOT the folded identity: the backend stores
        // owner/repo verbatim, so a folded name would be persisted as a second,
        // separate repo. Unparseable text is submitted as-is so the server's
        // error stays the honest one.
        const ref = parseRepoRef(typed, provider ?? 'github')
        const url = ref ? `https://${publicHost}/${ref.owner}/${ref.repo}` : typed
        out.push({ key: `${URL_TARGET_PREFIX}${typed}`, url, label: typed })
      }
    }
    return out
  }, [picked, url, provider, publicHost])

  const connectMutation = useMutation({
    mutationFn: async (list: ConnectTarget[]) => {
      // Sequential, not Promise.all: each connect shells out to `gh`, and a
      // burst of parallel calls just fights over the rate limit. Failures are
      // collected rather than thrown so one bad URL can't discard the repos
      // that DID connect.
      const failures: string[] = []
      const succeeded: string[] = []
      // Carries provider + host, which the backend resolved from the URL. Dropping
      // them here would hand the app a GitHub-shaped ref for a GitLab project, and
      // every later request would be authorized against the wrong provider — a
      // 404 "not connected" on a repo that was just connected successfully.
      let first: ActiveRepo | null = null
      for (let i = 0; i < list.length; i++) {
        // Checked BEFORE each request, so at most the in-flight one completes.
        if (cancelledRef.current) break
        setProgress({ done: i, total: list.length })
        try {
          const res = await issueRadarApi.connect(list[i].url)
          succeeded.push(list[i].key)
          if (!first) {
            first = {
              owner: res.owner,
              repo: res.repo,
              provider: res.provider,
              host: res.host,
            }
          }
        } catch (e) {
          failures.push(`${list[i].label}: ${(e as Error).message}`)
        }
      }
      setProgress(null)
      // `cancelled` is distinct from `failures`: a batch cut short by unmount
      // can have zero failures and still be incomplete, and reporting that as a
      // full success would close the flow over silently skipped targets.
      return { first, failures, succeeded, cancelled: cancelledRef.current }
    },
    onSuccess: ({ first, failures, succeeded, cancelled }) => {
      setErrors(failures)
      // Drop EVERY target that made it — ticked repos and the typed URL alike —
      // so the leftover selection is exactly what still needs attention. The
      // typed URL used to survive its own success and be resubmitted on the
      // next click, failing the second time as "already connected".
      if (succeeded.length) {
        const succeededKeys = new Set(succeeded)
        setPicked((prev) => {
          const next = new Set(prev)
          for (const key of succeededKeys) next.delete(key)
          return next
        })
        if ([...succeededKeys].some((k) => k.startsWith(URL_TARGET_PREFIX))) setUrl('')
        // This picker's "Connected" rows are now stale, so refresh them even on
        // a partial failure — the dialog stays open and must not offer a repo it
        // just connected.
        queryClient.invalidateQueries({ queryKey: ['issue-radar', 'recent-repos'] })
        // `repos`, by contrast, is deferred to the all-succeeded path below: on
        // FIRST RUN it is what decides whether onboarding is still showing, so
        // invalidating it mid-partial-failure unmounts the carousel and takes
        // the unread error list with it.
      }
      // Only hand control back — which closes the dialog / leaves onboarding —
      // when EVERY target succeeded. On a partial failure the dialog has to
      // stay open, otherwise it unmounts with the error list still unread and
      // a bulk connect looks like it fully succeeded.
      const complete = !cancelled && failures.length === 0 && succeeded.length === targetCountRef.current
      if (first && complete) {
        queryClient.invalidateQueries({ queryKey: ['issue-radar', 'repos'] })
        onConnected(first)
      }
    },
    onError: (e) => {
      setProgress(null)
      setErrors([(e as Error).message])
    },
  })

  return {
    provider,
    selectProvider: (p) => setProvider(p),
    clearProvider: () => setProvider(null),
    url,
    setUrl,
    picked,
    togglePicked: (fullName) => setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(fullName)) next.delete(fullName)
      else next.add(fullName)
      return next
    }),
    targets,
    submit: () => {
      if (!targets.length || connectMutation.isPending) return
      setErrors([])
      targetCountRef.current = targets.length
      connectMutation.mutate(targets)
    },
    pending: connectMutation.isPending,
    error: errors.length ? errors.join(' · ') : null,
    progress,
    reset: () => {
      setPicked((prev) => (prev.size ? new Set() : prev))
      setUrl((prev) => (prev ? '' : prev))
    },
  }
}

/** The panel body. Collapsed = provider rows only; expanded = rows on the left,
 * repo multi-select (or setup notice) on the right.
 *
 * The recent-repos query lives HERE, not inside the picker, because its result
 * gates more than the list: when `gh` isn't set up there is nothing the user
 * can usefully connect, so the manual URL field is hidden too (pasting a URL
 * would just fail the same way) and the whole right column becomes the setup
 * notice. */
export default function ConnectPanel({ flow }: { flow: ConnectFlow }) {
  // Both wired providers expand into the two-column body. Jira/Linear stay
  // collapsed because they are still placeholders.
  const expanded = flow.provider === 'github' || flow.provider === 'gitlab'
  const scopeProvider = flow.provider === 'gitlab' ? ('gitlab' as const) : ('github' as const)

  const query = useQuery({
    // Keyed by provider: the two lists come from different accounts on different
    // CLIs, so sharing one cache entry would show GitHub repos in the GitLab
    // picker (and mark the wrong ones "Connected").
    queryKey: ['issue-radar', 'recent-repos', RECENT_WINDOW_DAYS, scopeProvider],
    queryFn: () => issueRadarApi.recentRepos(RECENT_WINDOW_DAYS, { provider: scopeProvider }),
    // Only fetch once a wired provider's panel is actually open, and don't
    // re-shell out to the CLI on every window focus.
    enabled: expanded,
    refetchOnWindowFocus: false,
  })
  const setupRequired = query.data?.setup_required ?? null

  // Nothing here is connectable without a working `gh`, so any target the user
  // picked before the query resolved is now a guaranteed failure — and the URL
  // field it may have come from is gone, leaving no way to clear it. Dropping
  // them disables Connect instead of arming it for a certain 502.
  useEffect(() => {
    if (setupRequired) flow.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setupRequired])

  // Stack the columns on a narrow card — see STACK_BELOW_PX.
  const bodyRef = useRef<HTMLDivElement>(null)
  const [stacked, setStacked] = useState(false)
  useEffect(() => {
    const el = bodyRef.current
    if (!el || !expanded) return
    const measure = () => setStacked(el.clientWidth > 0 && el.clientWidth < STACK_BELOW_PX)
    measure()
    // ResizeObserver, not a window listener: the card itself resizes when the
    // provider expands, with no window event to hook.
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [expanded])

  return (
    <div className="flex flex-col gap-4 w-full flex-1 min-h-0 text-left">
      <div className="text-center flex-shrink-0">
        <div className="text-[20px] font-bold text-text tracking-[-0.2px]">Let's Connect a Provider</div>
        <div className="text-[13.5px] text-muted leading-[1.7] mt-1.5">
          Connect a provider. Nothing runs without your say.
        </div>
      </div>

      {/* The columns row absorbs the card's remaining height (flex-1 min-h-0)
       * and the repo list inside it flexes, so the URL row below can never be
       * pushed out of the card — earlier this was a fixed 240px list plus a
       * fixed URL block, whose combined height overran the card by a few px
       * and got clipped. */}
      <div
        ref={bodyRef}
        className={`flex-1 min-h-0 flex ${
          expanded
            ? stacked ? 'flex-col gap-4 overflow-y-auto' : 'gap-5'
            : 'justify-center'
        }`}
      >
        <div className={`${PROVIDER_COL} ${expanded && !stacked ? 'min-w-[180px]' : 'flex-shrink-0'} self-start flex flex-col gap-1.5`}>
          {PROVIDERS.map((p) => (
            <ProviderRow
              key={p.id}
              provider={p}
              selected={flow.provider === p.id}
              onSelect={() => flow.selectProvider(p.id)}
            />
          ))}
        </div>

        {/* Right column — everything GitHub-specific: the repo multi-select (or
         * the setup notice) and, when usable, the manual URL entry. */}
        {expanded && (
          <div
            className={`flex-1 min-w-0 flex flex-col gap-3 ${
              stacked ? 'border-t border-border pt-4 min-h-[220px]' : 'min-h-0 border-l border-border pl-5'
            }`}
          >
            <RecentRepoPicker
              picked={flow.picked}
              onToggle={flow.togglePicked}
              // submit() snapshots the target list, so a tick added mid-flight
              // is silently dropped when a full success closes the dialog.
              // Freezing the controls makes the in-flight set the real one.
              disabled={flow.pending}
              repos={query.data?.repos ?? []}
              truncated={query.data?.truncated ?? false}
              setupRequired={setupRequired}
              isLoading={query.isLoading}
              error={query.isError ? (query.error as Error).message : null}
              detail={query.data?.error ?? null}
              onRetry={() => query.refetch()}
              // The setup notice names a CLI, and the two providers use
              // different ones — telling a GitLab user to install `gh` sends
              // them to set up the wrong tool on the one screen meant to
              // unblock them.
              scopeProvider={scopeProvider}
            />

            {!setupRequired && (
              <div className="flex flex-col gap-2 border-t border-border pt-3 flex-shrink-0">
                <span className="text-[11px] font-semibold text-muted uppercase tracking-[.08em] opacity-70">
                  Or paste a URL
                </span>
                <input
                  id="ir-repo-url"
                  aria-label="Repository URL"
                  value={flow.url}
                  onChange={(e) => flow.setUrl(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') flow.submit() }}
                  disabled={flow.pending}
                  placeholder="https://github.com/<owner>/<repo> or https://gitlab.com/<group>/<project>"
                  className="w-full box-border text-[12.5px] px-3 py-2 rounded-md bg-bg text-text border border-border font-mono disabled:opacity-50"
                />
              </div>
            )}
          </div>
        )}
      </div>

      {flow.error && (
        <div className="flex items-start gap-1.5 text-danger text-xs flex-shrink-0">
          <AlertCircle size={13} className="flex-shrink-0 mt-0.5" />
          <span className="break-words">{flow.error}</span>
        </div>
      )}
    </div>
  )
}

function ProviderRow({ provider, selected, onSelect }: {
  provider: Provider; selected: boolean; onSelect: () => void
}) {
  const disabled = !provider.available
  return (
    <button
      onClick={disabled ? undefined : onSelect}
      disabled={disabled}
      aria-pressed={selected}
      className={`w-full flex items-center gap-3 px-3 h-10 flex-shrink-0 rounded-md border text-left transition-colors ${
        disabled
          ? 'border-border bg-transparent opacity-45 cursor-default'
          : selected
            ? 'border-accent bg-accent-subtle cursor-pointer'
            : 'border-border bg-transparent hover:bg-bg-hover cursor-pointer'
      }`}
    >
      <span className={`flex-shrink-0 ${selected ? 'text-accent' : 'text-text'}`}>{provider.icon}</span>
      <span className="flex-1 min-w-0 text-[13px] font-semibold text-text truncate">
        {provider.label}
      </span>
      {disabled
        ? <span className="flex-shrink-0 text-[10px] font-semibold text-muted uppercase tracking-[.08em] px-1.5 py-0.5 rounded border border-border">Soon</span>
        : selected
          ? <Check size={14} className="flex-shrink-0 text-accent" />
          : null}
    </button>
  )
}

/** Right column: repos the user personally contributed to in the window,
 * multi-select. Presentational — the query lives in ConnectPanel (see there).
 *
 * The scroll area is a FIXED height (not min/max): the card must not resize
 * when the repo list arrives, so every state — loading, setup notice, error,
 * empty, loaded — occupies exactly the same box. */
function RecentRepoPicker({
  picked, onToggle, repos, truncated, setupRequired, isLoading, error, detail, onRetry, disabled,
  scopeProvider,
}: {
  picked: Set<string>
  onToggle: (fullName: string) => void
  repos: RecentRepo[]
  truncated: boolean
  setupRequired: GhSetupReason | null
  isLoading: boolean
  error: string | null
  detail: string | null
  onRetry: () => void
  /** True while a connect is in flight — rows stop accepting changes. */
  disabled: boolean
  /** Which provider's account this picker is listing — drives the CLI named by
   * the setup notice. */
  scopeProvider: SourceProvider
}) {
  const showList = !isLoading && !error && !setupRequired
  // Never claim a count when the feed was truncated: repos contributed to
  // earlier in the window may be missing, and a picker that looks exhaustive
  // makes the user conclude they didn't work on a repo they did.
  const countLabel = picked.size > 0
    ? `${picked.size} selected`
    : showList
      ? (truncated ? 'most recent activity' : `${repos.length} found`)
      : ''

  return (
    <div className="flex flex-col gap-2 flex-1 min-h-0">
      {/* Header row is suppressed in the setup-error state: a "GITHUB CLI"
       * column label above an error message is noise, and the row would only
       * push the message further down. The h-4 spacer is kept so the right
       * column still lines up with the provider list. */}
      <div className="flex items-center justify-between gap-2 h-4 flex-shrink-0">
        {!setupRequired && (
          <>
            <span className="text-[11px] font-semibold text-muted uppercase tracking-[.08em] opacity-70">
              You contributed to
            </span>
            <span className="text-[11px] text-muted">{countLabel}</span>
          </>
        )}
      </div>

      {/* flex-1 min-h-0, not a fixed height: the card's height is what's fixed,
       * so the list absorbs whatever is left after the header and the URL row.
       * The card therefore still never resizes when the list resolves, and the
       * URL row can never be pushed past the card edge. */}
      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-1 pr-0.5">
        {isLoading && (
          <div className="flex items-center gap-2 text-muted text-xs h-full justify-center">
            <RefreshCw size={12} className="animate-spin" /> Loading your repos…
          </div>
        )}
        {error && !setupRequired && (
          <div className="text-xs text-danger h-full flex items-center justify-center text-center px-2">
            {error}
          </div>
        )}
        {setupRequired && (
          <ProviderSetupNotice detail={detail} onRetry={onRetry} provider={scopeProvider} />
        )}
        {showList && repos.length === 0 && (
          <div className="text-xs text-muted h-full flex items-center justify-center text-center px-3 leading-[1.6]">
            {/* Never claim the month was empty when the feed was truncated: the
              * user's contributions may simply lie beyond the fetched window,
              * and telling them they did nothing is a plain falsehood. */}
            {truncated
              ? 'Couldn\u2019t read far enough back through your activity \u2014 paste a URL below instead.'
              : 'No contributions in the last month \u2014 paste a URL below instead.'}
          </div>
        )}
        {showList && repos.map((r) => (
          <RepoRow
            key={r.full_name}
            repo={r}
            checked={picked.has(r.full_name)}
            onToggle={() => onToggle(r.full_name)}
            disabled={disabled}
          />
        ))}
      </div>
    </div>
  )
}

/** Shown instead of the repo list when the host has no usable provider CLI.
 * Issue Radar reads each provider exclusively through the user's own CLI
 * session, so there is no in-app fallback and nothing to connect — one plain
 * "set it up" message, the same for every underlying cause (binary missing,
 * rejected by trust validation, or not signed in). The server's specific
 * diagnostic stays in the collapsible Details block for anyone who needs it.
 *
 * The CLI is named from the PROVIDER being connected, not hard-coded: this is
 * the one screen whose whole job is unblocking the user, so naming `gh` to
 * someone connecting GitLab sends them to install the wrong tool and leaves them
 * exactly as stuck after following the instructions.
 *
 * Left-aligned and top-anchored: centred text in a tall box reads as a stray
 * fragment floating mid-column, and it broke alignment with the provider list
 * across the divider. */
function ProviderSetupNotice({ detail, onRetry, provider }: {
  detail: string | null; onRetry: () => void; provider: SourceProvider
}) {
  const terms = providerTerms({ provider } as RepoRef)
  return (
    <div className="flex flex-col items-start gap-2 text-left pr-2">
      <AlertCircle size={18} className="text-danger flex-shrink-0" />
      <p className="text-[13px] font-semibold text-text">
        Please set up the {terms.providerName} CLI
      </p>
      <p className="text-[11.5px] text-muted leading-[1.6]">
        Issue Radar reads {terms.providerName} through your own <code>{terms.cli}</code> session.
        Install and sign in to{' '}
        <code>{terms.cli}</code>, then{' '}
        <button
          onClick={onRetry}
          className="underline text-accent bg-transparent border-0 p-0 cursor-pointer text-[11.5px]"
        >
          check again
        </button>
        .
      </p>
      {detail && (
        <details className="text-[10.5px] text-muted opacity-60 max-w-full">
          <summary className="cursor-pointer">Details</summary>
          <p className="mt-1 leading-[1.5] break-words">{detail}</p>
        </details>
      )}
    </div>
  )
}

/** One picker row. Fixed height (h-9) so the list's scroll extent — and
 * therefore the card — is identical whether rows carry long or short names. */
function RepoRow({ repo, checked, onToggle, disabled }: {
  repo: RecentRepo; checked: boolean; onToggle: () => void; disabled: boolean
}) {
  const inputId = useId()
  const when = repo.last_contributed_at ? relativeTimeOrDate(repo.last_contributed_at) : ''

  if (repo.connected) {
    return (
      <div className="flex items-center gap-2.5 px-2.5 h-9 flex-shrink-0 rounded-md opacity-45">
        <Check size={13} className="flex-shrink-0 text-accent" />
        <span className="flex-1 min-w-0 text-[12.5px] text-text truncate font-mono">{repo.full_name}</span>
        <span className="flex-shrink-0 text-[10.5px] text-muted">Connected</span>
      </div>
    )
  }

  // `useId`, not a slug derived from the repo name: sanitising punctuation to
  // `-` was NOT injective, so legitimately distinct names collided (`a.b` and
  // `a-b` both became `a-b`) and clicking one row's label toggled the other
  // row's checkbox. The input is also nested inside the label, which the
  // `label-has-for` rule requires alongside the id.
  return (
    <label
      htmlFor={inputId}
      className={`flex items-center gap-2.5 px-2.5 h-9 flex-shrink-0 rounded-md border ${
        disabled ? 'cursor-default opacity-50' : 'cursor-pointer'
      } ${checked ? 'border-accent bg-accent-subtle' : 'border-transparent hover:bg-bg-hover'}`}
    >
      <input
        id={inputId}
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        disabled={disabled}
        aria-label={`Select ${repo.full_name}`}
        className="flex-shrink-0 accent-[var(--accent)] cursor-pointer disabled:cursor-default"
      />
      <span className="flex-1 min-w-0 text-[12.5px] text-text truncate font-mono">
        {repo.full_name}
      </span>
      <span
        className="flex-shrink-0 text-[10.5px] text-muted"
        title={repo.last_contributed_at
          ? `Your last contribution: ${new Date(repo.last_contributed_at).toLocaleString()}`
          : undefined}
      >
        {when}
      </span>
    </label>
  )
}
