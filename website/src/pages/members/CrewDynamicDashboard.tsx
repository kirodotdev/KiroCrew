import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RotateCw } from 'lucide-react'
import { api, type DashboardPackageRef } from '../../api/client'
import {
  DASHBOARD_BLOCK_PATCH_FRAME,
  PAGE_FULL_PAINT_MESSAGE_TYPE,
  patchFitsPage,
  seedVersion,
  subscribeBlockPatch,
  verdictFor,
  type DashboardBlockPatch,
} from './dashboardBlockPush'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useTheme } from '../../hooks/useTheme'
import { useSandboxDoc } from '../../hooks/useSandboxDoc'
import { useFrameOpenLink } from '../../hooks/useFrameOpenLink'
import { buildSrcdoc, readThemeVars } from '../../lib/widgetSrcdoc'
import { i18nT } from '../../i18n/t'
import { apiErrorCode } from '../../api/apiError'
import { useLanguage } from '../../i18n/LanguageProvider'

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
 * The FULL-PAINT message type. Mirrors `dashboard_frame.DATA_MESSAGE_TYPE`.
 *
 * Reached through `dashboardBlockPush` rather than spelled twice. It is NOT what
 * a fold push sends -- that forwards the renderer's own patch, which carries a
 * different type of its own, because this listener re-initialises every block.
 */
const DATA_MESSAGE_TYPE = PAGE_FULL_PAINT_MESSAGE_TYPE

/** The page's readiness beacon. Mirrors `dashboard_frame.READY_MESSAGE_TYPE`. */
const READY_MESSAGE_TYPE = 'kirocrew-dashboard:ready'

/** A page's request to put a reply in the chat box: `{ type, text }`.
 *
 * The page is crewmate-written HTML, so it never SENDS anything: the host only
 * fills the composer, and the person presses send. That is the whole safety
 * line -- a page that could send would be a page that could speak as the user. */
const ACT_MESSAGE_TYPE = 'kirocrew-dashboard:act'
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

/**
 * A page the read resolved: the composed document, and what the push path needs
 * to decide whether a patch belongs to it.
 *
 * `pkg` is nullable and the document is not, which is the asymmetry worth
 * reading twice. The document is what a person sees, so without it there is no
 * page at all. The package reference is what the PUSH PATH needs -- it carries
 * the `layout` a patch's own `layout` is compared against -- so a page served by
 * the template path, which has no package, still draws and simply has no patch
 * stream, with the finite fallback refetch underneath it.
 *
 * `blockIds` is the set of blocks the document is showing, taken from the read's
 * own `blocks` map. It is here rather than derived on demand because the browser
 * never receives `view.blocks`: the document is composed server-side, so the
 * keys of that map ARE the view as far as this code can know it.
 */
interface Loaded {
  html: string
  pkg: DashboardPackageRef | null
  /** The package artifact's version, or 0 on the template path. The number a
   *  patch's `layout` must equal. */
  layout: number
  blockIds: string[]
}

/**
 * The crewmate's dynamic dashboard, in the Dashboard tab beside the chat.
 *
 * It renders the crewmate's DASHBOARD PACKAGE -- a `kind="dashboard"` artifact
 * holding `bound_to`, `model`, `view` and `theme` -- which the controller serves
 * from `GET /api/members/{slug}/dashboard` together with the document it composed
 * from that package's view. The gateway fills every block from the folded values,
 * sets `window.kirocrew` and raises its own stale band. This component's jobs are
 * the four the document cannot do for itself: mint it into a sandbox, keep the
 * last page that successfully loaded, hand it the pushed blocks a fold advance
 * produces, and offer a way out when nothing loads at all.
 *
 * ## THERE IS NO DEFAULT PAGE
 *
 * A crewmate with no package has no dashboard, and this tab says so. Not a
 * builtin template, not a starter layout, not an empty copy of someone else's
 * page: the EMPTY STATE, which is an answer rather than a failure. `state` is
 * the only field that decides whether a page draws, so a body that carries a
 * document for a non-live state does not get it drawn -- which is the exact
 * regression `CrewDynamicDashboard.test.tsx` pins, because a fallback is a
 * single `if` away at all times and nothing about it looks wrong in a diff.
 *
 * The LAST GOOD PAGE is kept on two distinct failures, which is why the state is a
 * held value rather than a flag:
 *
 * 1. the mint failed -- `useSandboxDoc` already keeps its previous URL on a later
 *    failure by design, so the previous document stays on screen;
 * 2. the mint SUCCEEDED and the document never beaconed. Nothing below the frame
 *    can see that, so the held `Loaded` is re-mounted and the new html is dropped
 *    until the next read brings a different one.
 */
export default function CrewDynamicDashboard({ slug, member, displayName, onAct, preview = false, onPreviewGone }: {
  slug: string
  member: string
  displayName: string
  /** Put a reply the page offered into the chat box. Absent: the page's options do nothing. */
  onAct?: (text: string) => void
  /** Render the STAGED page instead of the adopted one. Its own cache entry, so the
   *  live tab never shows a page nobody applied; still under the `member-dashboard`
   *  prefix, so the frame that says the page changed refetches it too. */
  preview?: boolean
  /** Told whether the staged page is gone (applied or expired), so a host can drop its "not applied" label. */
  onPreviewGone?: (gone: boolean) => void
}) {
  const frameRef = useRef<HTMLIFrameElement | null>(null)
  const onActRef = useRef(onAct)
  onActRef.current = onAct
  // The page's options. Only a message from THIS frame's window counts: any other
  // window posting the same shape is ignored.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const win = frameRef.current?.contentWindow
      if (!win || event.source !== win) return
      const text = actText(event.data)
      if (text && onActRef.current) onActRef.current(text)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  // A pull request the page links to: this frame only, GitHub PR URLs only.
  useFrameOpenLink([frameRef])
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])

  // The language the page renders its own words in. In the key, so switching the
  // UI language re-reads the page in the new one.
  const { resolved: locale } = useLanguage()

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['member-dashboard', slug, member, locale, preview ? 'preview' : 'live'],
    queryFn: () => (preview ? api.memberDashboard(slug, member, locale, true) : api.memberDashboard(slug, member, locale)),
    enabled: Boolean(slug) && Boolean(member),
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
  const previewGone = preview && isError && apiErrorCode(error) === 'no_preview'
  useEffect(() => { onPreviewGone?.(previewGone) }, [onPreviewGone, previewGone])

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

  useEffect(() => {
    if (!data) return
    // STATE `error` NEVER becomes a page. The read answers 200 for it, but the
    // controller deliberately does not compose that state -- no data island, no
    // bootstrap, no ready beacon. Promoting it would draw placeholder markup as a
    // healthy dashboard: every cell empty, nothing marked missing, no band.
    if (data.state === 'error') return
    // STATE `empty` NEVER BECOMES A PAGE EITHER, AND THIS LINE IS THE FALLBACK
    // REMOVAL.
    //
    // The route used to answer `empty` WITH a rendered DEFAULT TEMPLATE, on the
    // reasoning that "an empty frame answers none of the questions a person opened
    // the tab with" -- and the old promotion here accepted it, because it refused
    // only `error`. So a crewmate who had composed nothing was shown a full
    // dashboard of a shipped layout, carrying their own fold values, with
    // `instance_version` 0 and nothing on it saying it was not theirs.
    //
    // v3's rule is that there is no default page: a dashboard exists once the agent
    // writes a layout, and until then the empty state IS the answer. A rule like
    // that cannot live in the server alone -- the body is free to carry a
    // `rendered_html` for any state it likes, and this is what declines to draw it.
    //
    // DELIBERATELY NOT a `state !== 'live'` gate, which would have been one
    // character shorter and would also have stopped `stale`. A stale instance is a
    // crewmate's OWN adopted dashboard whose template shipped a new version; it is
    // not a fallback, and refusing it would blank a page the crewmate chose in
    // order to remove one nobody did.
    if (data.state === 'empty') return
    // No composed document is not a page either. The renderer refused, or the
    // package named a block it could not draw; either way there is nothing to
    // mount, and the unavailable state below says that truthfully rather than
    // mounting something adjacent.
    if (!data.rendered_html) return
    const next: Loaded = {
      html: data.rendered_html,
      // Absent on the template path, which has no package. The page still draws;
      // see `Loaded`.
      pkg: data.package ?? null,
      layout: data.package?.version ?? 0,
      blockIds: Object.keys(data.blocks ?? {}),
    }
    // Keyed on the PACKAGE VERSION, not the html: a dashboard artifact versions
    // only when `model` / `view` / `theme` change, so this number moving IS the
    // server's statement that the layout is different -- and a different layout is
    // exactly what probation exists for, because it is the case where a page that
    // used to load might not any more.
    setCandidate(prev => {
      if (!prev) return next
      if (prev.layout !== next.layout) return next
      return prev.html === next.html ? prev : next
    })
    // A re-render at the SAME version is new VALUES, not a new layout -- which is
    // most of what a refetch brings, since values never version. Swapped in place
    // with no probation: the page on screen has already proved it loads, so
    // withholding its own fresher numbers behind a readiness beacon would freeze
    // the tab on the values it happened to open with.
    setGood(prev => {
      if (!prev || prev.layout !== next.layout) return prev
      return prev.html === next.html ? prev : next
    })
  }, [data])

  // A DASHBOARD THAT WENT AWAY IS GONE, and the held page goes with it.
  //
  // "Keep the last good page" is for a page that FAILED -- a mint that did not
  // settle, a document that never beaconed -- where the previous one is still the
  // best available answer about the same dashboard. `empty` is not that: it is the
  // read saying this crewmate has no dashboard, so the page on screen is a layout
  // that no longer exists, drawn under the name of a crewmate who no longer has
  // one. Holding it would reintroduce the default page from the other direction:
  // not a builtin served to someone with nothing, but a deleted one that never
  // stops being served.
  //
  // `error` is deliberately NOT here. It says the dashboard is there and could not
  // be composed, which is the failure case the kept-page band exists for.
  useEffect(() => {
    if (data?.state !== 'empty') return
    setGood(null)
    setCandidate(null)
    setReverted(false)
  }, [data?.state])

  // The FIRST page is shown without probation: there is no previous page to keep,
  // so withholding it would leave the tab empty to protect nothing.
  useEffect(() => {
    if (candidate && !good) setGood(candidate)
  }, [candidate, good])

  const shown = good
  // The dashboard exists and does not parse, so the controller composed nothing.
  // Held apart from `isError`, which is the read itself failing: this read
  // SUCCEEDED and told us the stored dashboard is broken, and a page already on
  // screen stays on screen under a band.
  const broken = data?.state === 'error'
  // The read has a page but the two effects above have not promoted it yet, so
  // `shown` is still null for a tick while `isLoading` is already false. Without
  // this the tab paints "could not be loaded" over a page that loaded fine --
  // invisible on a fast first paint, and the whole frame on a slow one.
  //
  // THE SAME TWO STATES the promotion refuses, and it has to be the same two: a
  // body the promotion will never accept has no page coming, so waiting on it
  // would hold the skeleton up for good over an answer the tab already has. For
  // `empty` that answer is every crewmate's first one, so the skeleton would be
  // the whole feature.
  const awaitingFirstPage =
    Boolean(data?.rendered_html)
    && data?.state !== 'empty'
    && !good
    && !broken
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
          })
        : null,
    [shown, isError, themeVars, theme],
  )
  const mint = useSandboxDoc(srcdoc)
  const { url, pending, retry } = mint
  const failed = mint.failed || mint.stalled

  /**
   * The readiness handshake. Listens for the document's beacon and, if a probating
   * candidate does not beacon in time, keeps the page that did.
   *
   * The listener is on `window` because a sandboxed frame's document is
   * unreachable from the parent -- `postMessage` is the only channel, which is the
   * same constraint that makes the height reporter a message rather than a read.
   */
  const probating = Boolean(candidate && good && candidate.layout !== good.layout)
  const timer = useRef<number | null>(null)

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
          })
        : null,
    [probating, candidate, isError, themeVars, theme],
  )
  const probe = useSandboxDoc(probeSrcdoc)

  /**
   * THE BLOCK PATCH, received.
   *
   * A fold advancing no longer re-reads the whole dashboard. The controller
   * pushes the blocks that subscribe to that fold, and this hands them to the
   * document -- the one channel there is, because the frame sits on an opaque
   * origin and its DOM is unreachable from here.
   *
   * `lastVersion` is a ref and not state deliberately: it is a position in a
   * stream, read and written inside one callback, and making it state would
   * re-run this effect on every frame and re-subscribe mid-stream.
   *
   * Every answer but `apply` is handled by RE-READING rather than by patching,
   * because the push protocol is an optimisation over the read and never the
   * only way a value arrives. The fallback refetch sits underneath all of it.
   */
  const lastVersion = useRef<number | null>(null)
  // Re-seeded on every read, so a refetch for any reason -- a layout change, a
  // gap, the finite fallback -- restarts the stream at the position the body was
  // composed at rather than at whatever the previous page had reached.
  useEffect(() => {
    lastVersion.current = seedVersion(data)
  }, [data])

  // THE FRAME NAME, CHECKED BOTH WAYS. The router dispatches on a static case, so
  // this bundle holds the type string; the controller also NAMES it in the body
  // for exactly this comparison. A rename that reaches the server first turns the
  // push path off, and a silent push path is indistinguishable from a quiet crew
  // log -- so it is said out loud once, where a developer running the dashboard
  // sees it, rather than discovered from a page that stopped moving.
  const serverFrame = data?.push_frame
  useEffect(() => {
    if (!serverFrame || serverFrame === DASHBOARD_BLOCK_PATCH_FRAME) return
    // eslint-disable-next-line no-console
    console.warn(
      `[dashboard] the gateway pushes "${serverFrame}" and this build listens for `
        + `"${DASHBOARD_BLOCK_PATCH_FRAME}": block patches will not arrive and the tab `
        + 'falls back to its refetch interval.',
    )
  }, [serverFrame])

  const onPatch = useCallback(
    (patch: DashboardBlockPatch) => {
      const at = shown
      // No page, so nothing to paint into. Not a refetch either: whatever state
      // the tab is in has its own answer and a patch cannot improve it.
      if (!at) return
      const verdict = verdictFor(patch, { layout: at.layout, lastVersion: lastVersion.current })
      if (verdict === 'refetch') {
        // The READ re-seeds the position, not this line. `held` comes from the
        // body -- `push_version` and `package.version` -- because a refetch exists
        // precisely for the case where what this tab holds cannot be trusted, and
        // taking the new position from the frame that failed the check would carry
        // the untrustworthy half forward.
        void refetch()
        return
      }
      // THE VIEW HAS TO AGREE. A patch naming a block this page is not showing
      // means the page and the controller disagree about the view while `layout`
      // says they do not, which is the one case that number cannot catch -- so it
      // is answered the same way a layout change is.
      //
      // A page with no package (`pkg === null`, the template path) lands here too:
      // it has no patch stream at all, so a frame addressed to it is a
      // disagreement by definition.
      if (!at.pkg || !patchFitsPage(patch, at.blockIds)) {
        void refetch()
        return
      }
      const win = frameRef.current?.contentWindow
      if (!win) return
      // THE RENDERER'S OWN PATCH, FORWARDED VERBATIM. Not a message built here:
      // it arrives with its own type, its own per-block narrowing and its own
      // formatted strings, so the narrowing, the formatting and the message name
      // all stay in one language and this side constructs no payload at all.
      //
      // Deliberately NOT the full-paint message. That listener replaces the whole
      // read and RE-INITIALISES every block, so using it for a fold advance gives
      // a block that owns a canvas a second canvas, with two scenes animating over
      // each other on top of the cells they were drawing. The two message types
      // are distinct for that reason and a test pins that they differ.
      //
      // `'*'` is the only deliverable target: the document is sandboxed WITHOUT
      // `allow-same-origin`, so it has an opaque origin that cannot be named. The
      // bound on what that costs is the document itself -- it runs no network
      // (`connect-src 'none'`), carries no agent-authored script, and checks that
      // the sender is its own `parent` before reading a word of this.
      win.postMessage(patch.patch, '*')
      // `held` advances ONLY on an applied frame, which is what makes the strict
      // check above mean anything: the next frame is checked against the last one
      // this page actually painted.
      lastVersion.current = patch.version
    },
    [shown, refetch],
  )

  useEffect(() => subscribeBlockPatch(slug, onPatch), [slug, onPatch])

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
    return (
      <div className="p-4 text-sm text-muted" data-testid="crew-dashboard-preview-gone">
        {i18nT('pages.chat.dashboardPreviewPanel.gone')}
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

  if (data?.state === 'empty') {
    return (
      // NOTHING COMPOSED AND NOTHING WRONG, which are different answers. The
      // controller reports `empty` for a crewmate with no dashboard package, so
      // this is a state and not a failure: no error styling and NO retry, because
      // re-reading returns the same empty answer and a button that cannot change
      // anything reads as a fault the reader could clear. What clears it is the
      // crewmate composing a dashboard.
      //
      // UNCONDITIONAL, and that is the whole of v3's "no default page" on this
      // side. Three ways this branch used to be reachable past a page are now
      // closed by it: the body's own `rendered_html` cannot promote itself (the
      // `state !== 'live'` gate above), a held page from an earlier read is
      // dropped rather than kept (the clearing effect above), and the old
      // `!shown &&` guard that let either of those win is gone. A dashboard that
      // was deleted is gone, and a ghost of it under this crewmate's name is the
      // worst of the three: it reads as current.
      <div className="p-4 text-sm text-muted" data-testid="crew-dashboard-none">
        {i18nT('pages.membersPage.dashboard_none_yet')}
      </div>
    )
  }

  if (broken && !shown) {
    return (
      // Nothing good to keep, so the frame is not drawn at all rather than drawn from
      // a package the controller refused to compose.
      <div className="p-4 space-y-1.5">
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

  if (!shown) {
    return (
      // A page the controller could not resolve while NOT reporting `empty`: the
      // read answers a body for every state, so a `live` body with no document
      // here is the composition failing rather than an absent dashboard. The copy
      // names THAT, not composing -- a reader told to ask the crewmate to compose
      // one would watch it confirm it had and still see nothing.
      <div className="p-4 space-y-1.5">
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
    <div className="h-full min-h-0 flex flex-col" data-testid="crew-dashboard-frame">
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
      {url ? (
        <iframe
          ref={frameRef}
          src={url}
          sandbox={CREW_DASHBOARD_SANDBOX}
          className="flex-1 min-h-0 w-full border-none bg-bg"
          style={{ colorScheme: theme }}
          title={i18nT('pages.membersPage.dashboard_frame_title', { crew: displayName })}
          data-testid="crew-dashboard-iframe"
          data-layout-version={shown.layout}
        />
      ) : failed && !pending ? null : (
        <div className="p-4 text-[11px] text-muted">{i18nT('pages.membersPage.dashboard_rendering')}</div>
      )}
      {probating && probe.url && (
        // The candidate, mounted OFF SCREEN so it can run and beacon. One pixel
        // rather than `display: none`: a hidden iframe does not always run its
        // scripts, and a candidate that never runs can never prove itself -- which
        // would make every new page look broken.
        <iframe
          src={probe.url}
          sandbox={CREW_DASHBOARD_SANDBOX}
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

export { ACT_MESSAGE_TYPE, DATA_MESSAGE_TYPE, READY_MESSAGE_TYPE, READY_TIMEOUT_MS }
