import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RotateCw } from 'lucide-react'
import { api, type DashboardManifest } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useTheme } from '../../hooks/useTheme'
import { useSandboxDoc } from '../../hooks/useSandboxDoc'
import { buildSrcdoc, readThemeVars } from '../../lib/widgetSrcdoc'
import { i18nT } from '../../i18n/t'

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
 *   nothing about a status page needs.
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

/** The gateway's refill message type. Mirrors `dashboard_frame.DATA_MESSAGE_TYPE`. */
const DATA_MESSAGE_TYPE = 'kirocrew-dashboard:data'

/** The page's readiness beacon. Mirrors `dashboard_frame.READY_MESSAGE_TYPE`. */
const READY_MESSAGE_TYPE = 'kirocrew-dashboard:ready'

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

interface Loaded {
  html: string
  manifest: DashboardManifest
  instanceVersion: number
  templateId: string
  templateVersion: number
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
 */
export default function CrewDynamicDashboard({ slug, member, displayName }: {
  slug: string
  member: string
  displayName: string
}) {
  const { theme, colorTheme, themeVersion } = useTheme()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['member-dashboard', slug, member],
    queryFn: () => api.memberDashboard(slug, member),
    enabled: Boolean(slug) && Boolean(member),
  })

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
    if (!data || !data.html) return
    // STATE `error` NEVER becomes a page. The read answers 200 for it and `wire()`
    // always carries `html`, but the gateway deliberately does not compose that state
    // -- `_render` is skipped, so there is no data island, no bootstrap and no ready
    // beacon. Promoting it would draw the template's own placeholder markup as a
    // healthy dashboard: every cell empty, nothing marked missing, no band, and no
    // sign to a reader that what they are looking at is not their data.
    if (data.state === 'error') return
    const next: Loaded = {
      html: data.rendered_html || data.html,
      manifest: data.manifest,
      instanceVersion: data.instance_version,
      templateId: data.template?.id ?? '',
      templateVersion: data.template?.version ?? 0,
    }
    // Keyed on the INSTANCE VERSION, not the html: contract v3 says every change
    // bumps it, so it is the server's own statement that this is a different page,
    // and a different page is what probation exists for.
    setCandidate(prev => {
      if (!prev) return next
      if (prev.instanceVersion !== next.instanceVersion) return next
      return prev.html === next.html ? prev : next
    })
    // A re-render of the SAME version is new VALUES, not a new page -- which is most
    // of what a refetch brings, and all of what an unadopted crewmate ever gets,
    // since the default instance sits at version 0 forever. Swapped in place with no
    // probation: the page on screen has already proved it loads, so withholding its
    // own fresher numbers behind a readiness beacon would freeze the tab on the
    // values it happened to open with.
    setGood(prev => {
      if (!prev || prev.instanceVersion !== next.instanceVersion) return prev
      return prev.html === next.html ? prev : next
    })
  }, [data])

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
  const awaitingFirstPage = Boolean(data?.html) && !good && !broken
  const srcdoc = useMemo(
    () =>
      shown && !isError
        // `rewriteBareLinks: false`: the sandbox withholds `allow-popups`, so a
        // `target="_blank"` link here would be a blocked popup (a dead click).
        ? buildSrcdoc({ html: shown.html, themeVars, mode: theme, rewriteBareLinks: false })
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
  const probating = Boolean(candidate && good && candidate.instanceVersion !== good.instanceVersion)
  const timer = useRef<number | null>(null)

  // A new page must actually be MOUNTED to get the chance to beacon, so the
  // probating candidate is what the srcdoc is built from while it is on probation.
  const probeSrcdoc = useMemo(
    () =>
      probating && candidate && !isError
        ? buildSrcdoc({ html: candidate.html, themeVars, mode: theme, rewriteBareLinks: false })
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

  if (isLoading || awaitingFirstPage) {
    return (
      <div className="p-4 space-y-1.5" data-testid="crew-dashboard-loading" aria-hidden>
        <div className="h-3 rounded bg-bg-hover animate-pulse" />
        <div className="h-3 w-2/3 rounded bg-bg-hover animate-pulse" />
      </div>
    )
  }

  if (isError) {
    return (
      <div className="p-4 space-y-1.5">
        {/* No agent hand-off: this frame is the Members page's Dashboard tab, and
            the hand-off navigates to /chat, unmounting the page's unsaved Profile
            and crew-editor drafts without asking its leave guard. */}
        <ErrorNotice message={i18nT('pages.membersPage.dashboard_render_error')} testId="crew-dashboard-error" />
        <Btn onClick={() => void refetch()} data-testid="crew-dashboard-error-retry">
          <RotateCw className="lucide-inline" aria-hidden />
          {i18nT('pages.membersPage.webview_retry')}
        </Btn>
      </div>
    )
  }

  if (broken && !shown) {
    return (
      // Nothing good to keep, so the frame is not drawn at all rather than drawn from
      // markup the gateway refused to fill.
      <div className="p-4 space-y-1.5">
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
      // Reached only when the gateway could not resolve a page at all -- the read
      // injects the default template for a crewmate that adopted nothing, so an
      // empty body here means the registry did not load. The copy therefore names
      // THAT, not publishing: a reader told to publish would watch the crewmate
      // confirm it published and still see nothing, because this tab renders the
      // dashboard and no longer renders a published document.
      <div className="p-4 space-y-1.5">
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
          src={url}
          sandbox={CREW_DASHBOARD_SANDBOX}
          className="flex-1 min-h-0 w-full border-none bg-bg"
          style={{ colorScheme: theme }}
          title={i18nT('pages.membersPage.dashboard_frame_title', { crew: displayName })}
          data-testid="crew-dashboard-iframe"
          data-instance-version={shown.instanceVersion}
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

export { DATA_MESSAGE_TYPE, READY_MESSAGE_TYPE, READY_TIMEOUT_MS }
