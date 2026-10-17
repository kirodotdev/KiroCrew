/** A session row's pull-request and issue chips: the provider mark, the one
 *  status glyph a change chip shows, and the strip with its expandable `+N`
 *  overflow, which fetches the links the slots payload leaves out. */
import { useState, useRef, useEffect, useMemo } from 'react'
import { X, ChevronUp, Link2, Circle, Check, Loader2, RotateCcw, GitMerge, CircleDot, TriangleAlert } from 'lucide-react'
import GithubLogo from '../../components/icons/GithubLogo'
import GitlabLogo from '../../components/icons/GitlabLogo'
import JiraLogo from '../../components/icons/JiraLogo'
import { sourceProviderMeta, sourceTabQualifier } from '../../utils/sourceProviderMeta'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { platformShortcut } from '../../utils/platform'
import { usePendingSourceUnlinks } from '../../components/SourceLinksSubmenu'
import { i18nT } from '../../i18n/t'
import type { SourceLinkState, SidebarSourceLink } from './types'
import { chipLabel } from './rows'

/** Lifecycle states after which a pull request can never merge, so its CI
 * rollup carries no actionable information and the lifecycle glyph is the only
 * meaningful signal. Named ONCE here because the vocabulary is shared by three
 * sibling conditionals; an inline literal per glyph is how `closed` came to be
 * covered by the badge but not by the CI gate.
 *
 * `closed` matters as much as `merged`: a closed pull request's check rollup can
 * stay pending FOREVER (GitHub parks fork-PR checks in PENDING /
 * ACTION_REQUIRED when the PR is closed before a maintainer approves the run),
 * so a chip gated only on `merged` spins its "checks running" spinner
 * indefinitely on a PR nobody is waiting for. Must stay in step with
 * `PullRequestPanel.tsx::SourceTabState`, which applies the same rule to the
 * source-strip tab — the chip and the tab describe one pull request and may not
 * disagree about its lifecycle. */
const TERMINAL_SOURCE_LINK_STATES: ReadonlySet<SourceLinkState> = new Set<SourceLinkState>([
  'merged',
  'closed',
])

/** Whether a chip should show its CI rollup or its merge state. Both are moot
 * once the pull request is terminal, so they share one gate. An ABSENT state
 * means the provider status has not been read yet (or the payload predates the
 * field), which is not terminal — such a chip keeps rendering CI exactly as it
 * always did. */
function showsChipCi(state: SourceLinkState | undefined): boolean {
  return state === undefined || !TERMINAL_SOURCE_LINK_STATES.has(state)
}

/** The single status glyph a change chip shows, or null for none.
 *
 * One function rather than sibling conditionals because the interesting part is
 * the PRECEDENCE, and precedence expressed as four independent `&&` guards is
 * how a chip comes to render two glyphs — or none — for a state nobody
 * enumerated.
 *
 * A conflict outranks a pending or passing rollup: green-check-on-unmergeable
 * is the reason this exists, since it reads as "ready" on a branch that cannot
 * land. It does NOT outrank a failed rollup — with both blockers live the worse
 * outcome is the one worth surfacing, and a red chip already says "do not
 * expect this to merge".
 *
 * `blocked` is deliberately not a conflict. On a repo with required reviews it
 * is the normal state of every open pull request, so treating it as a blocker
 * would decorate the whole session list and mean nothing.
 */
function chipStatusGlyph(
  link: SidebarSourceLink,
): 'failed' | 'conflict' | 'running' | 'passed' | null {
  if (!showsChipCi(link.state)) return null
  if (link.ci === 'failed') return 'failed'
  // GitHub can settle `mergeStateStatus: dirty` while `mergeable` is still
  // `unknown` (the two fields are recorded independently, each only once it is
  // real), and GitLab's `conflict` normalizes into both — so either field
  // alone is a real conflict answer.
  if (link.mergeable === 'conflicting' || link.mergeStateStatus === 'dirty') return 'conflict'
  if (link.ci === 'running') return 'running'
  if (link.ci === 'passed') return 'passed'
  return null
}

/** The provider's mark for a chip, or a neutral link glyph for a provider this
 * build does not recognize.
 *
 * One resolver rather than a ternary inlined at each chip: the change chip and
 * the issue chip render the identical mark, so the two copies were pure
 * duplication. Their LABEL branches were not duplication -- they encoded
 * genuinely different rules, since `!7` is a merge request and `#7` an issue --
 * which is exactly the knowledge that belongs with the parser rather than
 * spread across two render sites.
 *
 * The `default` is not dead code even though `provider` is typed as three
 * literals. That type describes what the serializer sends TODAY; the value
 * itself arrives over the wire from Python, where nothing enforces it. The
 * branch it replaced used GitLab as its implicit `else`, so an unrecognized
 * provider rendered GitLab's brand mark on someone else's review system --
 * a wrong attribution is worse than an anonymous one, and this is the one
 * failure mode a chip must not have.
 *
 * A REGISTERED provider sits between those two cases: it is not a built-in, so
 * it has no bundled mark here, but its descriptor may have supplied one. That
 * icon is consulted before the neutral glyph, which is what lets an edition
 * wear its own brand without any provider being able to wear another's. Mirrors
 * the fallback order in `MarkdownRenderer`'s forge chip and
 * `PullRequestPanel`'s tab strip.
 */
function SourceLinkIcon({ provider }: { provider: SidebarSourceLink['provider'] }) {
  switch (provider) {
    case 'github':
      return <GithubLogo size={10} className="shrink-0" />
    case 'gitlab':
      return <GitlabLogo size={10} className="shrink-0" />
    case 'jira':
      return <JiraLogo size={10} className="shrink-0" />
    default: {
      const Icon = sourceProviderMeta(provider).icon
      if (Icon) return <Icon size={10} className="shrink-0" />
      return <Link2 className="lucide-inline shrink-0" aria-hidden="true" />
    }
  }
}

/** A session row's pull-request / issue chip strip, including the expandable
 *  "+N" overflow chip.
 *
 *  A component rather than a block inside the row's render callback because the
 *  overflow chip is interactive and therefore needs per-row state. The slots
 *  payload deliberately serializes at most three links IN TOTAL per card,
 *  pull requests first and issues only in the slots left over (state_parts/source_links.py's
 *  `_SERIALIZED_SOURCE_LINKS_PER_SLOT`), so the strip stays short and a
 *  broadcast carrying dozens of rows stays small -- which means the links
 *  behind "+N" are not on the client at all and expanding has to fetch them. */
export function SessionSourceChips({ slotKey, links, total, connected, isActive, onOpenSource, onActivateSlot }: {
  slotKey: string
  /** The budgeted links from the slots payload — what the collapsed strip shows. */
  links: SidebarSourceLink[]
  /** `source_links_total`: how many the session actually has, budget aside. */
  total?: number
  connected: boolean
  isActive: boolean
  onOpenSource?: (slotKey: string, ref: { url: string; kind: 'change' | 'issue' }) => boolean
  /** Switch to this session — the chip reveals into ITS side panel, so the
   *  session has to be the active one first. */
  onActivateSlot: () => void
}) {
  const [wantsExpanded, setWantsExpanded] = useState(false)
  const pendingUnlinks = usePendingSourceUnlinks(slotKey)

  /** What the slots payload currently says this row's links are.
   *
   *  Part of the query key, so it is the LINK IDENTITY that decides whether a
   *  fetched list still applies — not the count. A session can drop one pull
   *  request as it gains another, leaving `total` unchanged, and a count-keyed
   *  cache would serve that superseded set forever. */
  const signature = `${total ?? ''}|${links.map(link => link.url).join(' ')}`
  // React Query rather than useState + fetch (website/AUTOSDE.yaml `use-react-query`):
  // the same session can be rendered by more than one column, and a shared cache
  // is what stops each copy issuing its own GET for the same slot. `enabled`
  // makes the read lazy — nothing is fetched until the row is actually expanded —
  // and `retry: false` keeps a failed expand immediate, because the user's next
  // click IS the retry.
  const { data: fetchedLinks, isFetching, isError, refetch } = useQuery<SidebarSourceLink[]>({
    queryKey: ['session-source-links', slotKey, signature],
    queryFn: async () => {
      const payload = await api.chatSlotSourceLinks(slotKey)
      // Shape-check rather than trust: a malformed 200 (a proxy, an older
      // gateway) would otherwise put `undefined` where the render filters an
      // array, and an exception in render unmounts the whole sidebar.
      if (!Array.isArray(payload?.links)) throw new Error('malformed source-links response')
      return payload.links
    },
    enabled: wantsExpanded,
    retry: false,
    // Owned by the query rather than inherited from the provider: collapsing and
    // re-expanding within the window must not re-issue the GET, and that
    // guarantee should not depend on a global default someone may retune.
    staleTime: 30_000,
  })

  // Expanded only while a list for THIS payload is in hand. Because the payload
  // is in the query key, a slots push that changes the links switches to a key
  // with no data yet: the row falls back to the live budgeted strip and re-offers
  // "+N" while the new list loads, instead of freezing on a snapshot that
  // silently omits the new link.
  const isExpanded = wantsExpanded && fetchedLinks !== undefined
  const failed = wantsExpanded && isError

  // Toggling REPLACES the button that was activated, so without this the
  // keyboard user is dropped to the top of the document mid-row. Armed only by
  // the two click handlers, so a re-render from a slots push never steals focus.
  const pendingFocus = useRef<'expand' | 'collapse' | null>(null)
  const expandRef = useRef<HTMLButtonElement>(null)
  const collapseRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const want = pendingFocus.current
    if (!want) return
    pendingFocus.current = null
    ;(want === 'collapse' ? collapseRef : expandRef).current?.focus()
  }, [isExpanded])

  const shownAll = isExpanded && fetchedLinks ? fetchedLinks : links
  // Pending mutations from any session menu hide chips; failure restores them.
  const shown = shownAll.filter(link => !link.identity || !pendingUnlinks.includes(link.identity))
  // Derived from the UNFILTERED set, so the optimistic-unlink filter only
  // removes visible chips and never inflates the overflow count: computing this
  // against `shown` would subtract an already-hidden chip from the stale server
  // `total` and render a phantom "+1 more" for the round-trip window until the
  // slots push refreshes `total`. Lands on 0 once expanded and self-corrects if
  // a payload ever reports a total below the links it carries.
  const hidden = typeof total === 'number' ? Math.max(0, total - shownAll.length) : 0
  const changeLinks = shown.filter(link => (link.kind ?? 'change') !== 'issue')
  const issueLinks = shown.filter(link => (link.kind ?? 'change') === 'issue')
  // Qualify chip labels when the visible strip spans more than one project, so
  // `group-a/svc#1` and `group-b/svc#1` are not both rendered as `#1`.
  // Returns null (-> bare chipLabel) when all shown links share one project.
  const qualify = useMemo(() => sourceTabQualifier(shown), [shown])

  const expand = () => {
    if (isFetching) return
    pendingFocus.current = 'collapse'
    // Already enabled means this is a retry after a failure (or a re-expand of a
    // key whose fetch never landed): flipping the flag again would not re-issue
    // the query, so ask for it explicitly.
    if (wantsExpanded) void refetch()
    else setWantsExpanded(true)
  }

  const overflowLabel = issueLinks.length
    ? i18nT('pages.chatSidebar.more_pull_request_or_issue_in_this_session', { count: hidden })
    : i18nT('pages.chatSidebar.more_pull_request_in_this_session', { count: hidden })
  /** Chip tooltip. A plain click now reveals in-panel, so a bare "Open <url>"
   *  would promise the browser and mislead; naming the modifier is also the only
   *  way that escape hatch is discoverable rather than found by accident. */
  const chipTitle = (link: SidebarSourceLink) => i18nT('pages.chatSidebar.open_source_link_in_side_panel', {
    url: link.url,
    modifier: platformShortcut('Cmd+click'),
  })
  /** Chip click: switch to the session the chip belongs to and reveal its pull
   *  request / issue in that session's side panel, rather than sending the user
   *  out to the provider's website.
   *
   *  The chip stays a real anchor with a real href, so four cases deliberately
   *  fall through to plain link navigation instead:
   *    - `onOpenSource` unset — the surface has no side panel to reveal into
   *      (the `/embed/sessions` list).
   *    - a modifier click — the user asked for a new tab/window explicitly, and
   *      "Copy link address" still yields the PR url.
   *    - offline — the panel loads a PR through the LOCAL provider CLI, so with
   *      the gateway down the provider's own page is the only thing that can
   *      answer at all.
   *    - `onOpenSource` returning false — the panel could not resolve this url,
   *      so the provider's page is better than a dead click.
   *  Middle-click never reaches a click handler (it fires auxclick), so it opens
   *  a background tab natively without a case here.
   *
   *  `preventDefault` comes LAST on purpose: the default action runs only after
   *  every handler returns, so suppressing navigation after the reveal is still
   *  effective — and it means the reveal decides, rather than being assumed to
   *  succeed. */
  const revealInPanel = (link: SidebarSourceLink) => (e: React.MouseEvent<HTMLAnchorElement>) => {
    // The row is a click-to-switch button; never let a chip click reach it,
    // whichever branch we take below.
    e.stopPropagation()
    // A pointer click focuses the anchor. The row's action strip reveals on
    // `:focus-visible`, which a mouse-focused anchor does not normally match;
    // drop focus on pointer clicks anyway so a stale focus can never pin it.
    // `detail` counts clicks and keyboard activation reports 0, so Enter on a
    // focused chip leaves focus where a keyboard user expects it.
    if (e.detail > 0) e.currentTarget.blur()
    if (!onOpenSource || !connected || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    if (!isActive) onActivateSlot()
    if (!onOpenSource(slotKey, { url: link.url, kind: link.kind ?? 'change' })) return
    e.preventDefault()
  }

  return (
    <div className="flex flex-wrap gap-1.5 mt-1">
      {changeLinks.map(link => (
        // `link.url` is always an `https://` URL on an allowlisted host
        // (state.py scans for the literal "https://" then validates via
        // parse_source_url), so no scheme sanitising is needed for the href.
        //
        // The row is a dnd-kit draggable as well as a button, so the anchor also
        // disables its own native HTML5 drag — that would otherwise put the URL
        // on the dataTransfer instead of the slot key in the board/flat scopes
        // that use native drag.
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
          draggable={false}
          onClick={revealInPanel(link)}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted no-underline border border-border bg-bg-elevated/60 hover:text-text hover:border-accent"
          title={chipTitle(link)}>
          <SourceLinkIcon provider={link.provider} />
          {qualify(link) != null ? `${qualify(link)} ${chipLabel(link)}` : chipLabel(link)}
          {link.state === 'merged' && (
            <span className="inline-flex shrink-0 text-aim" aria-label={i18nT('pages.chatSidebar.merged')} title={i18nT('pages.chatSidebar.merged')}>
              <GitMerge className="lucide-inline" aria-hidden="true" />
            </span>
          )}
          {/* Real text needs no aria-label/title of its own: the anchor's
              accessible name already includes it, and a child `title` would
              shadow the anchor's tooltip — the URL and the modifier escape
              hatch — for the region the word covers. The merged sibling
              carries both only because its span is icon-only. */}
          {link.state === 'closed' && (
            <span className="shrink-0 whitespace-nowrap text-danger">
              {i18nT('pages.chatSidebar.closed')}
            </span>
          )}
          {/* One status glyph, chosen by `chipStatusGlyph` — CI is moot
              once the PR is terminal (merged or closed), where the
              lifecycle glyph is the signal, and a merge conflict
              outranks a pending or passing rollup. */}
          {/* Pending CI is a STATIC amber dot (the provider's own pending
              convention), never a spinner: an animated glyph on a session
              card reads as "the agent is working on this session", which is
              a stronger claim than "this PR's checks haven't finished".
              Motion on the card stays reserved for session activity. */}
          {(() => {
            switch (chipStatusGlyph(link)) {
              case 'running':
                return <Circle className="lucide-inline shrink-0 text-warn scale-75" fill="currentColor" strokeWidth={0} aria-label={i18nT('pages.chatSidebar.checks_running')} />
              case 'passed':
                return <Check className="lucide-inline shrink-0 text-ok" aria-label={i18nT('pages.chatSidebar.checks_passed')} />
              case 'failed':
                return <X className="lucide-inline shrink-0 text-danger" aria-label={i18nT('pages.chatSidebar.checks_failed')} />
              case 'conflict':
                // The panel's own conflict-banner key, reused rather than
                // duplicated: the chip and the banner describe one pull
                // request, so they must not word it differently in any
                // locale.
                return <TriangleAlert className="lucide-inline shrink-0 text-danger" aria-label={i18nT('components.pullRequestPanel.merge_conflicts')} />
              default:
                return null
            }
          })()}
        </a>
      ))}
      {issueLinks.map(link => (
        // Issue chip: the same anchor discipline (reveal in panel, no native
        // drag) but deliberately NO ci / state / merge decoration — the
        // chip-status cache is pull-request-only in this phase, so an issue chip
        // has nothing truthful to colour and a borrowed glyph would assert state
        // we never fetched. How the number is written is the serializer's call
        // (`source_ref_label`), so nothing here branches on provider except the
        // issue dot, which Jira does not get: its label is already a whole
        // identifier (PROJ-123) rather than a bare number needing a marker.
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
          data-testid={`session-issue-chip-${link.number}`}
          draggable={false}
          onClick={revealInPanel(link)}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted no-underline border border-border bg-bg-elevated/60 hover:text-text hover:border-accent"
          title={chipTitle(link)}>
          <SourceLinkIcon provider={link.provider} />
          {link.provider !== 'jira' && <CircleDot className="lucide-inline shrink-0" aria-hidden="true" />}
          {qualify(link) != null ? `${qualify(link)} ${chipLabel(link)}` : chipLabel(link)}
        </a>
      ))}
      {hidden > 0 && (
        // Gated on `hidden`, NOT on the expand intent: a row whose payload moved
        // under an open expansion renders the live budgeted strip again, and
        // must re-offer the overflow rather than hide it behind a stale state.
        //
        // `onMouseDown` stops the row's drag from claiming the press, matching
        // the row's other in-place controls; without it a click on the chip can
        // be swallowed as a drag activation. Deliberately NOT `disabled` while
        // loading — disabling the focused button blurs it to <body>, and the
        // `if (loading) return` guard in `expand` already prevents a double
        // fetch.
        <button type="button"
          ref={expandRef}
          data-testid="session-source-overflow"
          draggable={false}
          aria-expanded={false}
          onMouseDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); expand() }}
          className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60 cursor-pointer hover:text-text hover:border-accent"
          title={failed ? i18nT('pages.chatSidebar.source_links_expand_failed') : overflowLabel}
          // An aria-label OUTRANKS the title in the accessible-name computation,
          // so the failure has to be named here too or a screen reader still
          // announces "2 more pull requests…" on a button that just failed.
          aria-label={failed ? i18nT('pages.chatSidebar.source_links_expand_failed') : overflowLabel}>
          {/* This spinner is exempt from the "no motion on a session card" rule
              that governs the CI glyph above: it is transient feedback for the
              user's OWN click on this button, not an ambient status claim about
              the session. It exists only while their expand is in flight. */}
          {isFetching
            ? <Loader2 className="lucide-inline shrink-0 animate-spin" aria-hidden="true" />
            : failed && <RotateCcw className="lucide-inline shrink-0 text-warn" aria-hidden="true" />}
          +{hidden}
        </button>
      )}
      {isExpanded && (
        <button type="button"
          ref={collapseRef}
          data-testid="session-source-collapse"
          draggable={false}
          aria-expanded={true}
          onMouseDown={e => e.stopPropagation()}
          onClick={e => { e.stopPropagation(); pendingFocus.current = 'expand'; setWantsExpanded(false) }}
          className="inline-flex items-center px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium text-muted border border-border bg-bg-elevated/60 cursor-pointer hover:text-text hover:border-accent"
          title={i18nT('pages.chatSidebar.collapse_source_links')}
          aria-label={i18nT('pages.chatSidebar.collapse_source_links')}>
          <ChevronUp className="lucide-inline shrink-0" aria-hidden="true" />
        </button>
      )}
    </div>
  )
}
