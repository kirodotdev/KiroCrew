import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Lock, RotateCw } from 'lucide-react'
import { api, type DashboardManifest } from '../../api/client'
import { apiErrorCode, isNotFoundError } from '../../api/apiError'
import { retryPolicy } from '../../api/queryClient'
import { Badge, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useTheme } from '../../hooks/useTheme'
import { useSandboxDoc } from '../../hooks/useSandboxDoc'
import { useFrameOpenLink } from '../../hooks/useFrameOpenLink'
import { useFrameOpenSession } from '../../hooks/useFrameOpenSession'
import { buildSrcdoc, readThemeVars } from '../../lib/widgetSrcdoc'
import { startDashboardLoadTiming, type DashboardLoadTiming } from '../../lib/dashboardTiming'
import { i18nT } from '../../i18n/t'
import { useLanguage } from '../../i18n/LanguageProvider'
import { MEMBER_DASHBOARD_QUERY_PREFIX, SESSION_DASHBOARD_QUERY_PREFIX, subscribeDashboardFields } from '../../hooks/useWebSocket'
import CrewDashboardEmpty from './CrewDashboardEmpty'

/**
 * The sandbox grants for a crewmate's dynamic dashboard, and the ONE line of
 * this file that is a security boundary rather than a layout choice.
 *
 * `allow-scripts` alone, which contract v3 requires (`html` MAY run JS: a chart
 * is script or it is nothing) and nothing more. Every other grant is withheld:
 *
 * - No `allow-same-origin`, so the document lands on a null (opaque) origin and
 *   cannot read the dashboard's cookies or localStorage, or touch the parent DOM.
 * - No `allow-popups`. A template is loaded at run time, including one a user
 *   imported from a shared file, and a window it could open is a capability
 *   nothing about a status page needs. The one way out is the host's own
 *   `kirocrew-dashboard:open` bridge (`useFrameOpenLink`): a GitHub pull-request
 *   URL, from this frame, right after a user gesture, opened by the host.
 * - No `allow-top-navigation`, `allow-forms`, or `allow-modals`.
 *
 * Egress is closed by the document CSP (`connect-src 'none'`), which the gateway
 * injects inside the composed document AND `buildSrcdoc` injects outside it.
 * Policies combine by intersection, so the stricter wins on every directive.
 *
 * Same value as the published-panel frame deliberately: these are two documents
 * on one surface, and a reader comparing them should not have to wonder whether
 * one of them is more trusted.
 */
export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'

/**
 * The grant set for a page the server says may NOT run script: empty, which is
 * every sandbox restriction applied.
 *
 * An agent-written page is licensed to run by the RFC amendment's fourth reason
 * -- the owner agent already reaches the network, so a page navigating its own
 * frame opens no path that agent lacked. Under an admin ceiling that DENIES the
 * agent egress that reason is false, and nothing else here stops navigation: a
 * closed `connect-src` does not. So the server answers `scripts: false` and the
 * page is mounted static.
 *
 * It still SHOWS its values, because the GATEWAY fills them. A
 * `data-dashboard-field` binding is normally filled by the composed document's
 * own bootstrap, which does not run here -- so when the server withholds script
 * it writes each bound cell's text itself, HTML-escaped
 * (`dashboard_frame.fill_bindings_as_text`). Without that the page drew every
 * bound cell empty with its stale band still hidden, which reads as a current
 * page showing nothing.
 */
export const CREW_DASHBOARD_SANDBOX_STATIC = ''

/** The grant set this page gets: the server decides, not the page. */
export function crewDashboardSandbox(scripts: boolean): string {
  return scripts ? CREW_DASHBOARD_SANDBOX : CREW_DASHBOARD_SANDBOX_STATIC
}

/** The gateway's refill message type. Mirrors `dashboard_frame.DATA_MESSAGE_TYPE`. */
const DATA_MESSAGE_TYPE = 'kirocrew-dashboard:data'

/** The page's readiness beacon. Mirrors `dashboard_frame.READY_MESSAGE_TYPE`. */
const READY_MESSAGE_TYPE = 'kirocrew-dashboard:ready'

/** A page's request to put a reply in the chat box: `{ type, text }`.
 *
 * The page is crewmate-written HTML, so it never SENDS anything: the host only
 * fills the composer, and the person presses send. That is the whole safety
 * line -- a page that could send would be a page that could speak as the user. */
const ACT_MESSAGE_TYPE = 'kirocrew-dashboard:act'
/** What the page posts when it cannot place a patch in order. Mirrors
 *  `dashboard_frame.REFETCH_MESSAGE_TYPE`. */
const REFETCH_MESSAGE_TYPE = 'kirocrew-dashboard:refetch'
/** Longest reply a page may put in the composer. Longer is refused, not cut. */
const ACT_MAX_CHARS = 600

/** The text a page's act message may put in the composer, or null to refuse it. */
export function actText(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; text?: unknown }
  if (msg.type !== ACT_MESSAGE_TYPE || typeof msg.text !== 'string') return null
  const text = msg.text.trim()
  if (!text || text.length > ACT_MAX_CHARS) return null
  if (/[\u0000-\u0009\u000b-\u001f\u007f]/.test(text)) return null
  return text
}

/**
 * How long a newly minted document has to report that it loaded.
 *
 * This is the whole mechanism behind "keep the last good page". A mint that never
 * lands is already visible as a failure; a document whose first script THROWS is
 * not -- it renders blank, with no error and nothing to fall back to, which is
 * indistinguishable from a healthy page that happens to have nothing to show. The
 * beacon is what tells the two apart, and this is how long the frame waits for it
 * before deciding the new page did not come up.
 *
 * Generous because the cost of waiting is a few seconds of the PREVIOUS page
 * staying on screen, while the cost of being too eager is reverting a page that
 * was merely slow to parse a chart library.
 */
const READY_TIMEOUT_MS = 6000

/**
 * How long an open Dashboard tab may go without re-reading, when no WebSocket
 * frame has told it to.
 *
 * The tab's liveness is the two frames `handleDashboardMoved` listens for -- a
 * crewmate's own write, and a fold advancing. This number is only the floor under
 * them, for a frame that never arrived because the socket dropped or the tab was
 * closed when it was sent.
 *
 * Exported so a test can assert it is FINITE: a missing or infinite interval looks
 * identical to a working one in every case where a frame does arrive, which is
 * every case anybody writes.
 */
export const DASHBOARD_FALLBACK_REFETCH_MS = 60_000

/** The preview read's own "nothing staged" answer: 404 `no_preview`. Only that one.
 *  The same route answers 404 `member_not_found` for a crewmate that is gone, and
 *  that is a load failure the reader must be told about, not a preview that ended. */
/** The read refuses a caller who is not the crewmate's owner. Retry reads the
 *  same refusal, so the tab says whose page it is instead of offering Retry. */
function isOwnerOnly(e: unknown): boolean {
  return apiErrorCode(e) === 'owner_only'
}

function isNothingStaged(e: unknown): boolean {
  return isNotFoundError(e) && apiErrorCode(e) === 'no_preview'
}

/** The id of the composed page's data island. Mirrors `dashboard_frame._DATA_ELEMENT_ID`. */
const DATA_ELEMENT_ID = 'kirocrew-dashboard-data'

/**
 * The read a composed page was filled from, taken out of its data island.
 *
 * The gateway bakes the values into `rendered_html` and sends them nowhere else, so
 * this is where a refill's values come from. Parsed by `DOMParser`, which builds an
 * inert document and runs none of its scripts, and read back as the island's TEXT:
 * the island is JSON escaped for HTML with JSON's own `\u` escapes, so `JSON.parse`
 * returns exactly what the gateway composed. Null on anything else, which makes the
 * caller load a new document instead.
 */
export function islandRead(html: string): Record<string, unknown> | null {
  try {
    const doc = new DOMParser().parseFromString(html, 'text/html')
    const raw = doc.getElementById(DATA_ELEMENT_ID)?.textContent
    if (!raw) return null
    const read: unknown = JSON.parse(raw)
    return read && typeof read === 'object' && !Array.isArray(read) ? (read as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** Post the read baked into `html` to `win`, as the page's refill listener takes it.
 *  False when the html has no readable island, so the caller re-mints instead.
 *  `win` is always the dashboard frame's own `contentWindow`; `'*'` because that
 *  frame's origin is opaque and can be named no other way. */
function postRefill(win: Window, html: string): boolean {
  const read = islandRead(html)
  if (!read) return false
  // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
  win.postMessage({ type: DATA_MESSAGE_TYPE, read }, '*')
  return true
}

/** Which page a document is: a change to any part of this is a new document. */
function pageKey(page: Loaded): string {
  return `${page.instanceVersion}:${page.templateId}:${page.templateVersion}`
}

interface Loaded {
  html: string
  manifest: DashboardManifest
  instanceVersion: number
  templateId: string
  templateVersion: number
  /** Whether this page may run script. Carried WITH the page rather than read
   *  from the latest response, so the grants always match the markup mounted. */
  scripts: boolean
  /** The SURFACE this page belongs to: the package slug it was read for, or `''` for
   *  the crewmate's template instance.
   *
   *  Carried with the page because the two are different surfaces rather than two
   *  versions of one, and every other thing that could tell them apart is reusable.
   *  An instance version restarts per record and the html can coincide, so a read
   *  that moves this tab between them would otherwise reconcile against the held
   *  document and keep it. */
  dashboard: string
  /** Which staged page this is, on a preview read; 0 on the live page. */
  stagedMs: number
}

/**
 * The crewmate's dynamic dashboard, in the Dashboard tab beside the chat.
 *
 * It renders the INSTANCE -- the crewmate's own copy of a template -- which the
 * registry serves from `GET /api/members/{slug}/dashboard`. The gateway composes
 * the document: it fills every `data-dashboard-field` element from the folded
 * values, sets `window.kirocrew`, marks the agentic cells and raises its own stale
 * band. This component's jobs are the three the document cannot do for itself:
 * mint it into a sandbox, keep the last page that successfully loaded, and offer a
 * way out when nothing loads at all.
 *
 * The LAST GOOD PAGE is kept on two distinct failures, which is why the state is a
 * held value rather than a flag:
 *
 * 1. the mint failed -- `useSandboxDoc` already keeps its previous URL on a later
 *    failure by design, so the previous document stays on screen;
 * 2. the mint SUCCEEDED and the document never beaconed. Nothing below the frame
 *    can see that, so the held `Loaded` is re-mounted and the new html is dropped
 *    until the next read brings a different one.
 *
 * With `preview` it renders the STAGED page instead -- the one `dashboard_preview`
 * offered, read from the same route with `preview=1`. The host draws the one
 * preview label above it (`DashboardPreviewLabel`) and drops it on `onPreviewGone`.
 */
/** Whose dashboard this frame reads: a crewmate's, or one ROOT session's own page. */
export type DashboardTarget =
  | { kind: 'member'; slug: string; member: string }
  | { kind: 'session'; slot: string }

/** The query key a target's read is cached under. The first segment is the prefix
 *  `handleDashboardMoved` invalidates by, so a frame naming only a slug or a slot
 *  reaches the read. */
export function dashboardQueryKey(target: DashboardTarget): readonly string[] {
  return target.kind === 'member'
    ? [MEMBER_DASHBOARD_QUERY_PREFIX, target.slug, target.member]
    : [SESSION_DASHBOARD_QUERY_PREFIX, target.slot]
}

export default function CrewDynamicDashboard({ target, displayName, avatar, onAct, preview = false, onPreviewGone, onPreviewLoaded, onOpenProfileTab, dashboardSlug = '' }: {
  target: DashboardTarget
  displayName: string
  /** The crew record's `avatar` field, verbatim, for the suggestions page's face. */
  avatar?: unknown
  /** Put a reply the page offered into the chat box. Absent: the page's options do nothing. */
  onAct?: (text: string) => void
  /** Render the STAGED page instead of the adopted one. Its own cache entry, so the
   *  live tab never shows a page nobody applied; still under the `member-dashboard`
   *  prefix, so the frame that says the page changed refetches it too. */
  preview?: boolean
  /** Told whether the staged page is gone (applied or expired). A host that listens
   *  hands over to the live page itself, so this frame draws nothing for that state. */
  onPreviewGone?: (gone: boolean) => void
  /** Told the `staged_ms` of the staged page the frame has drawn (0 until it has),
   *  so the host's Keep and Discard name exactly that page. */
  onPreviewLoaded?: (stagedMs: number) => void
  /** Called when the person clicks the Locked badge to jump to the Profile tab. */
  onOpenProfileTab?: () => void
  /** The `kind="dashboard"` package to show instead of the crewmate's template page.
   *  Empty shows the template instance, which is every crewmate today: no caller
   *  resolves a package slug yet (that lookup is #18707). Carried here so the frames
   *  this tab accepts and the refetch it asks for both name one package. */
  dashboardSlug?: string
}) {
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  /** A gap recovery that did not arrive. The page keeps its values; this says they
   *  are older than the record, which nothing else on screen can say: the page's
   *  own stale band is driven by a read that never came. */
  const [refetchError, setRefetchError] = useState<string | null>(null)
  /** A whole-read request is in flight. The notice's Retry is disabled on it, which is
   *  what acknowledges the press: the notice itself stays up until the read arrives, so
   *  without this nothing on screen changes and the button reads as dead. */
  const [refetchPending, setRefetchPending] = useState(false)
  const onActRef = useRef(onAct)
  onActRef.current = onAct
  // A pull request the page links to: this frame only, GitHub PR URLs only.
  useFrameOpenLink([frameRef])
  // The worker session behind an org-chart card: this frame only, resolved on the gateway from the
  // item id. The key stays in the host's JavaScript; the page is told only whether it worked.
  useFrameOpenSession(
    frameRef,
    target.kind === 'member'
      ? (target.slug && target.member ? { kind: 'member', slug: target.slug, member: target.member } : null)
      : (target.slot ? { kind: 'session', slot: target.slot } : null),
  )
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])

  // The language the page renders its own words in. In the key, so switching the
  // UI language re-reads the page in the new one.
  const { resolved: locale } = useLanguage()

  const staged = preview
  // STEP TIMERS (#20952). One timer per fresh step-1 fetch, held in a ref so the
  // step-2 (sandbox-doc POST) and step-3 (frame load) effects below can time against
  // the same load start. A timing probe only -- see lib/dashboardTiming.
  const timingRef = useRef<DashboardLoadTiming | null>(null)
  const timingLabel =
    target.kind === 'member' ? `member:${target.slug}` : `session:${target.slot}`
  const { data, isLoading, isError, isFetching, error, refetch } = useQuery({
    queryKey: [...dashboardQueryKey(target), locale, staged ? 'preview' : 'live'],
    queryFn: async () => {
      // A live load only; a staged/preview read is a person action, not the
      // every-open path #20952 is about, so it is not timed.
      const timer = staged ? null : startDashboardLoadTiming(timingLabel)
      // Always adopt this read's timer, even when it is null: a staged/preview read
      // then clears the ref so an EARLIER load's timer can never catch step 2 and
      // step 3 of this read (a theme change re-mints the url the same way).
      timingRef.current = timer
      const res = await (target.kind === 'member'
        ? (staged ? api.memberDashboard(target.slug, target.member, locale, true) : api.memberDashboard(target.slug, target.member, locale))
        : api.sessionDashboard(target.slot, locale, staged))
      if (timer) {
        timer.step('step1-get')
      }
      return res
    },
    enabled: target.kind === 'member' ? Boolean(target.slug) && Boolean(target.member) : Boolean(target.slot),
    // Nothing staged is a 404 that a retry cannot change: the page was kept,
    // discarded or expired. Every other failure keeps the client's own ladder.
    ...(staged
      ? { retry: (failureCount: number, error: unknown) => !isNothingStaged(error) && retryPolicy(failureCount, error) }
      : {}),
    // THE FALLBACK, not the mechanism. Liveness comes from the two WS frames
    // `handleDashboardMoved` listens for; this is what covers the gap when one is
    // missed -- a dropped socket, a fold that advanced while the tab was closed, a
    // write broadcast the tab reconnected just after.
    //
    // Finite and long on purpose. Freshness-by-push means the page would otherwise
    // sit on whatever it read when the tab opened until the user pressed something,
    // and a project report that silently stops being current is worse than one that
    // costs a read a minute. Paused while the tab is in the background, because a
    // dashboard nobody is looking at does not need to be current.
    refetchInterval: DASHBOARD_FALLBACK_REFETCH_MS,
    refetchIntervalInBackground: false,
  })
  // The crewmate behind a member target; empty for a root-session target, which has
  // neither a slug nor a package and so arms neither the refetch nor the push below.
  const memberSlug = target.kind === 'member' ? target.slug : ''
  const memberName = target.kind === 'member' ? target.member : ''
  /** The package this tab is showing. The ROUTE resolves the crewmate's binding, so
   *  the server's answer wins over the prop: a mount does not know a package slug and
   *  should not have to. The prop stays for a host that opens one explicitly. */
  const livePackage = data?.dashboard || dashboardSlug
  /** The same value, readable from inside a pending promise. A refetch started under
   *  one package must not deliver into the document that replaced it, and the closure
   *  the request was made in holds the old value by definition. */
  const livePackageRef = useRef(livePackage)
  livePackageRef.current = livePackage

  /** THE WHOLE READ, asked for once and repeatable.
   *
   *  One function for both callers -- the page's own gap message, and the Retry beside
   *  the notice that gap raised. The notice exists because THIS request did not arrive,
   *  so its button has to repeat THIS request; the dashboard query's `refetch` re-reads
   *  the record, which is a different question and leaves the page's numbers exactly as
   *  behind as they were.
   *
   *  WHICH DOCUMENT ASKED. A refetch is a round trip, and the frame under it can be
   *  replaced while it is open -- the ordinary dashboard read remounts the document when
   *  the package or the page changes. Posting a late answer into the document that
   *  replaced the asker hands it values computed for a page it is not, and the reply
   *  looks current to its bootstrap: a different generation is all it sees, which it
   *  accepts. So the asking window and the package it asked for are captured here and
   *  compared on delivery; a reply that outlived either is dropped, and the page it was
   *  for is gone anyway. */
  const askWholeRead = useCallback(() => {
    // A root session's page has no crewmate slug and no refetch route; its liveness
    // is the fallback read alone, so there is nothing to ask for here.
    if (!memberSlug) return
    const askedFor = livePackageRef.current
    // AND THERE IS NOTHING TO ASK FOR WITHOUT A PACKAGE. The route refetches one
    // package's armed page and answers 400 `missing_dashboard` to an empty name, so
    // asking anyway turns a page this tab is not even showing into an error band the
    // Retry can never clear.
    if (!askedFor) return
    const askedBy = frameRef.current?.contentWindow
    setRefetchPending(true)
    api.memberDashboardRefetch(memberSlug, memberName, askedFor)
      .then((answer) => {
        const read = answer?.read
        const current = frameRef.current?.contentWindow
        if (!current || current !== askedBy || askedFor !== livePackageRef.current) return
        if (read && typeof read === 'object') {
          // '*' is REQUIRED, not a laxity: this frame is sandboxed with
          // `allow-scripts` and WITHOUT `allow-same-origin`, so its origin is
          // opaque and no non-wildcard targetOrigin can ever match it. The
          // message is posted to THIS frame's own contentWindow and carries only
          // the fields the page's own model declares, already redacted by the
          // controller -- so the wildcard reaches exactly one window.
          // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
          current.postMessage({ type: DATA_MESSAGE_TYPE, read }, '*')
          // AFTER the values are in the page, never before. Clearing it at click time
          // states the numbers are current again while the read that would make that
          // true is still in flight -- and if it fails, nothing on screen ever says so.
          setRefetchError(null)
        }
      })
      .catch((err) => {
        // REPORTED, not swallowed. The page keeps the values it has, which is right,
        // but those values are now known to be older than the record and the reader
        // cannot tell: the page's own stale band is driven by the host's read, and a
        // refetch that never arrived leaves it saying nothing. The notice sits beside
        // the page rather than over it, and a retry that fails again leaves it there.
        setRefetchError(apiErrorCode(err) || 'refetch_failed')
      })
      .finally(() => setRefetchPending(false))
  }, [memberSlug, memberName])

  // The page's options, and its request for a whole read. Only a message from THIS
  // frame's window counts: any other window posting the same shape is ignored.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const win = frameRef.current?.contentWindow
      if (!win || event.source !== win) return
      const text = actText(event.data)
      if (text && onActRef.current) {
        onActRef.current(text)
        return
      }
      // THE GAP. The page applies patches in sequence and asks for the whole read
      // when one skips a version; answering with another patch would reach the same
      // half-updated page one round trip later.
      const kind = (event.data ?? {}) as { type?: unknown }
      if (kind.type !== REFETCH_MESSAGE_TYPE) return
      askWholeRead()
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [askWholeRead])

  // THE PUSH. The gateway sends the fields that MOVED; this posts them into the
  // frame, whose bootstrap merges the patch, refills the bindings and calls back
  // only the subscribers for the fields the patch carried.
  //
  // `postMessage` and not a re-render: the page owns its own DOM and the parent
  // cannot reach into it, which is the whole point of the sandbox. A refetch of the
  // route would work and is what the other two liveness frames do -- and it would
  // throw away the page's own state (a chart's animation, a tree's scroll position,
  // a 3D scene) on every value change, which is what the push exists to avoid.
  //
  // Only a member target has a crewmate slug the push bus is keyed by; a root
  // session's page is not pushed and the subscription is not armed for it.
  useEffect(() => {
    if (!memberSlug) return
    return subscribeDashboardFields(memberSlug, (read, fromPackage) => {
      // NOT MINE: one crewmate can be shown through two packages at once, and the
      // socket delivers every frame to every tab. Applying another package's patch
      // would replace this page's values with values computed for a different layout.
      //
      // Read through the REF, not the value. This subscription is keyed on the slug
      // alone on purpose: taking `livePackage` as a dependency would tear the socket
      // subscription down and build it again every time the shown package changes,
      // and a push that arrives in the gap is simply lost. The ref is the same value
      // readable from inside a long-lived callback, which is what it exists for.
      // NO PACKAGE OF MINE IS ALSO AN ANSWER, so the empty case is a drop and not a
      // pass. This tab shows a template page -- because none is bound, or because the
      // person locked the layout and the gateway stopped selecting one -- while the
      // (crewmate, package) page can still be armed from before. Letting its pushes
      // through gave the template's bootstrap a generation it cannot place, so it asked
      // for a whole read with no package to name, the gateway answered 400
      // `missing_dashboard`, and the stale band came back on every push.
      const mine = livePackageRef.current
      if (fromPackage !== mine) return
      // Same reason as the refetch answer above: an opaque origin cannot be named,
      // and the post reaches this one frame's contentWindow with model-declared,
      // already-redacted fields.
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      frameRef.current?.contentWindow?.postMessage({ type: DATA_MESSAGE_TYPE, read }, '*')
    })
  }, [memberSlug])
  // Only a SETTLED answer says the page is gone. A frame remounted on a newer
  // preview link can start from a cached `no_preview` while its own read is in
  // flight, and handing over on that would skip the page staged since.
  const previewGone = staged && isError && !isFetching && apiErrorCode(error) === 'no_preview'
  // A crewmate with no ADOPTED page, and none named for it, gets the suggestions
  // page. The GATEWAY decides (`suggestions`), by the same
  // function that tells the crewmate what its tab shows, so the two cannot drift.
  // Only the live member read: a staged preview is a page the crewmate offered, and
  // a session's page has no crewmate to ask.
  const liveMember = target.kind === 'member' && !staged
  const suggestions = liveMember && !isError && data?.suggestions === true
  useEffect(() => { onPreviewGone?.(previewGone) }, [onPreviewGone, previewGone])

  // The person's layout lock, read off the same body the page comes from. This tab
  // only SHOWS it: the switch that sets it is on the crewmate's Profile card.
  const locked = target.kind === 'member' && !!data && 'structure_locked' in data && data.structure_locked === true

  /**
   * The page currently believed to work, and the candidate waiting to prove it.
   *
   * Two pieces of state rather than one, because "what is on screen" and "what the
   * server last sent" genuinely differ while a candidate is on probation. Collapsing
   * them would mean either showing an unproven page (the bug) or never showing a new
   * one (the feature gone).
   */
  const [good, setGood] = useState<Loaded | null>(null)
  const [candidate, setCandidate] = useState<Loaded | null>(null)
  const [reverted, setReverted] = useState(false)
  /**
   * The SERVER's latest word on whether this page may run script, held apart from the
   * document that happens to be promoted.
   *
   * Withdrawing script is a governance act, so it cannot wait on anything the PAGE
   * does -- and both promotion paths are things the page does or does not do. A new
   * version waits on the readiness beacon, which a page mounted without script can
   * never send; a same-version read is swapped in place only when the html differs,
   * so a read whose only change is this flag is dropped. Either way the frame on
   * screen would keep `allow-scripts` the server had already taken back.
   *
   * Absent means allowed, the same reading `Loaded.scripts` uses: only a server that
   * has decided the ceiling denies the owner agent's egress sends `false`.
   */
  const [mayRunScript, setMayRunScript] = useState(true)
  /**
   * The SURFACE the page currently held belongs to, or `null` when none is.
   *
   * A ref and not a dependency: the read effect below is what promotes a page, so
   * depending on the promoted page would make it re-run on its own output. Written at
   * exactly the two moments the held page changes surface -- when a read establishes
   * one, and when any branch clears it -- so it never lags behind what is mounted.
   */
  const heldSurfaceRef = useRef<string | null>(null)

  useEffect(() => {
    if (!data) return
    // THE SURFACE THIS READ IS FOR. The route resolves the crewmate's binding, so its
    // answer wins over the prop; `''` is the crewmate's template instance.
    const identity = livePackage ?? ''
    // THE SURFACE IS ANSWERED FIRST, AHEAD OF EVERY STATE BRANCH BELOW.
    //
    // This is the root of the rule and the only place it is asked. A page belonging to
    // another surface is not an older version of this read's page, so NO branch below
    // may return while one is still mounted -- and each of them returns for its own
    // good reason, which is exactly how one of them ends up keeping a page the tab has
    // moved off. `state: 'error'` was that branch: a read that moves this tab from a
    // package to the template instance AND reports the stored copy unparseable kept the
    // package document, `allow-scripts` and all, because the error branch answered
    // first and the surface was never compared.
    //
    // So the comparison happens before any of them, and the clear is the same one the
    // no-page guard does: the page, its candidate and the script decision. Asking it
    // here rather than inside the two promotion setters is what makes it unskippable --
    // a setter only runs on a read that got as far as promoting something.
    if (heldSurfaceRef.current !== null && heldSurfaceRef.current !== identity) {
      setCandidate(null)
      setGood(null)
      setMayRunScript(false)
      heldSurfaceRef.current = null
    }
    // STATE `error` NEVER becomes a page, AND is the one read that leaves a page on THIS
    // surface alone. The read answers 200 for it and `wire()` always carries `html`, but
    // the gateway deliberately does not compose that state -- `_render` is skipped, so
    // there is no data island, no bootstrap and no ready beacon. Promoting it would
    // draw the template's own placeholder markup as a healthy dashboard: every cell
    // empty, nothing marked missing, no band, and no sign to a reader that what they
    // are looking at is not their data.
    //
    // It is not the same answer as the guard below, which is why it is asked separately.
    // `error` is the route saying this read SUCCEEDED and the crewmate's NEWER stored
    // copy does not parse; the page already on screen is still the record's last good
    // page for this surface, and it stays, under a band that says so. The guard below is
    // for a read that gives this tab no page to be on at all.
    if (data.state === 'error') return
    // FAIL CLOSED ON EVERY READ THAT ESTABLISHES NO PAGE, in one place, with no case
    // exempted for being merely stale.
    //
    // The suggestions page stands in for an unadopted crewmate; a page that read
    // carried never becomes the held page, and nothing held from before stays
    // mounted behind it.
    if (suggestions) {
      setCandidate(null)
      setGood(null)
      setMayRunScript(false)
      heldSurfaceRef.current = null
      return
    }
    // ONLY the composed page is ever mounted. `data.html` is the crewmate's STORED
    // copy, out of a writable `instance.json`; `data.rendered_html` is what the gateway
    // built from the catalog's own template, or from the package bytes a person
    // approved. Falling back to the stored copy handed the sandbox -- which runs
    // scripts -- exactly the bytes the gateway had just declined to run.
    //
    // Returning early on the missing page alone left the held document MOUNTED with the
    // grant it loaded under. Several reads arrive that way and none of them is evidence
    // that the held page is still the record's: a bound package with nothing applied
    // (`state: empty` -- version pruned, or nobody approved one), and a record whose
    // locked `template_id` the catalog no longer has, which resolves to no package slug
    // and composes nothing. Under any of them a reader goes on looking at a page the
    // record does not have, and if an admin ceiling has since restricted the owner
    // agent's egress that document still holds `allow-scripts` and can navigate its own
    // frame to a host now forbidden.
    //
    // So the page, its candidate and the script decision all go. `mayRunScript` drops
    // to `false` rather than to the permissive default because an absent `scripts`
    // field here is the ABSENCE OF A DECISION, and that cannot be read as the last
    // decision: the next read that does establish a page sets it from that page's own
    // answer, so nothing stays static that the server says may run. The tab then draws
    // its own empty or unavailable state, which is the honest answer for a binding with
    // nothing behind it.
    if (!data.rendered_html) {
      setCandidate(null)
      setGood(null)
      setMayRunScript(false)
      heldSurfaceRef.current = null
      return
    }
    const next: Loaded = {
      html: data.rendered_html,
      manifest: data.manifest,
      instanceVersion: data.instance_version,
      templateId: data.template?.id ?? '',
      templateVersion: data.template?.version ?? 0,
      // Absent means allowed: only a server that has decided the ceiling denies
      // the owner agent's egress sends `false`, so an older gateway keeps the
      // behaviour it shipped with instead of going silently static.
      scripts: (data as { scripts?: boolean }).scripts !== false,
      dashboard: identity,
      stagedMs: 'staged_ms' in data && typeof data.staged_ms === 'number' ? data.staged_ms : 0,
    }
    // Keyed on the INSTANCE VERSION. Contract v3 says every change bumps it, so it is
    // the server's own statement that this is a different page, and a different page is
    // what probation exists for.
    //
    // No surface comparison HERE: the guard at the top of this effect has already
    // cleared anything from another surface, so `prev` is either null or this surface's
    // own page. A second comparison in this setter would be unreachable, and an
    // unreachable guard is one a later reader trusts to be doing the work.
    setCandidate(prev => {
      if (!prev) return next
      if (prev.instanceVersion !== next.instanceVersion) return next
      return prev.html === next.html && prev.stagedMs === next.stagedMs ? prev : next
    })
    // A re-render of the SAME version is new VALUES, not a new page -- which is most
    // of what a refetch brings, and all of what an unadopted crewmate ever gets,
    // since the default instance sits at version 0 forever. Swapped in place with no
    // probation: the page on screen has already proved it loads, so withholding its
    // own fresher numbers behind a readiness beacon would freeze the tab on the
    // values it happened to open with.
    //
    setGood(prev => {
      if (!prev) return prev
      if (prev.instanceVersion !== next.instanceVersion) return prev
      return prev.html === next.html && prev.stagedMs === next.stagedMs ? prev : next
    })
    // Recorded on EVERY read, before and regardless of either promotion above. This is
    // the half that makes a revocation independent of the layout: the two setters can
    // both decline to move, and this one still carries the answer to the sandbox.
    setMayRunScript(next.scripts)
    // The surface this read established, for the guard at the top of the next one.
    heldSurfaceRef.current = next.dashboard
  }, [data, livePackage, suggestions])

  // The FIRST page is shown without probation: there is no previous page to keep,
  // so withholding it would leave the tab empty to protect nothing.
  useEffect(() => {
    if (candidate && !good) setGood(candidate)
  }, [candidate, good])

  const shown = good
  // The stored copy does not parse, so the gateway sent no rendered page. Held apart
  // from `isError`, which is the read itself failing: this read SUCCEEDED and told us
  // the copy is broken, and a page already on screen stays on screen under a band.
  const broken = data?.state === 'error'
  // The read has a page but the two effects above have not promoted it yet, so
  // `shown` is still null for a tick while `isLoading` is already false. Without
  // this the tab paints "could not be loaded" over a page that loaded fine --
  // invisible on a fast first paint, and the whole frame on a slow one.
  // `!broken` because that state never promotes a page: `wire()` carries its `html`,
  // so without it the tab waits on a first page that is never coming and shows the
  // skeleton for good.
  //
  // Keyed on `rendered_html` and not on `html` for the same reason the promotion
  // above is: a read that carries a stored copy the gateway declined to compose has
  // no page coming either, so waiting on it would show the skeleton for good.
  const awaitingFirstPage = Boolean(data?.rendered_html) && !good && !broken && !suggestions
  /**
   * THE DOCUMENT IS KEYED ON THE PAGE, NOT ON ITS VALUES.
   *
   * A same-version read swaps `good` to html with new values baked in. Building the
   * srcdoc from that html on every swap minted a new document per read, and the frame
   * reloaded on every refetch -- the whole tab flickering each time the data moved.
   * So the document is rebuilt only when the PAGE changes (version, template id or
   * template version), the theme changes, or `docRev` says a page that cannot take a
   * refill needs its new values the old way. A same-page read is posted into the open
   * document instead (the effect below).
   *
   * `shown.html` is read without being a dependency on purpose: whenever the document
   * IS rebuilt, it is built from the newest values, so a theme change never brings
   * back numbers a refill already replaced.
   */
  const [docRev, setDocRev] = useState(0)
  const shownKey = shown ? pageKey(shown) : null
  const srcdoc = useMemo(
    () =>
      shown && !isError
        // `rewriteBareLinks: false`: the sandbox withholds `allow-popups`, so a
        // `target="_blank"` link here would be a blocked popup (a dead click).
        ? // offlineScripts: the CDN script origins are dropped for THIS document.
          // The page is filled with this crewmate's own task titles, summaries
          // and costs, and since the preview/apply pair it can be written by an
          // agent -- so a permitted script origin is a channel
          // `connect-src 'none'` does not close: a page can put field values in
          // a CDN script's query string. A chart the page's own inline script
          // draws still works.
          buildSrcdoc({
            html: shown.html,
            themeVars,
            mode: theme,
            rewriteBareLinks: false,
            offlineScripts: true,
            // The phone side panel's closing drag starts over this frame.
            relayTouches: true,
          })
        : null,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the page, see above
    [shownKey, docRev, isError, themeVars, theme],
  )
  const mint = useSandboxDoc(srcdoc)
  const { pending, retry } = mint
  const failed = mint.failed || mint.stalled
  const runsScript = Boolean(mayRunScript && shown?.scripts)
  const grantKey = crewDashboardSandbox(runsScript)

  /**
   * A NEW DOCUMENT LOADS OFF SCREEN, AND THE OPEN FRAME IS REPLACED ONLY WHEN IT IS UP.
   *
   * The mint hands back a new URL whenever the document is rebuilt (a page the open
   * document cannot take as a refill, a theme change). Pointing the frame a reader is
   * looking at at that URL navigates it, and until the new document paints the frame
   * shows its own background: a black flash in the dark theme, on every agent field
   * write and on every fallback read.
   *
   * So `held` is the URL of the document the visible frame holds, and a minted URL
   * that differs from it is mounted in a second, invisible frame first (`stageUrl`).
   * That same element is moved on screen once it reports it came up, so the document
   * loads once. Nothing is held back where there is nothing to protect: the first
   * document, and a change of grant, both go straight to the visible frame.
   * The grant is part of the frame's identity (see its `key`), so a frame under a
   * withdrawn grant must be replaced at once, and cannot wait on the page.
   */
  const [held, setHeld] = useState<{ url: string | null; grant: string }>({ url: null, grant: grantKey })
  const stageUrl =
    mint.url !== null && held.url !== null && held.grant === grantKey && mint.url !== held.url
      ? mint.url
      : null
  const url = stageUrl !== null ? held.url : mint.url
  const stageUrlRef = useRef(stageUrl)
  stageUrlRef.current = stageUrl
  const grantKeyRef = useRef(grantKey)
  grantKeyRef.current = grantKey
  useEffect(() => {
    if (stageUrl !== null) return
    setHeld(prev => (prev.url === url && prev.grant === grantKey ? prev : { url, grant: grantKey }))
  }, [stageUrl, url, grantKey])
  /** What the off-screen frame had already shown when it moved on screen, so the
   *  "drawn" bookkeeping below does not wait for a `load` or a beacon that will not
   *  come again. Recorded as observed: a frame shown because the wait ran out has
   *  NOT reported ready, and a Keep that names a page nobody saw come up is wrong. */
  const preloadedRef = useRef<{ url: string; loaded: boolean; ready: boolean } | null>(null)
  const stageRef = useRef<HTMLIFrameElement | null>(null)
  /** When the current run of off-screen documents began. A read that lands while one
   *  is loading rebuilds it under a new url, and the wait is measured from the first,
   *  or writes arriving more often than the wait would keep the old document up for as
   *  long as they last. */
  const stagingSinceRef = useRef<number | null>(null)
  useEffect(() => {
    if (stageUrl === null) {
      stagingSinceRef.current = null
      return
    }
    if (stagingSinceRef.current === null) stagingSinceRef.current = Date.now()
    let loaded = false
    let ready = false
    const promote = () => {
      preloadedRef.current = { url: stageUrl, loaded, ready }
      setHeld({ url: stageUrl, grant: grantKeyRef.current })
    }
    // Not coming up in time is not a reason to keep the old document for good: it
    // would go on showing numbers that are behind with nothing saying so, and a page
    // whose script threw was shown as it was before this existed.
    const waited = Date.now() - stagingSinceRef.current
    const timer = window.setTimeout(promote, Math.max(0, READY_TIMEOUT_MS - waited))
    const el = stageRef.current
    const onLoad = () => {
      loaded = true
      // A page without script sends no beacon, so its load is all there is to wait on.
      if (!runsScript) {
        ready = true
        promote()
      }
    }
    const onMessage = (event: MessageEvent) => {
      const win = stageRef.current?.contentWindow
      if (!win || event.source !== win) return
      const payload = event.data as { type?: unknown } | null
      if (payload && payload.type === READY_MESSAGE_TYPE) {
        ready = true
        promote()
      }
    }
    el?.addEventListener('load', onLoad)
    if (runsScript) window.addEventListener('message', onMessage)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      el?.removeEventListener('load', onLoad)
    }
  }, [stageUrl, runsScript])

  // The staged page the person can Keep is the one MOUNTED, never the newest read:
  // while a mint is pending, or after one failed, the frame still holds the earlier
  // document. Each url is tagged with the srcdoc it was minted from when it lands
  // (the mint keeps only the latest request, so that is the srcdoc at that moment),
  // and nothing is offered until the url on screen was minted from `shown`'s own.
  const srcdocRef = useRef<string | null>(null)
  srcdocRef.current = srcdoc
  // A document can reach the screen after newer ones were minted, so the srcdoc a url
  // came from is recorded when it lands rather than read when it is shown.
  const mintedFromRef = useRef(new Map<string, string | null>())
  useEffect(() => {
    if (!mint.url) return
    const minted = mintedFromRef.current
    minted.set(mint.url, srcdocRef.current)
    while (minted.size > 8) minted.delete(minted.keys().next().value as string)
  }, [mint.url])
  const [urlSource, setUrlSource] = useState<{ url: string | null; srcdoc: string | null }>({ url: null, srcdoc: null })
  useEffect(() => {
    const from = url ? mintedFromRef.current.get(url) : undefined
    setUrlSource({ url, srcdoc: from !== undefined ? from : srcdocRef.current })
  }, [url])
  // STEP 2 (#20952): the sandbox-doc POST resolved and a URL is in hand. Timed once
  // per URL -- the mint keeps only the latest request, so a URL landing is that POST
  // completing. The step-1 timer carries this load; a probe only.
  useEffect(() => {
    if (url) timingRef.current?.step('step2-sandbox-post')
  }, [url])
  // And it must be DRAWN: a new `src` keeps the earlier document on screen until the
  // frame loads the new one, so the stamp waits for this url's `load` and, for a page
  // that runs script, its ready beacon as well. Reset whenever the url moves.
  const drawnUrlRef = useRef(url)
  drawnUrlRef.current = url
  const [drawn, setDrawn] = useState<{ url: string | null; loaded: boolean; ready: boolean }>({ url: null, loaded: false, ready: false })
  useEffect(() => {
    // A document loaded off screen brings what it already reported with it.
    const pre = url !== null && preloadedRef.current?.url === url ? preloadedRef.current : null
    setDrawn({ url, loaded: pre?.loaded ?? false, ready: pre?.ready ?? false })
  }, [url])
  const onFrameLoad = useCallback(() => {
    // STEP 3 (#20952): the iframe loaded the minted document -- the third and last
    // transfer. This completes the three-step load the "Drawing…" state covers.
    timingRef.current?.step('step3-frame-get')
    // This load's timer is spent: drop it so a later re-mint (a theme change, a
    // staged preview) cannot log its step 2 / step 3 against this finished load.
    timingRef.current = null
    setDrawn(prev => (prev.url === drawnUrlRef.current ? { ...prev, loaded: true } : prev))
  }, [])
  // Listened on the element rather than through `onLoad`, which the a11y lint reads
  // as an interaction handler on a non-interactive element. Re-attached whenever the
  // frame element can be replaced: a new url, or a new grant (its key).
  useEffect(() => {
    const el = frameRef.current
    if (!el || !url) return
    el.addEventListener('load', onFrameLoad)
    return () => el.removeEventListener('load', onFrameLoad)
  }, [url, grantKey, onFrameLoad])
  useEffect(() => {
    if (!staged) return
    const onMessage = (event: MessageEvent) => {
      const win = frameRef.current?.contentWindow
      if (!win || event.source !== win) return
      const payload = event.data as { type?: unknown } | null
      if (!payload || payload.type !== READY_MESSAGE_TYPE) return
      setDrawn(prev => (prev.url === drawnUrlRef.current ? { ...prev, ready: true } : prev))
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [staged])
  const isDrawn = drawn.url === url && drawn.loaded && (drawn.ready || !runsScript)
  const mountedStagedMs = staged && shown && url && !pending && !failed && isDrawn
    && urlSource.url === url && urlSource.srcdoc === srcdoc
    ? shown.stagedMs
    : 0
  useEffect(() => {
    onPreviewLoaded?.(mountedStagedMs)
  }, [onPreviewLoaded, mountedStagedMs])

  /**
   * THE REFILL: new values for the page already on screen, posted into it.
   *
   * Only for a document that said it can take one. Its ready beacon carries
   * `refill: true` when the page's own script listens for the refill
   * (`dashboard_frame._REFILL_PROBE_JS`); every builtin template does. A page that
   * does not would keep showing its old numbers, so it is re-minted instead, which is
   * what every page got before. The signal is per DOCUMENT URL: a new document has
   * said nothing until its own beacon, so it is re-minted until then.
   *
   * Posted only to THIS frame's own `contentWindow`, and the message carries only the
   * read the gateway already composed into this page's html. `'*'` is required: the
   * frame is sandboxed without `allow-same-origin`, so its origin is opaque and no
   * other targetOrigin can match it.
   */
  const shownRef = useRef(shown)
  shownRef.current = shown
  const urlRef = useRef(url)
  urlRef.current = url
  const refillUrlRef = useRef<string | null>(null)
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      // The off-screen frame speaks for the document that is about to be shown, so
      // what its beacon says is recorded against ITS url: it moves on screen without
      // a second beacon, and a refill ability recorded nowhere would make the next
      // read re-mint it.
      const visibleWin = frameRef.current?.contentWindow
      const stageWin = stageRef.current?.contentWindow
      const fromStage = Boolean(stageWin) && event.source === stageWin
      const win = fromStage ? stageWin : visibleWin
      if (!win || event.source !== win) return
      const payload = event.data as { type?: unknown; refill?: unknown } | null
      if (!payload || payload.type !== READY_MESSAGE_TYPE) return
      if (payload.refill !== true) {
        refillUrlRef.current = null
        return
      }
      refillUrlRef.current = fromStage ? stageUrlRef.current : urlRef.current
      // A read that landed while this document was loading is in `shown` but may not
      // be in the document, so the newest values are posted as soon as it can take
      // them. Same values again is a redraw, not a reload.
      const current = shownRef.current
      if (current) postRefill(win, current.html)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  const mountedRef = useRef<{ key: string; html: string } | null>(null)
  useEffect(() => {
    if (!shown) {
      mountedRef.current = null
      return
    }
    const key = pageKey(shown)
    const before = mountedRef.current
    mountedRef.current = { key, html: shown.html }
    // A new page is a new document, which the srcdoc above already builds.
    if (!before || before.key !== key || before.html === shown.html) return
    const win = frameRef.current?.contentWindow
    const docUrl = urlRef.current
    const refilled = Boolean(win && docUrl && refillUrlRef.current === docUrl && postRefill(win, shown.html))
    // A document still loading off screen was built from the values before this read,
    // so it is rebuilt too: it can only be handed these values once it beacons, and a
    // page that does not take refills would otherwise appear holding the old ones.
    if (refilled && stageUrlRef.current === null) return
    // NEVER STALE: a page that has not said it takes refills gets a new document.
    setDocRev(n => n + 1)
  }, [shown])

  /**
   * The readiness handshake. Listens for the document's beacon and, if a probating
   * candidate does not beacon in time, keeps the page that did.
   *
   * The listener is on `window` because a sandboxed frame's document is
   * unreachable from the parent -- `postMessage` is the only channel, which is the
   * same constraint that makes the height reporter a message rather than a read.
   */
  // A candidate that may NOT run script never probates. The beacon is the page saying
  // "I came up", sent by script -- so asking a static page for one is asking for a
  // message that cannot arrive, and the page it was meant to replace would stay on
  // screen with the grant the server just withdrew. Nothing is being trusted here that
  // probation was protecting: a static document cannot fail the way probation catches,
  // because the host fills its bindings itself and no script of its own has to run.
  const probating = Boolean(
    candidate && good && candidate.instanceVersion !== good.instanceVersion && candidate.scripts,
  )
  const timer = useRef<number | null>(null)

  // Promoted at once, for the reason above. Separate from the probation effect so the
  // two paths cannot both act on one candidate.
  useEffect(() => {
    if (!candidate || !good) return
    if (candidate.instanceVersion === good.instanceVersion) return
    if (candidate.scripts) return
    setGood(candidate)
    setReverted(false)
  }, [candidate, good])

  // A new page must actually be MOUNTED to get the chance to beacon, so the
  // probating candidate is what the srcdoc is built from while it is on probation.
  const probeSrcdoc = useMemo(
    () =>
      probating && candidate && !isError
        ? // offlineScripts: the CDN script origins are dropped for THIS document.
          // The page is filled with this crewmate's own task titles, summaries
          // and costs, and since the preview/apply pair it can be written by an
          // agent -- so a permitted script origin is a channel
          // `connect-src 'none'` does not close: a page can put field values in
          // a CDN script's query string. A chart the page's own inline script
          // draws still works.
          buildSrcdoc({
            html: candidate.html,
            themeVars,
            mode: theme,
            rewriteBareLinks: false,
            offlineScripts: true,
            // The phone side panel's closing drag starts over this frame.
            relayTouches: true,
          })
        : null,
    [probating, candidate, isError, themeVars, theme],
  )
  const probe = useSandboxDoc(probeSrcdoc)

  useEffect(() => {
    if (!probating || !candidate) return
    let settled = false
    const onMessage = (event: MessageEvent) => {
      if (settled) return
      const payload = event.data as { type?: string } | null
      if (!payload || payload.type !== READY_MESSAGE_TYPE) return
      settled = true
      setGood(candidate)
      setReverted(false)
    }
    window.addEventListener('message', onMessage)
    timer.current = window.setTimeout(() => {
      if (settled) return
      settled = true
      // The candidate never came up. The page on screen stays, and the band below
      // says so -- a reader looking at numbers is told they are not the newest
      // rather than left to assume they are.
      setReverted(true)
    }, READY_TIMEOUT_MS)
    return () => {
      window.removeEventListener('message', onMessage)
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
    // `probe.url` is a dependency because the band's Retry re-mints the candidate at a
    // NEW url, and that re-mint is the whole point of the button: without it here the
    // effect never re-runs, `settled` stays true from the timeout that produced the
    // band, and the fresh document's beacon is dropped by the guard above -- so Retry
    // could never promote the page it just re-minted.
  }, [probating, candidate, probe.url])

  // A badge-button ("Layout locked — unlock on Profile") that opens the Profile tab
  // when clicked (via onOpenProfileTab), so the person can unlock in one tap. When
  // onOpenProfileTab is absent the badge falls back to a non-interactive Badge.
  // Drawn in EVERY view that has a read, not only the page view: a
  // page locked while its record was healthy can later read as broken or empty, and
  // the person should still see why the crewmate is not changing it. Not on a staged
  // preview: that read is not the page the lock holds, so it carries no lock.
  const lockBadge = locked && !preview ? (
    <div className="shrink-0 flex justify-end px-2 pt-1">
      {onOpenProfileTab ? (
        // Clickable: goes straight to the Profile tab where the lock switch lives.
        // Uses the shared Btn primitive for consistent interactive semantics.
        <Btn
          type="button"
          onClick={onOpenProfileTab}
          className="inline-flex items-center gap-1 px-2 py-[2px] rounded-full text-[13px] font-medium whitespace-nowrap bg-[var(--bg-hover)] text-accent underline hover:opacity-80 focus-ring"
          aria-label={i18nT('pages.membersPage.dashboard_locked_badge_label')}
          data-testid="crew-dashboard-lock-badge"
        >
          <Lock size={11} aria-hidden="true" />
          {i18nT('pages.membersPage.dashboard_locked_badge')}
        </Btn>
      ) : (
        <Badge
          variant="muted"
          role="status"
          aria-label={i18nT('pages.membersPage.dashboard_locked_badge_label')}
          data-testid="crew-dashboard-lock-badge"
          style={{ cursor: 'default' }}
        >
          <Lock size={11} aria-hidden="true" />
          {i18nT('pages.membersPage.dashboard_locked_badge')}
        </Badge>
      )}
    </div>
  ) : null

  if (isLoading || awaitingFirstPage) {
    return (
      <div className="p-4 space-y-1.5" data-testid="crew-dashboard-loading" aria-hidden>
        <div className="h-3 rounded bg-bg-hover animate-pulse" />
        <div className="h-3 w-2/3 rounded bg-bg-hover animate-pulse" />
      </div>
    )
  }

  // 404 `no_preview`: the staged page was applied or expired. Nothing is broken
  // and a retry reads the same answer, so no error styling and no Retry.
  if (previewGone) {
    // A host that listens moves to the live page on this same answer; drawing the
    // closed notice first would flash it in between.
    if (onPreviewGone) return null
    return (
      <div className="p-4 text-sm text-muted" data-testid="crew-dashboard-preview-gone">
        {i18nT('pages.chat.dashboardPreviewPanel.gone')}
      </div>
    )
  }

  // 403 `owner_only`: the page carries the crewmate's task titles, so only its
  // owner reads it.
  if (isError && isOwnerOnly(error)) {
    return (
      <div className="p-4">
        {/* No hand-off and no Retry: the hand-off navigates to /chat, unmounting the
            Members page's unsaved Profile and crew-editor drafts, and a retry reads
            the same refusal. */}
        <ErrorNotice message={i18nT('pages.membersPage.dashboard_owner_only')} testId="crew-dashboard-owner-only" />
      </div>
    )
  }

  if (isError) {
    return (
      <div className="p-4 space-y-1.5">
        {/* No agent hand-off: this frame is the Members page's Dashboard tab, and
            the hand-off navigates to /chat, unmounting the page's unsaved Profile
            and crew-editor drafts without asking its leave guard.

            LOADED, not drawn: the read itself failed, so no page ever arrived to
            draw. "Drawn" is the word for the branch below, where a page DID arrive
            and would not render. Two dead ends that read the same tell a reader
            nothing about which one they are in. */}
        <ErrorNotice message={i18nT('pages.membersPage.dashboard_unavailable')} testId="crew-dashboard-error" />
        <Btn onClick={() => void refetch()} data-testid="crew-dashboard-error-retry">
          <RotateCw className="lucide-inline" aria-hidden />
          {i18nT('pages.membersPage.webview_retry')}
        </Btn>
      </div>
    )
  }

  if (suggestions) {
    return (
      <div className="h-full min-h-0 flex flex-col" data-testid="crew-dashboard-none">
        {lockBadge}
        <CrewDashboardEmpty
          member={target.kind === 'member' ? target.member : ''}
          displayName={displayName}
          avatar={avatar}
          onAct={onAct}
        />
      </div>
    )
  }

  if (broken && !shown) {
    return (
      // Nothing good to keep, so the frame is not drawn at all rather than drawn from
      // markup the gateway refused to fill.
      <div className="p-4 space-y-1.5">
        {lockBadge}
        {/* No hand-off: this tab sits on the Members page, which holds unsaved
            Profile and crew-editor drafts; the hand-off navigates to /chat and
            unmounts them past the page's leave guard. Reasoned in full at the
            isError branch above -- every notice in this file protects the same
            two drafts, and Retry is the recovery that costs nothing. */}
        <ErrorNotice
          message={i18nT('pages.membersPage.dashboard_render_error')}
          testId="crew-dashboard-broken"
        />
        <Btn onClick={() => void refetch()} data-testid="crew-dashboard-broken-retry">
          <RotateCw className="lucide-inline" aria-hidden />
          {i18nT('pages.membersPage.webview_retry')}
        </Btn>
      </div>
    )
  }

  if (!shown && data?.state === 'empty') {
    return (
      // Nothing is adopted and nothing is wrong, which are different answers. The
      // route reports `empty` for a crewmate with no page, so this is a state and
      // not a failure: no error styling and NO retry, because re-reading returns
      // the same empty answer and a button that cannot change anything reads as a
      // fault the reader could clear.
      <div className="p-4 text-sm text-muted" data-testid="crew-dashboard-none">
        {lockBadge}
        {i18nT('pages.membersPage.dashboard_none_yet')}
      </div>
    )
  }

  if (!shown) {
    return (
      // A page the gateway could not resolve while NOT reporting `empty`: the read
      // answers a body for every other state, so a missing one here is the registry
      // failing to load. The copy names THAT, not publishing -- a reader told to
      // publish would watch the crewmate confirm it published and still see
      // nothing, because this tab renders the dashboard and no longer renders a
      // published document.
      <div className="p-4 space-y-1.5">
        {lockBadge}
        {/* No hand-off: the Members page's unsaved Profile and crew-editor drafts,
            as at the isError branch above. */}
        <ErrorNotice
          message={i18nT('pages.membersPage.dashboard_unavailable')}
          testId="crew-dashboard-empty"
        />
        <Btn onClick={() => void refetch()} data-testid="crew-dashboard-empty-retry">
          <RotateCw className="lucide-inline" aria-hidden />
          {i18nT('pages.membersPage.webview_retry')}
        </Btn>
      </div>
    )
  }

  return (
    <div className="relative h-full min-h-0 flex flex-col" data-testid="crew-dashboard-frame" data-preview={preview ? 'true' : undefined}>
      {lockBadge}
      {broken && (
        // The page on screen is the last one that parsed. Banded, never replaced: the
        // alternative is blanking a working page because a NEWER copy is broken.
        <div
          className="shrink-0 border-b border-border p-2 flex items-start gap-2"
          data-testid="crew-dashboard-broken-band"
        >
          {/* No hand-off: the Members page's unsaved Profile and crew-editor
              drafts, as at the isError branch above. It matters more in a BAND
              than in the dead ends: a working page is on screen behind this, and
              navigating away would discard the drafts to fix a banner. */}
          <ErrorNotice
            message={i18nT('pages.membersPage.dashboard_kept_last_good')}
            className="flex-1 min-w-0"
            testId="crew-dashboard-broken-kept"
          />
          <Btn onClick={() => void refetch()} className="shrink-0">
            <RotateCw className="lucide-inline" aria-hidden />
            {i18nT('pages.membersPage.webview_retry')}
          </Btn>
        </div>
      )}
      {reverted && (
        // THE KEPT-PAGE BAND, above the frame and never over it: the document on
        // screen keeps every pixel it earned. It is distinct from the document's
        // OWN stale band, which is about values that did not resolve; this one is
        // about a newer page that did not load at all.
        <div
          className="shrink-0 border-b border-border p-2 flex items-start gap-2"
          data-testid="crew-dashboard-kept-band"
        >
          {/* No hand-off: the Members page's unsaved Profile and crew-editor
              drafts, as at the isError branch above. */}
          <ErrorNotice
            message={i18nT('pages.membersPage.dashboard_kept_last_good')}
            className="flex-1 min-w-0"
            testId="crew-dashboard-kept"
          />
          <Btn disabled={probe.pending} onClick={probe.retry} className="shrink-0">
            <RotateCw className="lucide-inline" aria-hidden />
            {i18nT('pages.membersPage.webview_retry')}
          </Btn>
        </div>
      )}
      {failed && (
        <div
          className="shrink-0 border-b border-border p-2 flex items-start gap-2"
          data-testid="crew-dashboard-mint-error-band"
        >
          {/* No hand-off: the Members page's unsaved Profile and crew-editor
              drafts, as at the isError branch above. */}
          <ErrorNotice
            // ONE sentence for one situation. This band and the kept-page band above
            // both mean "the page you are looking at is the last one that loaded", so
            // they say it in the same words; two wordings for one state left a reader
            // deciding whether they were two different problems.
            message={i18nT(
              url
                ? 'pages.membersPage.dashboard_kept_last_good'
                : 'pages.membersPage.dashboard_render_error',
            )}
            className="flex-1 min-w-0"
            testId="crew-dashboard-mint-error"
          />
          <Btn disabled={pending} onClick={retry} className="shrink-0">
            <RotateCw className="lucide-inline" aria-hidden />
            {i18nT('pages.membersPage.webview_retry')}
          </Btn>
        </div>
      )}
      {refetchError && (
        <div
          className="shrink-0 border-b border-border p-2 flex items-start gap-2"
          data-testid="crew-dashboard-refetch-error-band"
        >
          {/* The page keeps its values, which is right. What a reader cannot otherwise
              learn is that those values are older than the record: the page's own stale
              band is raised by a read, and this is the case where the read never came.
              Beside the page rather than over it, like the two bands above.

              ITS OWN SENTENCE, not the kept-last-good one. That string says a newer
              VERSION of the page did not load, which is a different situation with a
              different remedy: here the page is the right one and only its numbers are
              behind. Reusing it would tell the reader to expect a new page that is not
              coming. */}
          {/* No hand-off: the Members page's unsaved Profile and crew-editor
              drafts, as at the two bands above. This notice only reports that the
              values on screen are behind; nothing is discarded by showing it, and
              the Retry beside it re-reads rather than navigating away. */}
          <ErrorNotice
            message={i18nT('pages.membersPage.dashboard_values_stale')}
            className="flex-1 min-w-0"
            testId="crew-dashboard-refetch-error"
          />
          {/* REDOES WHAT FAILED. The notice is raised by one thing -- the whole read
              the page asked for did not arrive -- so this asks for that read again.
              `refetch()` on the dashboard query would re-read the RECORD, which is not
              what failed: the page on screen is the right page and only its numbers are
              behind, so a reader would watch the notice clear with the same stale
              values under it. */}
          <Btn
            disabled={refetchPending}
            onClick={askWholeRead}
            className="shrink-0"
            data-testid="crew-dashboard-refetch-retry"
          >
            <RotateCw className="lucide-inline" aria-hidden />
            {i18nT('pages.membersPage.webview_retry')}
          </Btn>
        </div>
      )}
      {url ? [
        <iframe
          // THE GRANT IS PART OF THE FRAME'S IDENTITY, and it has to be.
          //
          // A frame's sandbox flags are computed when its document is NAVIGATED, so
          // rewriting the attribute on a frame that already holds a running document
          // leaves that document with the grants it loaded under: it keeps running, keeps
          // taking pushed values, and can still navigate itself to a host the ceiling now
          // forbids. Withdrawing script is a governance act, so it may not wait for the
          // page to do anything -- and a same-version revocation changes nothing else at
          // all, so there is no other reason for this element to be replaced.
          //
          // Keyed on the GRANT alone rather than on the read: a key that moved with every
          // poll would discard the page state the push exists to keep (a scroll position,
          // an animation, a 3D camera) once a minute.
          //
          // AND THE URL: the element that loaded a document off screen is the one that
          // is shown when it is promoted, so React has to see the same key on both
          // sides of the swap. A key without the url would navigate this frame to the
          // new document, which is the blank frame the swap exists to avoid.
          key={`frame-${grantKey}-${url}`}
          ref={frameRef}
          src={url}
          // The SERVER's latest answer, not the promoted document's. A revocation that
          // arrived after this document was promoted has to reach the frame that is
          // mounted, which is what the key above makes happen.
          sandbox={crewDashboardSandbox(mayRunScript && shown.scripts)}
          className="flex-1 min-h-0 w-full border-none bg-bg"
          style={{ colorScheme: theme }}
          title={i18nT('pages.membersPage.dashboard_frame_title', { crew: displayName })}
          data-testid="crew-dashboard-iframe"
          data-instance-version={shown.instanceVersion}
        />,
        stageUrl !== null && (
          // The document that replaces the one above, loading where nobody sees it.
          // Full size and transparent rather than one pixel: a page that sizes a chart
          // from its window on load would draw at 1x1 and keep that size once shown.
          // It sits after the visible frame and never before, so promotion removes the
          // old element and leaves this one in place -- moving an iframe in the DOM
          // reloads it.
          <iframe
            key={`frame-${grantKey}-${stageUrl}`}
            ref={stageRef}
            src={stageUrl}
            sandbox={crewDashboardSandbox(mayRunScript && shown.scripts)}
            aria-hidden
            tabIndex={-1}
            className="absolute inset-x-0 bottom-0 h-full w-full opacity-0 pointer-events-none border-none"
            style={{ colorScheme: theme }}
            title={i18nT('pages.membersPage.dashboard_rendering')}
            data-testid="crew-dashboard-swap"
          />
        ),
      ] : failed && !pending ? null : (
        <div className="p-4 text-[11px] text-muted">{i18nT('pages.membersPage.dashboard_rendering')}</div>
      )}
      {probating && probe.url && (
        // The candidate, mounted OFF SCREEN so it can run and beacon. One pixel
        // rather than `display: none`: a hidden iframe does not always run its
        // scripts, and a candidate that never runs can never prove itself -- which
        // would make every new page look broken.
        <iframe
          // Keyed on its own grant for the reason the mounted frame is: a candidate
          // whose grant narrows while it is proving itself must be re-navigated, or it
          // goes on running under the wider one it started with.
          key={`probe-grant-${crewDashboardSandbox(
            mayRunScript && (candidate ? candidate.scripts : shown.scripts),
          )}`}
          src={probe.url}
          // A candidate on probation gets the SAME grants its own response
          // decided, so a page cannot earn promotion while running under looser
          // ones than it will be mounted with.
          sandbox={crewDashboardSandbox(mayRunScript && (candidate ? candidate.scripts : shown.scripts))}
          aria-hidden
          tabIndex={-1}
          className="absolute w-px h-px opacity-0 pointer-events-none border-none"
          // A real title, even though `aria-hidden` already takes this out of the
          // accessibility tree: an empty one is a lint error on every iframe and
          // suppressing the rule here would suppress it for a frame a reader DOES
          // meet the next time this file is edited.
          title={i18nT('pages.membersPage.dashboard_rendering')}
          data-testid="crew-dashboard-probe"
        />
      )}
    </div>
  )
}

export { ACT_MESSAGE_TYPE, DATA_MESSAGE_TYPE, REFETCH_MESSAGE_TYPE, READY_MESSAGE_TYPE, READY_TIMEOUT_MS }
