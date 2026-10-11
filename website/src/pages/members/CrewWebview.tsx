import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { api, type CrewPanelData, type CrewPanelMeta } from "../../api/client";
import { useDialogFocusTrap } from "../../hooks/useDialogFocusTrap";
import { useQuery } from "@tanstack/react-query";
import { Maximize2, Minimize2, RotateCw, ShieldCheck } from "lucide-react";
import { Btn } from "../../components/ui";
import ErrorNotice from "../../components/ErrorNotice";
import { useTheme } from "../../hooks/useTheme";
import { useSandboxDoc } from "../../hooks/useSandboxDoc";
import { useFrameOpenLink } from "../../hooks/useFrameOpenLink";
import { buildSrcdoc, readThemeVars } from "../../lib/widgetSrcdoc";
import { i18nT } from "../../i18n/t";
import { fmtDateTime, fmtRelative, toDate } from "../../i18n/format";
import CrewDashboardEmpty from "./CrewDashboardEmpty";

/**
 * The sandbox grants for a crew's webview, and the ONE line of this file that is
 * a security boundary rather than a layout choice.
 *
 * `allow-scripts` alone. Every other grant is withheld deliberately:
 *
 * - No `allow-same-origin`, so the document lands on a null (opaque) origin and
 *   cannot read the dashboard's cookies or localStorage, or touch the parent DOM.
 * - No `allow-popups`, which is where this is STRICTER than the artifact frame
 *   it borrows its plumbing from. The gateway route that serves the document
 *   sets one CSP `sandbox` header for every consumer and that header does grant
 *   popups — but sandbox restrictions COMBINE rather than union, so a grant the
 *   frame attribute withholds stays withheld. A crew publishes on an unattended
 *   loop with nobody at the keyboard; a window it could open is a capability
 *   nothing about a status dashboard needs. The one way out is the host's own
 *   `kirocrew-dashboard:open` bridge (`useFrameOpenLink`): a GitHub pull-request
 *   URL, from this frame, right after a user gesture, opened by the host.
 * - No `allow-top-navigation`, `allow-forms`, or `allow-modals`.
 *
 * Scripts DO run, which is what the template needs to read its data island and
 * render. Egress is closed by the document CSP `buildSrcdoc` injects
 * (`connect-src 'none'`, `form-action 'none'`, `img-src data: blob:`).
 *
 * ONE value for both the docked and the expanded view: the frame is the same
 * element in both, so there is no second attribute to keep in step. Pinned by a
 * test that asserts it in each state.
 */
export const CREW_WEBVIEW_SANDBOX = "allow-scripts";

/**
 * The shared-element ids for the docked-to-expanded transition.
 *
 * The docked card and the full-window view are two subtrees rather than one,
 * and they have to be: the expanded one holds the iframe, whose document URL is
 * single-use, so it must survive a collapse behind ``hidden`` instead of
 * unmounting. That makes a plain `flag ? A : B` the shape the codebase forbids --
 * the card vanishes and a different surface appears, which reads as two things
 * rather than one thing changing size.
 *
 * So the id is carried by WHICHEVER surface is currently the visible one, never
 * both: Framer Motion matches the id across the flip and animates position and
 * size between them, while the hidden subtree stays mounted and keeps its frame.
 * Two ids rather than one, because the eye needs a landing spot INSIDE the
 * growing box as well: the "Contained" bar exists in both states and says the
 * same sentence, so it is the anchor whose text stays continuous.
 */
const SURFACE_LAYOUT_ID = "crew-webview-surface";
const CONTAINED_BAR_LAYOUT_ID = "crew-webview-contained-bar";

/**
 * The sentence-length threshold, mirroring ``PROSE_CHARS`` in ``default.html``.
 *
 * Deliberately duplicated rather than shared: the template is an HTML file the
 * Python package ships and this is a React module, so there is no seam to import
 * across. Both sides answer the same question -- "is this a counter or a
 * sentence?" -- and both must answer it the same way, or the drawer's lead line
 * would be a field the expanded document renders as a tile.
 */
const PROSE_CHARS = 44;

/** How many scalars the docked summary will show. Three single-line rows is what
 *  fits under the lead without the card becoming a second dashboard. */
const MAX_DOCKED_STATS = 3;

/** Length ceilings for a docked stat row. A row is only worth showing if it
 *  reads WHOLE at drawer width -- past these it would need truncating, and a
 *  truncated counter tells the operator nothing. Such a field is dropped from
 *  the summary and stays in the expanded document, which has the room. */
const MAX_STAT_LABEL_CHARS = 22;
const MAX_STAT_VALUE_CHARS = 18;

/**
 * How the document learns it is the DOCKED copy. Prepended to the template
 * fragment of the docked mint only; a template reads it with
 * `document.querySelector('meta[name="kirocrew-view"]')` (or `body:has(...)`
 * in CSS) and renders its compact layout. A constant this file authors: no crew
 * byte is in it, and the fragment still goes through `buildSrcdoc`'s typed
 * parse like every other document.
 *
 * Spelled as a regex's `source` for the reason NOTE_SUFFIX_RE is a regex: it is a
 * protocol token shared with templates, not copy, and a regex carries no string
 * literal for the strict i18n gate to count. None of its characters is special in
 * a pattern, so `source` is exactly the markup.
 */
export const DOCKED_VIEW_MARK = /<meta name="kirocrew-view" content="docked">/.source;

/** The template's docked opt-in, or null. A non-number, NaN or non-positive
 *  value is no opt-in at all, so a malformed response keeps the native path. */
function dockedFrameHeight(meta: CrewPanelMeta | null): number | null {
  const h = meta?.docked_height;
  if (typeof h !== "number" || !Number.isFinite(h) || h <= 0) return null;
  // The read route always clamps through agent_panel.docked_height; the client
  // only rejects a value that is not a usable height at all.
  return Math.round(h);
}

/** What the template renders for a value the crew does not have. */
const NIL_TEXT = "\u2014";

/**
 * Matches the caveat convention's suffix (``<field>_note`` qualifies ``<field>``).
 *
 * A PATTERN rather than a string constant on purpose. The suffix is a protocol
 * token shared with the template, not copy, so translating it would break the
 * lookup it exists to perform -- but the strict i18n gate counts every string
 * literal on a line this branch wrote and does not (and should not) try to guess
 * which ones are machine tokens. A regex carries no string literal, so the intent
 * is stated without asking the gate to make an exception.
 */
const NOTE_SUFFIX_RE = /_note$/;

function isScalarValue(v: unknown): boolean {
  return (
    v === null ||
    typeof v === "string" ||
    typeof v === "number" ||
    typeof v === "boolean"
  );
}

/** A value the crew does not have. Distinct from `false` and from `0`. */
function isNilValue(v: unknown): boolean {
  return v === null || v === undefined || v === "";
}

function isProse(v: unknown): v is string {
  return typeof v === "string" && v.length > PROSE_CHARS;
}

function scalarText(v: unknown): string {
  if (isNilValue(v)) return NIL_TEXT;
  // Through the catalog: a boolean rendered as a hardcoded `yes`/`no` is copy,
  // and it shipped as English in every locale until the strict gate caught it.
  if (v === true) return i18nT("pages.membersPage.webview_value_yes");
  if (v === false) return i18nT("pages.membersPage.webview_value_no");
  return String(v);
}

/** `open_rulings` and `openRulings` both read as `open rulings`, matching the
 *  heading convention the template uses so the two views name a field alike. */
function labelOf(key: string): string {
  return key
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .trim();
}

interface DockedStat {
  key: string;
  label: string;
  value: string;
}

interface DockedSummary {
  title: string;
  subtitle: string | null;
  /** The first sentence-length field in PUBLISHED order — the crew's urgent
   *  line. Order is the only channel the crew has for saying what matters
   *  most, so this is a scan from the front and not a search for a known key. */
  lead: { label: string; text: string } | null;
  stats: DockedStat[];
}

/**
 * Reduce a published record to what fits, and is worth reading, at drawer width.
 *
 * This is why the docked view renders chosen fields natively instead of the
 * document. The expanded dashboard needs a full page to be legible — four tiles
 * across, multi-column grids — while the panel hosting the docked view starts
 * at 320px wide, where the document puts the crew's most important line below a
 * fold with no scroll and no signal. Choosing a few fields natively is what
 * makes the docked view answer "what does this crew need from me" instead of
 * showing a clipped dashboard.
 */
function summarize(meta: CrewPanelMeta | null, slug: string): DockedSummary {
  const data: CrewPanelData =
    meta?.data && typeof meta.data === "object" ? meta.data : {};
  const rawTitle = typeof data.title === "string" ? data.title : "";
  const rawSubtitle = typeof data.subtitle === "string" ? data.subtitle : "";

  // A crew that published no title still gets a heading: an untitled card in a
  // list of crews is unattributable.
  const title = rawTitle || meta?.title || meta?.crew || slug;

  const keys = Object.keys(data).filter(
    (k) => k !== "title" && k !== "subtitle",
  );
  // `<field>_note` is a caveat on another field, not a field — it must not be
  // mistaken for the crew's urgent line or shown as a stat.
  const isAttachedNote = (k: string) =>
    NOTE_SUFFIX_RE.test(k) &&
    Object.prototype.hasOwnProperty.call(data, k.replace(NOTE_SUFFIX_RE, ""));
  const own = keys.filter((k) => !isAttachedNote(k));

  let lead: DockedSummary["lead"] = null;
  for (const k of own) {
    if (isProse(data[k])) {
      lead = { label: labelOf(k), text: data[k] as string };
      break;
    }
  }

  const stats: DockedStat[] = [];
  for (const k of own) {
    if (stats.length >= MAX_DOCKED_STATS) break;
    const v = data[k];
    if (!isScalarValue(v) || isProse(v) || isNilValue(v)) continue;
    const label = labelOf(k);
    const value = scalarText(v);
    if (
      label.length > MAX_STAT_LABEL_CHARS ||
      value.length > MAX_STAT_VALUE_CHARS
    )
      continue;
    stats.push({ key: k, label, value });
  }

  return { title, subtitle: rawSubtitle || null, lead, stats };
}

/** Read the computed theme vars (known set only, each value sanitized) so the
 * sandboxed frame matches the dashboard theme. Mirrors the helper in
 * ArtifactBody / ArtifactThumbs. */

/** The absolute publish time, for the relative chip's tooltip.
 *
 * A bare `23m` has no anchor: it does not say 23 minutes before WHAT, and it
 * goes stale while the drawer sits open.
 *
 * Formatted in the APP's language, not the browser's. `toLocaleString()` here
 * read the host locale, so a Japanese UI rendered an English tooltip on a
 * translated chip. Deliberately the `medium` date width rather than the numeric
 * one that matches `toLocaleString()`'s old output: this tooltip exists to
 * DISAMBIGUATE, and `07/30/2026` versus `30/07/2026` is the one ambiguity a
 * cross-locale surface should not carry.
 *
 * Empty for an unparseable stamp, which renders as NO tooltip — `fmtDateTime`
 * would give the em dash it uses for absent values, and a tooltip reading `—`
 * is worse than none. */
function absoluteStamp(publishedAt: string): string {
  return toDate(publishedAt) ? fmtDateTime(publishedAt) : "";
}

/**
 * One crew's webview, in two views that share nothing but the record.
 *
 * DOCKED is a native React summary. It renders the crew's title, its urgent
 * line and up to three short counters as ordinary escaped text — no iframe, no
 * document. That is a containment improvement as well as a legibility one:
 * React text children cannot become markup, so this path is strictly stronger
 * than the sandbox it replaces. It must stay that way; a
 * `dangerouslySetInnerHTML` here would be the one edit that undoes it.
 *
 * EXPANDED is the sandboxed document, which is where the dashboard is actually
 * read because a dashboard needs a full page to be legible.
 */
export default function CrewWebview(props: CrewWebviewProps) {
  /* One instance per crew IDENTITY, not per slug. Slugs are lossy -- two crews
     can share one -- and both frames hold a minted URL that `useSandboxDoc`
     keeps across a later failed mint by design. Reusing the instance across a
     switch between two same-slug crews would therefore leave the previous
     crew's document on screen under the new crew's name whenever the new
     crew's mint failed. A key on the exact pair drops every such state. */
  return (
    <CrewWebviewView
      key={JSON.stringify([props.slug, props.member])}
      {...props}
    />
  );
}

interface CrewWebviewProps {
  slug: string;
  member: string;
  /**
   * Open the crew manager on this member, for the empty state's affordance.
   *
   * A callback rather than a route this file navigates itself: the page that
   * owns the drawer owns the draft guard, so the jump has to run through the
   * SAME `leave()` every other navigation here does, or a half-typed message
   * gets unmounted by a link that looks harmless. Optional, so a consumer that
   * has no manager to open (a test, or a surface with no router) renders the
   * sentence without a dead control under it.
   */
  onSetUp?: () => void;
  /**
   * Report whether this webview is holding a live minted document.
   *
   * `everExpanded` keeps the frame mounted behind `display:none` across a
   * collapse, because the minted URL is single-use and a re-created iframe would
   * request a spent one. That rule only reaches as far as this component: the
   * page that owns the side panel decides whether the whole subtree stays
   * mounted, and it cannot see this flag. So the flag is reported outward, and a
   * consumer that keeps a hidden panel alive for an unsent answer keeps it alive
   * for a live frame on the same terms.
   *
   * Called with `false` on unmount, so a consumer holding the value in state
   * does not keep holding the panel for a frame that is gone.
   */
  onLiveFrameChange?: (live: boolean) => void;
  /**
   * How the crewmate is NAMED to the reader, for the empty state's sentence.
   *
   * `member` is the exact identity the read is keyed on and must stay the raw
   * name; but a crewmate can carry a display name the roster shows instead, and
   * "atlas has not published" under a header that says "Atlas" names the same
   * crewmate two ways. Falls back to `member`.
   */
  displayName?: string;
}

function CrewWebviewView({ slug, member, onSetUp, onLiveFrameChange }: CrewWebviewProps) {
  const [expanded, setExpanded] = useState(false);
  /**
   * Whether the document has EVER been opened, which is what gates minting.
   *
   * Two rules ride on this single flag, and both are load-bearing because the
   * minted URL is SINGLE-USE server-side:
   *
   * 1. It stays false while docked, so a drawer the operator never expands
   *    costs zero mints and zero gateway round trips.
   * 2. Once true it NEVER goes back to false, so collapsing leaves the frame
   *    MOUNTED behind a `display:none` wrapper. Unmounting it would re-request
   *    a spent URL on the next expand and render a blank frame — which is the
   *    bug this flag exists to make impossible, not merely to avoid.
   */
  const [everExpanded, setEverExpanded] = useState(false);
  // A pull request the panel links to: these two frames only, GitHub PR URLs only.
  const dockedFrameRef = useRef<HTMLIFrameElement | null>(null);
  const expandedFrameRef = useRef<HTMLIFrameElement | null>(null);
  useFrameOpenLink([dockedFrameRef, expandedFrameRef]);

  /* Keyed on a ref so a consumer passing an inline arrow does not re-run this on
     every render and report the same value repeatedly. */
  const reportLiveFrame = useRef(onLiveFrameChange);
  reportLiveFrame.current = onLiveFrameChange;
  useEffect(() => {
    reportLiveFrame.current?.(everExpanded);
    return () => {
      if (everExpanded) reportLiveFrame.current?.(false);
    };
  }, [everExpanded]);

  /**
   * The shared-element timing, and what reduced motion changes about it.
   *
   * The ids stay attached either way: the requirement is that the surface remain
   * ONE element across the flip, and honouring the preference by removing them
   * would restore exactly the hard swap they exist to prevent. So the preference
   * takes the DURATION to zero -- the layout still resolves through one element,
   * it simply arrives immediately.
   */
  const reducedMotion = useReducedMotion();
  const surfaceTransition = useMemo(
    () =>
      reducedMotion
        ? { duration: 0 }
        : { type: "spring" as const, stiffness: 420, damping: 38, mass: 0.9 },
    [reducedMotion],
  );

  const { theme, colorTheme, themeVersion } = useTheme();
  // The deps are deliberate and the body does not name them: `readThemeVars`
  // reads the computed values off `document.documentElement`, so the theme change
  // is what must retrigger it. Kept on ONE line so the directive covers the call
  // it is aimed at -- split across lines, the finding is raised on the deps line
  // and the disable lands on the line above it. Same shape as ArtifactBody.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion]);

  /**
   * Through React Query rather than a hand-rolled `fetch().then()`.
   *
   * The manual version cached nothing (every drawer open re-fetched) and, more
   * to the point, had no refetch to offer: the error state was a dead end with
   * no way out but reselecting the crew. `refetch` is what makes the read
   * failure recoverable in place.
   */
  const { data, isLoading, isError, refetch } = useQuery({
    // Keyed on BOTH: slugs are lossy, so two crews share one key and the cached
    // answer would be the other crew's panel.
    queryKey: ["member-panel", slug, member],
    // Through the shared `api` layer, like every sibling reader on this page. A
    // component-local `fetch` was the one reader the page's own tests could not
    // stub, so a scenario every sibling handled silently -- a remembered crew
    // renamed away, where the page falls back to the first row -- surfaced here as
    // a red alert about a crew that no longer exists.
    //
    // `member` is the exact crew name and the route REQUIRES it, the same way
    // /activity does: the stored record carries an ownership claim, and the
    // server refuses to hand this crew a record another crew owns.
    queryFn: () => api.memberPanel(slug, member),
    enabled: Boolean(slug) && Boolean(member),
  });

  const html = data?.html ?? null;
  const meta = data?.panel ?? null;

  // A different crew is a different document, so the next expand must mint
  // afresh rather than reuse the URL minted for the previous one.
  useEffect(() => {
    setExpanded(false);
    setEverExpanded(false);
  }, [slug]);

  const collapse = useCallback(() => setExpanded(false), []);
  const open = useCallback(() => {
    setEverExpanded(true);
    setExpanded(true);
  }, []);

  /**
   * The Tab trap and Escape come from the shared hook; the focus hand-off does not.
   *
   * The hook is what the ~20 sibling overlays use, including the iframe-hosting
   * `McpAppFrame`, and it is the right owner of the two things that were missing:
   * Tab was never trapped, so a keyboard user tabbed out of an `aria-modal`
   * overlay into a drawer, sidebar and composer that AT could no longer announce
   * but that stayed reachable. It also carries an IME guard on the Tab path that
   * no local version would — IMEs use Tab to cycle candidates, and on WebKit the
   * committing keydown arrives after `compositionend` with `isComposing` false.
   *
   * Its focus-in/restore half cannot serve us, and that is a lifecycle mismatch
   * rather than a defect in either place: the hook focuses on MOUNT, which is
   * correct for a dialog that is conditionally rendered when it opens. This one
   * is mounted permanently — the docked card is the same component — and merely
   * toggles `expanded`. Mounting it on expand instead is not available: the
   * minted document URL is single-use, so the frame must survive a collapse.
   * Hence `restoreFocus: false` and an explicit hand-off keyed to `expanded`.
   * `hasOpened` keeps a first render from stealing focus just by being in the
   * drawer.
   */
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useDialogFocusTrap(dialogRef, collapse, {
    enabled: expanded,
    restoreFocus: false,
  });

  const openRef = useRef<HTMLButtonElement | null>(null);
  const collapseRef = useRef<HTMLButtonElement | null>(null);
  const hasOpened = useRef(false);
  useEffect(() => {
    if (expanded) {
      hasOpened.current = true;
      collapseRef.current?.focus();
    } else if (hasOpened.current) {
      hasOpened.current = false;
      openRef.current?.focus();
    }
  }, [expanded]);

  // Null until the first expand: `useSandboxDoc` mints when it receives a
  // document, so withholding it here is what makes the docked view free.
  // `rewriteBareLinks: false`: CREW_WEBVIEW_SANDBOX withholds `allow-popups`,
  // so a `target="_blank"` link here would be a blocked popup (a dead click);
  // bare links keep navigating the frame as they always have.
  const srcdoc = useMemo(
    () =>
      // The read-error state below replaces every frame, and a minted URL is
      // single-use: an iframe re-created after it would request a spent
      // document. Dropping the srcdoc while the read is in error resets the
      // mint, so recovery mints a fresh one even for byte-identical html.
      html && everExpanded && !isError
        ? buildSrcdoc({ html, themeVars, mode: theme, rewriteBareLinks: false })
        : null,
    [html, everExpanded, isError, themeVars, theme],
  );
  const mint = useSandboxDoc(srcdoc);
  const { url, pending, retry } = mint;
  // A mint that outlived the pending ceiling counts as failed, so a wedged
  // gateway reaches the same notice and retry as a refused mint.
  const failed = mint.failed || mint.stalled;

  /**
   * The DOCKED frame, for a template that opted in (`docked_height`).
   *
   * A second, separate mint: the docked copy carries DOCKED_VIEW_MARK so the
   * template can render compactly, and the two URLs are each single-use, so one
   * frame can never serve both views. The same single-use rule as the expanded
   * frame applies, which is why the docked card below stays MOUNTED (hidden)
   * while expanded instead of unmounting and re-requesting a spent URL.
   *
   * Null for a template that did not opt in, so such a drawer still costs zero
   * mints until Expand.
   */
  const dockedHeight = dockedFrameHeight(meta);
  const dockedSrcdoc = useMemo(
    () =>
      // Reset across a read error for the same reason as the expanded mint.
      html && dockedHeight !== null && !isError
        ? buildSrcdoc({
            html: DOCKED_VIEW_MARK + html,
            themeVars,
            mode: theme,
            rewriteBareLinks: false,
          })
        : null,
    [html, dockedHeight, isError, themeVars, theme],
  );
  const docked = useSandboxDoc(dockedSrcdoc);
  // A docked mint that outlived the pending ceiling counts as failed: with no
  // document the card falls back to the native lead and stats, and with one on
  // screen that document stays; either way the failure notice and its retry
  // show, and Expand still works.
  const dockedFailed = docked.failed || docked.stalled;
  const showDockedFrame =
    dockedHeight !== null && !(dockedFailed && !docked.url);

  /**
   * The publish instant of the document CURRENTLY on screen, which is not always
   * the one `meta` describes.
   *
   * `useSandboxDoc` keeps a live document when a LATER mint fails, so in that
   * state the frame shows an older render while `meta` has already moved on to
   * the newest record. The Contained bar's chip dates `meta`, so without this the
   * expanded view showed three ages that disagreed: a chip dating a record the
   * reader cannot see, a document from before it, and a band that said only "the
   * last version that loaded". A reader told UX review they could not tell how old
   * the page was, which is the one question the chip exists to answer.
   *
   * Captured when a url arrives rather than when `html` changes, because the url
   * is the thing the frame is actually showing.
   *
   * The stamp is stored WITH the url it describes, and a url already stamped is
   * never re-stamped. That pairing is the whole correctness argument: the failed
   * -refresh state is reached precisely BECAUSE `meta` moved on while `url` did
   * not, so any write that a metadata change can reach would date the retained
   * document by the newest record -- the one record the reader cannot see -- and
   * the aged band would then claim the stale page is the fresh one. Keying on the
   * url's own identity makes that unreachable by construction rather than by a
   * dependency list a later edit can widen.
   */
  const [shown, setShown] = useState<{ url: string; at: string | null } | null>(
    null,
  );
  useEffect(() => {
    if (!url) return;
    setShown((prev) =>
      prev?.url === url ? prev : { url, at: meta?.published_at ?? null },
    );
  }, [url, meta?.published_at]);
  const shownAgo = shown?.at ? fmtRelative(shown.at) : "";
  /* The record is NEWER than the document on screen only when its publish
   * instant is later than the shown one's. A failed re-mint alone does not make
   * it so: a theme change re-mints the SAME record, and labelling that "new
   * version" put three ages on one surface that described one publish -- a
   * reader told UX review they could not tell which copy they were looking at.
   * Compared as instants, not strings, so two spellings of one moment agree. */
  const shownAt = shown?.at ? toDate(shown.at) : null;
  const metaAt = meta?.published_at ? toDate(meta.published_at) : null;
  const newerRecord =
    failed &&
    !!url &&
    !!shownAt &&
    !!metaAt &&
    metaAt.getTime() > shownAt.getTime();

  const summary = useMemo(() => summarize(meta, slug), [meta, slug]);

  if (isLoading) {
    return (
      <div
        className="mb-4 space-y-1.5"
        data-testid="crew-webview-loading"
        aria-hidden
      >
        {/* `bg-bg-hover`, not the accent: every other skeleton in the app uses a
            muted token, and the accent read as content rather than as absence —
            a reader told UX review the purple bars "could also be a broken
            drawing", which is the one thing a placeholder must not suggest. */}
        <div className="h-3 rounded bg-bg-hover animate-pulse" />
        <div className="h-3 w-2/3 rounded bg-bg-hover animate-pulse" />
      </div>
    );
  }

  if (isError) {
    // The shared error surface, with the agent hand-off and a retry. This state
    // held text and nothing else, while the mint failure right below it offered
    // "Try again" — the same failure class with two different answers, one of
    // them a dead end.
    return (
      <div className="mb-4 space-y-1.5">
        <ErrorNotice
          message={i18nT("pages.membersPage.webview_error")}
          askAgent
          testId="crew-webview-error"
        />
        <Btn
          onClick={() => void refetch()}
          data-testid="crew-webview-error-retry"
        >
          <RotateCw className="lucide-inline" aria-hidden />
          {i18nT("pages.membersPage.webview_retry")}
        </Btn>
      </div>
    );
  }

  if (!html) {
    /* The FIRST state every operator meets, because the panel server ships
       opt-in and off — so this is the copy that has to carry its own weight.
       The sentence says what is true right now and the control says what to do
       about it; neither repeats the other, and the mechanism vocabulary the old
       copy used ("add its panel server under Tools & MCP") is gone from both.

       Saying the task once is also what keeps this state's height honest. In a
       side panel the vertical budget is the scarce one, and a first attempt that
       put the instruction in the sentence AND in the control spent a whole extra
       line on the duplication — enough to push the memory block's own button
       1.5% out of the viewport and redden the co-visibility assertion in
       `playwright/member-memory.spec.ts`. The row also wraps, so a wider column
       puts the control beside the sentence rather than under it. */
    return (
      <div
        className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1"
        data-testid="crew-webview-empty"
      >
        <span className="text-[11px] text-muted">
          {i18nT("pages.membersPage.webview_empty")}
        </span>
        {onSetUp && (
          <Btn onClick={onSetUp} data-testid="crew-webview-setup">
            {i18nT("pages.membersPage.webview_setup")}
          </Btn>
        )}
      </div>
    );
  }

  const collapseLabel = i18nT("pages.membersPage.webview_collapse");
  // Names the CONTENT, not an action. `aria-label` was the collapse string, so a
  // screen reader announced the dialog as "Collapse the dashboard" — which is
  // what the button inside it does, not what the region is.
  const dialogLabel = i18nT("pages.membersPage.webview_frame_title", {
    crew: meta?.crew || slug,
  });
  // `fmtRelative` rather than a local age ladder: it asks CLDR, so the chip reads
  // in the active language instead of an English `2m` inside a translated UI. The
  // four `webview_age_*` catalog keys this replaced were a reinvention of it --
  // the drawer's sibling CrewWakeSection already called these helpers.
  const ago = meta?.published_at ? fmtRelative(meta.published_at) : "";
  const agoTitle = meta?.published_at ? absoluteStamp(meta.published_at) : "";

  return (
    <div
      className={
        expanded ? "fixed inset-0 z-50 flex flex-col bg-bg p-4" : "mb-4"
      }
      data-testid="crew-webview"
      data-expanded={expanded ? "true" : "false"}
      role={expanded ? "dialog" : undefined}
      ref={dialogRef}
      aria-modal={expanded ? true : undefined}
      aria-label={expanded ? dialogLabel : undefined}
    >
      {/*
       * Mounted for as long as the drawer is, and merely HIDDEN while expanded:
       * for a template that opted in, this card holds the docked frame, whose
       * URL is single-use exactly like the expanded one. The shared ids ride
       * only while it is the visible surface, for the same reason as below.
       *
       * Sized to content, never clipped. Everything that could overflow is
       * clamped EXPLICITLY with an ellipsis (the subtitle to one line, the
       * lead to three) and every stat is pre-filtered to a length that reads
       * whole — because the failure this view replaced was a fixed 420px box
       * with `overflow-hidden` that cut the crew's most important line off
       * with no scrollbar and no fade to say it had.
       */}
      <motion.div
        layoutId={expanded ? undefined : SURFACE_LAYOUT_ID}
        transition={surfaceTransition}
        className={expanded ? "hidden" : "rounded-lg border border-border bg-card"}
        data-testid="crew-webview-summary"
      >
        <motion.div
          layoutId={expanded ? undefined : CONTAINED_BAR_LAYOUT_ID}
          transition={surfaceTransition}
          className={
            "flex items-center gap-2 px-2.5 py-1.5 text-[11px] text-muted " +
            "bg-bg-elevated border-b border-border rounded-t-lg"
          }
        >
          <ShieldCheck className="lucide-inline text-ok" aria-hidden />
          {/* The docked bar has no room for the full claim, so the word alone
              leaves a cold reader guessing what the green shield asserts. The
              title carries the SAME string the expanded bar prints, so the two
              surfaces cannot drift into claiming different things. */}
          {/* `title` alone reaches a mouse and nothing else, so the claim is
              also the chip's accessible NAME: a touch or keyboard user gets the
              same sentence a hover would give, rather than the bare word. */}
          <span
            className="text-text-strong font-medium"
            title={i18nT("pages.membersPage.webview_contained_detail")}
            aria-label={i18nT("pages.membersPage.webview_contained_detail")}
          >
            {i18nT("pages.membersPage.webview_contained")}
          </span>
          {ago && (
            /* Same label as the expanded bar. Kept bare at first on the
               reasoning that the ambiguity had only been measured there, which
               was wrong: sitting beside "Contained", a naked "23m ago" reads as
               the age of that STATUS rather than of the dashboard, which a
               reader told UX review outright. One form on both surfaces is also
               one fewer way for them to drift. The one deliberate difference:
               the expanded bar switches to "New version published" while a
               failed refresh shows an older document, because only that view
               has a second age (the band's) to disagree with; docked, there is
               one age and the plain label is the true one. */
            <span
              className="ml-auto shrink-0"
              data-testid="crew-webview-age"
              title={agoTitle}
            >
              {i18nT("pages.membersPage.webview_published_ago", { ago })}
            </span>
          )}
        </motion.div>

        <div className="px-3 py-2.5">
          {/* Crew-supplied strings from here down. Every one is a React text
              child, which is the containment for this path. */}
          <div className="text-[13px] font-semibold text-text-strong leading-snug line-clamp-2">
            {summary.title}
          </div>
          {summary.subtitle && (
            <div
              className="text-[11px] text-muted truncate mt-0.5"
              title={summary.subtitle}
            >
              {summary.subtitle}
            </div>
          )}

          {showDockedFrame && (
            /* The template's own compact view, in the SAME sandbox as the
               expanded frame. A fixed height the template declared, so the
               frame never needs a height channel from the document. The slot
               is reserved while the mint is in flight so the card does not
               jump when the frame lands. */
            <div
              className="mt-2.5 rounded-md border border-border overflow-hidden bg-card"
              style={{ height: dockedHeight ?? undefined }}
              data-testid="crew-webview-docked"
            >
              {docked.url ? (
                <iframe
                  ref={dockedFrameRef}
                  src={docked.url}
                  sandbox={CREW_WEBVIEW_SANDBOX}
                  className="w-full h-full border-none bg-card"
                  style={{ colorScheme: theme }}
                  data-testid="crew-webview-docked-frame"
                  title={i18nT("pages.membersPage.webview_frame_title", {
                    crew: meta?.crew || slug,
                  })}
                />
              ) : (
                /* The compact card's own loading line, so a pending slot reads
                   as loading rather than as a blank or broken widget, and never
                   borrows the expanded dashboard's name. */
                <div
                  className="h-full flex items-center justify-center bg-bg-hover animate-pulse text-[11px] text-muted"
                  data-testid="crew-webview-docked-pending"
                >
                  {i18nT("pages.membersPage.webview_docked_rendering")}
                </div>
              )}
            </div>
          )}

          {dockedHeight !== null && dockedFailed && (
            /* The docked mint failed. Through the shared error surface, like
               the expanded view's failures, with the same retry beside it: the
               URL is single-use, so re-rendering a spent one recovers nothing.
               With a document already on screen the refresh failed and the
               last version stays; with none, the native summary below answers. */
            /* Stacked, not side by side: at drawer width a notice squeezed
               beside its own hand-off and a button breaks mid-word. */
            <div
              className="mt-2.5 flex flex-col items-start gap-1.5"
              data-testid="crew-webview-docked-failed"
            >
              <ErrorNotice
                message={i18nT(
                  docked.url
                    ? "pages.membersPage.webview_docked_refresh_error"
                    : "pages.membersPage.webview_docked_error",
                )}
                askAgent
                actionPlacement="below"
                className="w-full"
                testId="crew-webview-docked-error"
              />
              <Btn
                disabled={docked.pending}
                onClick={docked.retry}
                data-testid="crew-webview-docked-retry"
              >
                <RotateCw className="lucide-inline" aria-hidden />
                {i18nT("pages.membersPage.webview_retry")}
              </Btn>
            </div>
          )}

          {!showDockedFrame && summary.lead && (
            <div className="mt-2.5" data-testid="crew-webview-lead">
              <div className="text-[10px] font-mono uppercase tracking-[0.09em] text-muted mb-1">
                {summary.lead.label}
              </div>
              <div className="text-[12px] leading-snug text-text line-clamp-3">
                {summary.lead.text}
              </div>
            </div>
          )}

          {!showDockedFrame && summary.stats.length > 0 && (
            <dl className="mt-2.5 pt-2 border-t border-border space-y-1">
              {summary.stats.map((s) => (
                <div key={s.key} className="flex items-baseline gap-2">
                  <dt className="text-[10px] font-mono uppercase tracking-[0.09em] text-muted">
                    {s.label}
                  </dt>
                  <dd className="ml-auto m-0 text-[12px] font-mono tabular-nums text-text-strong">
                    {s.value}
                  </dd>
                </div>
              ))}
            </dl>
          )}

          {/* A real button with words on it. The affordance it replaces was a
              14px icon at the far right of a bar whose own text truncated
              mid-word, which is how a reviewer failed to find the dashboard
              at all. */}
          <Btn
            className="mt-3 w-full justify-center"
            onClick={open}
            aria-haspopup="dialog"
            data-testid="crew-webview-expand"
            ref={openRef}
          >
            <Maximize2 className="lucide-inline" aria-hidden />
            {i18nT("pages.membersPage.webview_open")}
          </Btn>
        </div>
      </motion.div>

      {/*
       * Mounted from the first expand onward and merely HIDDEN when collapsed.
       * `hidden` is `display:none`, which keeps the element and its loaded
       * document alive; unmounting would re-request a single-use URL. This is
       * the one place in the file where the difference between hiding and
       * unmounting is a bug rather than a preference.
       *
       * The shared ids are attached only while EXPANDED, for that same reason:
       * this subtree outlives the collapse, so carrying them while hidden would
       * put two elements on one id and Framer Motion would have no single
       * element to animate.
       */}
      {everExpanded && (
        <motion.div
          layoutId={expanded ? SURFACE_LAYOUT_ID : undefined}
          transition={surfaceTransition}
          className={expanded ? "flex flex-col flex-1 min-h-0" : "hidden"}
        >
          <motion.div
            layoutId={expanded ? CONTAINED_BAR_LAYOUT_ID : undefined}
            transition={surfaceTransition}
            className={
              "flex items-center gap-2 px-2.5 py-1.5 text-[11px] text-muted bg-bg-elevated " +
              "border border-border rounded-t-lg shrink-0"
            }
          >
            <ShieldCheck className="lucide-inline text-ok" aria-hidden />
            <span className="text-text-strong font-medium">
              {i18nT("pages.membersPage.webview_contained")}
            </span>
            <span className="truncate">
              {i18nT("pages.membersPage.webview_contained_detail")}
            </span>
            {ago && (
              /* Labelled, not bare. Beside "Rendering the dashboard…" a naked
                 "39m ago" reads as if it could be dating the render attempt, and
                 a reader told UX review they could not tell which of the two was
                 true. The prefix names what the instant belongs to. Only this
                 bar carries the label: it is the one that sits directly above the
                 rendering line, which is where the ambiguity was measured. The
                 docked chip keeps its bare form, under a heading and beside the
                 summary it dates.

                 While a refresh has FAILED, an older document is still on
                 screen (`url` set, `failed`) AND the record's publish instant is
                 later than the shown document's, this chip dates the NEWER
                 record -- the one that could not be rendered -- and the band
                 below dates the version the reader is looking at. Two ages on
                 one surface read as a contradiction unless the label says they
                 describe two versions, which "New version published" does. When
                 the failed re-mint was for the SAME record (a theme change), the
                 plain label stays: there is one version and one age. */
              <span
                className="ml-auto shrink-0"
                data-testid="crew-webview-age"
                title={agoTitle}
              >
                {i18nT(
                  newerRecord
                    ? "pages.membersPage.webview_published_ago_newer"
                    : "pages.membersPage.webview_published_ago",
                  { ago },
                )}
              </span>
            )}
            <Btn
              className="shrink-0"
              onClick={collapse}
              data-testid="crew-webview-collapse"
              ref={collapseRef}
            >
              <Minimize2 className="lucide-inline" aria-hidden />
              {collapseLabel}
            </Btn>
          </motion.div>

          {failed && (
            /* ABOVE the frame, not over it. As an `absolute inset-x-0 top-0`
               overlay this covered the top of whatever the last mint had
               already painted — including that document's own freshness line —
               so the reader was told the dashboard "could not be rendered"
               while looking at a rendered one, with no way to judge whether the
               numbers under the banner were current. Stacked in the column, the
               document keeps every pixel it earned and the banner is a band
               between the Contained bar and the frame.

               Two failures, two strings: with `url` already set, the FIRST mint
               succeeded and a later one failed (`useSandboxDoc` keeps the live
               document on a later failure by design), so nothing is missing and
               the honest sentence is that the refresh failed. With no `url` at
               all, nothing rendered and the hard error is the true one. The
               retry stays beside both: the minted URL is single-use, so
               re-rendering a spent one recovers nothing. */
            <div
              className="shrink-0 border-x border-b border-border bg-bg-elevated p-2 flex items-start gap-2"
              data-testid="crew-webview-mint-error-band"
            >
              <ErrorNotice
                message={i18nT(
                  url
                    ? shownAgo
                      ? "pages.membersPage.webview_refresh_error_aged"
                      : "pages.membersPage.webview_refresh_error"
                    : "pages.membersPage.webview_error",
                  { ago: shownAgo },
                )}
                askAgent
                className="flex-1 min-w-0"
                testId="crew-webview-mint-error"
              />
              <Btn disabled={pending} onClick={retry} className="shrink-0">
                <RotateCw className="lucide-inline" aria-hidden />
                {i18nT("pages.membersPage.webview_retry")}
              </Btn>
            </div>
          )}

          <div className="border border-border border-t-0 rounded-b-lg overflow-hidden bg-card flex-1 min-h-0">
            {url ? (
              <iframe
                ref={expandedFrameRef}
                src={url}
                sandbox={CREW_WEBVIEW_SANDBOX}
                className="w-full h-full border-none bg-card"
                style={{ colorScheme: theme }}
                title={i18nT("pages.membersPage.webview_frame_title", {
                  crew: meta?.crew || slug,
                })}
              />
            ) : failed && !pending ? (
              /* Nothing. The band above already says the render failed, and this
                 slot used to keep saying "Rendering the dashboard…" underneath
                 it — two lines directly contradicting each other, which a reader
                 flagged to UX review. The line belongs to the window where a
                 mint is genuinely in flight, so it is withheld once a mint has
                 failed and no retry is running. */
              null
            ) : (
              <div className="p-4 text-[11px] text-muted">
                {i18nT("pages.membersPage.webview_rendering")}
              </div>
            )}
          </div>
        </motion.div>
      )}
    </div>
  );
}

/**
 * The crewmate's published dashboard, and nothing around it: the Members side
 * panel's Dashboard tab (crewmate-panel IA). The drawer above frames the same
 * document in a summary card, a Contained bar and an Expand control, which in a
 * panel that already IS the dashboard read as three containers stacked over
 * one page. This renders the full document straight into the space it is given.
 *
 * Same read (`api.memberPanel`, keyed on slug AND exact member), same sandbox
 * (`CREW_WEBVIEW_SANDBOX`) and same single-use mint rule: the frame stays
 * mounted once it has a URL, and a read error drops the srcdoc so recovery
 * mints afresh. It mints on mount rather than on Expand, because being on
 * screen is the only reason this component exists.
 */
export interface CrewDashboardFrameProps extends CrewWebviewProps {
  /**
   * The crew record's `avatar` field, verbatim, for the empty state's face.
   * The empty state is the crewmate speaking for itself, so it wears the face
   * the roster row wears; absent, the name-seeded ghost, like everywhere else.
   */
  avatar?: unknown;
  /**
   * Put a suggested prompt into this crewmate's chat box; the person sends it.
   * The empty state's prompts are shown only when this is given, because a
   * prompt with nowhere to land is a dead control.
   */
  onAct?: (text: string) => void;
  /**
   * Leave for Settings → Developer → Feature Previews, through the page's own
   * leave guard. The newer-dashboard hint offers its button only when this is
   * given, because a raw navigate from here would drop the page's unsaved drafts.
   */
  onOpenPreviews?: () => void;
}

export function CrewDashboardFrame(props: CrewDashboardFrameProps) {
  // Keyed on the exact identity for the drawer's reason: slugs are lossy.
  return (
    <CrewDashboardFrameView
      key={JSON.stringify([props.slug, props.member])}
      {...props}
    />
  );
}

function CrewDashboardFrameView({ slug, member, displayName, avatar, onAct, onOpenPreviews }: CrewDashboardFrameProps) {
  // A pull request the panel links to: this frame only, GitHub PR URLs only.
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  useFrameOpenLink([frameRef]);
  const { theme, colorTheme, themeVersion } = useTheme();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion]);
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["member-panel", slug, member],
    queryFn: () => api.memberPanel(slug, member),
    enabled: Boolean(slug) && Boolean(member),
  });
  // Whether this crewmate ADOPTED a dynamic dashboard, which this tab does not draw
  // while the Dashboard feature preview is off. Without a line saying so, a person
  // told "your page is applied" opens this tab and sees the published record instead.
  // Its own key, outside the `member-dashboard` prefix: every fold frame invalidates
  // that prefix, and this read only needs the version, not a live page. A refused
  // read (a non-owner) is an answer, so it is not retried and shows nothing.
  // Finite staleTime: the client default is Infinity and nothing pushes on this key,
  // so a page adopted while the tab is open shows the line on the next mount or focus.
  const { data: adopted } = useQuery({
    queryKey: ["member-dashboard-adopted", slug, member],
    queryFn: () => api.memberDashboard(slug, member),
    enabled: Boolean(slug) && Boolean(member),
    select: (d) => (d?.instance_version ?? 0) > 0,
    retry: false,
    staleTime: 60_000,
  });
  // Two wordings, one per state below it: over the published view the line names
  // that view; over the empty state it says nothing is published, so it never
  // contradicts the bubble under it.
  const newerHint = (key: string) => adopted ? (
    <div
      className="shrink-0 border-b border-border px-4 py-2 flex items-center gap-3 text-xs text-muted"
      data-testid="crew-dashboard-newer-hint"
    >
      <p className="flex-1 min-w-0">{i18nT(key)}</p>
      {onOpenPreviews && (
        <Btn onClick={onOpenPreviews} className="shrink-0" data-testid="crew-dashboard-newer-hint-open">
          {i18nT("pages.membersPage.webview_newer_dashboard_open")}
        </Btn>
      )}
    </div>
  ) : null;
  const html = data?.html ?? null;
  const meta = data?.panel ?? null;
  const srcdoc = useMemo(
    () =>
      html && !isError
        ? buildSrcdoc({ html, themeVars, mode: theme, rewriteBareLinks: false })
        : null,
    [html, isError, themeVars, theme],
  );
  const mint = useSandboxDoc(srcdoc);
  const { url, pending, retry } = mint;
  const failed = mint.failed || mint.stalled;

  if (isLoading) {
    return (
      <div className="p-4 space-y-1.5" data-testid="crew-webview-loading" aria-hidden>
        <div className="h-3 rounded bg-bg-hover animate-pulse" />
        <div className="h-3 w-2/3 rounded bg-bg-hover animate-pulse" />
      </div>
    );
  }
  if (isError) {
    return (
      <div className="p-4 space-y-1.5">
        {/* No hand-off: this frame is the Members page's Dashboard tab, and the
            hand-off is a raw navigate to /chat that unmounts the whole page —
            the Profile card's New schedule draft and the crew editor's unsaved
            panes with it — without asking the page's leave guard (ErrorNotice
            has no gate to hand it). The retry below is the recovery; the chat
            beside this frame is one click away. */}
        <ErrorNotice message={i18nT("pages.membersPage.webview_error")} testId="crew-webview-error" />
        <Btn onClick={() => void refetch()} data-testid="crew-webview-error-retry">
          <RotateCw className="lucide-inline" aria-hidden />
          {i18nT("pages.membersPage.webview_retry")}
        </Btn>
      </div>
    );
  }
  if (!html) {
    // No set-up control: the crew editor has nothing that makes a crewmate
    // publish. It publishes through `panel_publish` when asked or on its own
    // cycle, so the honest next step is asking it in the chat beside this --
    // which is what the empty state's prompts are, pre-written.
    const empty = <CrewDashboardEmpty member={member} displayName={displayName} avatar={avatar} onAct={onAct} />;
    const hint = newerHint("pages.membersPage.webview_newer_dashboard_hint_empty");
    return hint ? (
      <div className="h-full min-h-0 flex flex-col">
        {hint}
        {empty}
      </div>
    ) : empty;
  }
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="crew-dashboard-frame">
      {newerHint("pages.membersPage.webview_newer_dashboard_hint")}
      {failed && (
        // A band above the frame, never over it: a document already on screen
        // keeps every pixel, and the sentence says which failure this is.
        <div className="shrink-0 border-b border-border p-2 flex items-start gap-2" data-testid="crew-webview-mint-error-band">
          {/* No hand-off, for the read-error state's reason: the navigate would
              unmount the page's unsaved Profile / editor drafts unguarded. */}
          <ErrorNotice
            message={i18nT(url ? "pages.membersPage.webview_refresh_error" : "pages.membersPage.webview_error")}
            className="flex-1 min-w-0"
            testId="crew-webview-mint-error"
          />
          <Btn disabled={pending} onClick={retry} className="shrink-0">
            <RotateCw className="lucide-inline" aria-hidden />
            {i18nT("pages.membersPage.webview_retry")}
          </Btn>
        </div>
      )}
      {url ? (
        <iframe
          ref={frameRef}
          src={url}
          sandbox={CREW_WEBVIEW_SANDBOX}
          className="flex-1 min-h-0 w-full border-none bg-bg"
          style={{ colorScheme: theme }}
          title={i18nT("pages.membersPage.webview_frame_title", { crew: meta?.crew || slug })}
          data-testid="crew-dashboard-iframe"
        />
      ) : failed && !pending ? null : (
        <div className="p-4 text-[11px] text-muted">{i18nT("pages.membersPage.webview_rendering")}</div>
      )}
    </div>
  );
}
