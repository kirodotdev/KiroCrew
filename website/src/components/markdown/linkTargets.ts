import { useContext, useEffect, useState } from 'react'
import type { Element as HastElement } from 'hast'
import { safeHttpUrl } from '../../lib/safeUrl'
import { classifyShortName, sessionKeyFrom } from '../../utils/sessionKeys'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { ClosedSessionCtx, LinkUnfurlCtx, type SessionActions, type SidebarFolderActions } from './contexts'

/**
 * Where a rendered link or chip points: an in-app artifact route, whether an
 * href may be unfurled, the one link a paragraph consists of, and the open
 * session a key names. Classification only; the components that act on the
 * answer are `MdAnchor`, `MdParagraph` (both in `MarkdownRenderer.tsx`) and
 * `InlineCode`.
 */

/** Extract the artifact slug from an `/artifacts/<slug>` href. Returns null
 *  when the href isn't an artifact route. Handles a leading origin, a trailing
 *  query/hash, and percent-encoded slugs (the agent emits an encoded slug
 *  matching the canonical full-page artifact URL). */
export function artifactSlugFromHref(href: string | null | undefined): string | null {
  if (!href) return null
  // Strip an optional origin so both relative (`/artifacts/x`) and absolute
  // (`http://host/artifacts/x`) forms resolve identically.
  let path = href
  try { path = new URL(href, 'http://x').pathname } catch { /* keep raw */ }
  const m = /^\/artifacts\/([^/?#]+)/.exec(path)
  if (!m) return null
  try { return decodeURIComponent(m[1]) } catch { return m[1] }
}

/**
 * The href to unfurl, or null when the link must stay a plain anchor.
 *
 * Three exclusions, all deliberate:
 *  - non-http(s) (and Basic-auth userinfo) — `safeHttpUrl`. `artifact:`,
 *    `vscode:`, `mailto:`, `javascript:` and relative paths all fail here, so
 *    only an absolute web URL can ever reach the backend.
 *  - `/artifacts/<slug>` — an in-app artifact route, handled by the renderer
 *    root's click interception; unfurling it would fetch our own dashboard.
 *  - anything else same-origin — likewise an in-app dashboard route. There is no
 *    page title to show that the UI doesn't already know.
 */
export function unfurlableHref(href: string | null | undefined): string | null {
  if (!href || !safeHttpUrl(href)) return null
  if (artifactSlugFromHref(href)) return null
  try {
    if (new URL(href).origin === window.location.origin) return null
  } catch {
    return null
  }
  return href
}

/** Resolve the unfurl target for an href under the current gate. A hook (reads
 *  context), so it is called unconditionally by both link components. */
export function useUnfurlHref(href: string | null | undefined): string | null {
  const { enabled, live } = useContext(LinkUnfurlCtx)
  if (!enabled || live) return null
  return unfurlableHref(href)
}

/**
 * The single `<a>` that is a paragraph's ONLY element child, or null.
 *
 * Whitespace-only text siblings are ignored (remark leaves a trailing newline
 * text node on `<p><a>…</a></p>`), but any real text, or a second element,
 * disqualifies the paragraph — that link is inline prose and gets a chip.
 * `text` is the anchor's own visible text, used only as the probe argument for a
 * `LinkOverrideCtx` provider.
 */
export function soleLinkInParagraph(node?: HastElement): { href: string; text: string } | null {
  if (!node?.children) return null
  let anchor: HastElement | null = null
  for (const child of node.children) {
    if (child.type === 'text') {
      if (child.value.trim()) return null
      continue
    }
    if (child.type !== 'element' || anchor || child.tagName !== 'a') return null
    anchor = child
  }
  const href = anchor?.properties?.href
  if (!anchor || typeof href !== 'string') return null
  const text = anchor.children
    .map((c) => (c.type === 'text' ? c.value : ''))
    .join('')
  return { href, text }
}

/**
 * Whether a recognised slot key may render as a chip, and what to title it with.
 *
 * Mirrors the path chip's rule — an affordance only once the target is CONFIRMED —
 * with the slot roster standing in for the stat probe. Three refusals, each of
 * which must stay plain text rather than become a chip that cannot act:
 *
 *   - the caller wired no handler or no roster (see `SessionActions`);
 *   - the key names a session that is not open, so there is nothing to switch to.
 *     A closed session's transcript may still exist on disk, but reopening it is
 *     a History-page resume rather than a slot switch, so `onSessionOpen` could
 *     not honour a chip here;
 *   - the key names the session the reader is ALREADY in, where a click would be
 *     a visible no-op.
 *
 * A SHORT name (`chat-1380`, no timestamp) resolves through the same roster. The
 * roster was already the authority for whether a chip may exist, so letting it
 * also say which session a nickname means adds no new trust: a name it does not
 * answer for is refused by the second rule above, like any other unknown key.
 */
export function resolveSessionChip(raw: string, actions: SessionActions): { key: string; title: string } | null {
  // Delegate to the one classifier, the same way `sessionKeyFromShort` delegates
  // to `classifyShortName`: the chip is exactly the switchable case, so there is
  // no second copy of the handler / roster / active-key guard to drift from.
  const c = classifySessionLink(raw, actions)
  return c.kind === 'open' ? { key: c.key, title: c.title } : null
}

/**
 * What a `?sid=` link's sid IS, from the ONE roster lookup — the SINGLE place the
 * handler / roster / active-key guard lives. `MdAnchor` reads the affordance
 * (muted vs live) and the hover tooltip off this, and `resolveSessionChip`
 * returns its `open` case, so a link, a bare key and a chip naming one session
 * can never disagree. A second copy drifts: a key spelling added to one lookup
 * alone would leave the other saying the opposite, which is exactly the false
 * "not open" tooltip this fixes.
 *
 *   - `open`      — resolves to a switchable session (the chip case). Live link.
 *   - `active`    — resolves to the session the reader is already in. Muted, and
 *                   NO "not open" tooltip: it is open, so that would be false.
 *   - `not-open`  — a full key the roster does not hold, or a short name whose
 *                   number no open slot answers to. Muted, and the only case that
 *                   earns the "not open" tooltip, because here it is TRUE.
 *   - `unresolved`— a short name we cannot pin to one open slot though it may name
 *                   one: ambiguous (two open slots share the number) or no message
 *                   timestamp. Muted, but SILENT — "not open" would be a guess.
 *   - `not-routing`— the renderer cannot route sessions (handler or roster not
 *                   wired). The link stays an ordinary navigating anchor.
 */
export type SessionLinkClass =
  | { kind: 'open'; key: string; title: string }
  | { kind: 'active' }
  | { kind: 'not-open' }
  | { kind: 'unresolved' }
  | { kind: 'not-routing' }

export function classifySessionLink(raw: string, actions: SessionActions): SessionLinkClass {
  if (!actions.onSessionOpen || !actions.sessions) return { kind: 'not-routing' }
  const roster = actions.sessions
  // A full key names its generation exactly, independent of any write time. A
  // short name goes through the roster ONCE, and its outcome also says WHY it did
  // not resolve — the distinction a "not open" tooltip needs.
  const fullKey = sessionKeyFrom(raw)
  const short = fullKey ? null : classifyShortName(raw, roster.keys(), actions.writtenAtEpoch)
  const key = fullKey ?? (short!.kind === 'resolved' ? short!.key : null)
  if (key) {
    if (key === actions.activeSession) return { kind: 'active' }
    const title = roster.get(key)
    // A key the roster does not hold is a session that is not open.
    return title === undefined ? { kind: 'not-open' } : { kind: 'open', key, title }
  }
  // No key, so this is a short name (a full key always yields a key above).
  // Distinguish "no open slot has this number" (truly not open) from "cannot pin
  // to one" (ambiguous / no write time — may well be open, so stay silent).
  return short!.kind === 'no-match' ? { kind: 'not-open' } : { kind: 'unresolved' }
}

/** A probed row as the gateway has it: `title` is raw, '' when untitled. */
type ClosedRow = { key: string; title: string }
/** The row under the name the reader sees, worked out at render or click, so
 *  a language switch re-localizes "Untitled session"; the cache stays raw. */
const namedRow = (row: ClosedRow): ClosedRow => ({ key: row.key, title: sessionDisplayTitle(row) })
/** A probe's answer: the row, a definite "no such session", or a failed request. */
type ClosedAnswer = ClosedRow | null | 'failed'

/**
 * One probe per key per page life. A found row and a definite "no such
 * session" are both kept; a failed request is not, so a later mount retries.
 * A session resumed from here becomes open and resolves through the roster
 * first, so a kept row never shadows a live tab.
 */
const closedProbes = new Map<string, Promise<ClosedAnswer>>()
const closedAnswers = new Map<string, ClosedRow | null>()
/** Every mounted `useSessionChip`, told when a shared answer turns definite
 *  "gone", so each reference to that key drops together with the one whose
 *  click or retry found it deleted, notice included. */
const closedListeners = new Set<() => void>()
function markGone(key: string): void {
  closedProbes.delete(key)
  closedAnswers.set(key, null)
  for (const notify of closedListeners) notify()
}

function probeClosed(key: string, lookup: (key: string) => Promise<ClosedRow | null>, fresh = false): Promise<ClosedAnswer> {
  let pending = fresh ? undefined : closedProbes.get(key)
  if (!pending) {
    pending = lookup(key).then(
      (row): ClosedAnswer => { closedAnswers.set(key, row); return row },
      (): ClosedAnswer => { closedProbes.delete(key); return 'failed' },
    )
    closedProbes.set(key, pending)
  }
  return pending
}

/** Test seam: forget every probe answer. */
export function resetClosedSessionProbes(): void {
  closedProbes.clear()
  closedAnswers.clear()
  closedListeners.clear()
}

/**
 * Set on a closed-session target whose check failed (the background probe, or
 * the check a click runs). The probe itself says nothing; the reader learns of
 * the failure only when they click, through an inline notice at that spot.
 */
export type ClosedSessionFailure = {
  /** The reader clicked, so the inline notice is up. */
  shown: boolean
  /** A retry is in flight. */
  busy: boolean
  /** Check again; open the session if it is there. */
  retry: () => void
}

export type SessionChipTarget = {
  key: string
  /** The session as the reader should see it named: a probed row's title
   *  ("Untitled session" when it has none), else the caller's visible label
   *  for it, else the raw key. */
  title: string
  open: (key: string) => void
  /** Present only while the closed session's check is failing. */
  failure?: ClosedSessionFailure
}

/**
 * `resolveSessionChip`, widened to a CLOSED session the page can resume.
 *
 * An open session resolves exactly as before, and opens through
 * `onSessionOpen`. A miss falls back to `ClosedSessionCtx.lookup` for a FULL key
 * only (a short name like `chat-7` has no timestamp, so it cannot say which past
 * session it means), and only when the open roster is wired, so an offline or
 * no-controller render stays as it was. Until the probe answers there is no
 * target, and a key the gateway says does not exist never gets one.
 *
 * A probe that FAILED (not a 404) still gives a target, carrying `failure`: its
 * click shows the inline notice instead of opening, and the notice's retry asks
 * again. `open` is the activation the caller must use, since the kinds of target
 * act differently.
 *
 * `label` is what the reader sees standing for the session (a link's prose
 * text). A failed target has no probed title to name itself by, so its `title`
 * falls back to the label before the raw key: the notice then says "Couldn't
 * open the old run" rather than quoting a key the reader never saw.
 */
export function useSessionChip(raw: string | null, actions: SessionActions, label?: string): SessionChipTarget | null {
  const closed = useContext(ClosedSessionCtx)
  const live = raw ? resolveSessionChip(raw, actions) : null
  const full = !live && raw && closed.lookup && closed.open && actions.onSessionOpen && actions.sessions ? sessionKeyFrom(raw) : null
  const candidate = full && full !== actions.activeSession && !actions.sessions!.has(full) ? full : null
  const [answer, setAnswer] = useState<{ for: string; value: ClosedAnswer } | null>(null)
  const [notice, setNotice] = useState<{ for: string; busy: boolean } | null>(null)
  const [, bump] = useState(0)
  useEffect(() => {
    const notify = () => bump(n => n + 1)
    closedListeners.add(notify)
    return () => { closedListeners.delete(notify) }
  }, [])
  useEffect(() => {
    if (!candidate || !closed.lookup || closedAnswers.has(candidate)) return
    let current = true
    void probeClosed(candidate, closed.lookup).then((value) => { if (current) setAnswer({ for: candidate, value }) })
    return () => { current = false }
  }, [candidate, closed.lookup])
  if (live) {
    // Open now: the roster speaks for it. Forget a closed-time answer, so a
    // title it had then is not shown if the session is closed again later.
    const key = live.key
    if (closedAnswers.has(key)) { closedAnswers.delete(key); closedProbes.delete(key) }
    return { ...live, open: actions.onSessionOpen! }
  }
  if (!candidate || !closed.open || !closed.lookup) return null
  // A definite "gone" in the shared answer wins over everything: the session
  // is deleted, whichever reference found out. Then this chip's own failed
  // check; otherwise the shared answer, then ours.
  const mine = answer?.for === candidate ? answer : null
  const shared = closedAnswers.has(candidate) ? closedAnswers.get(candidate)! : undefined
  const value: ClosedAnswer = shared === null
    ? null
    : mine?.value === 'failed'
      ? 'failed'
      : shared !== undefined ? shared : mine ? mine.value : null
  if (value === null) return null
  const openClosed = closed.open
  const lookup = closed.lookup
  const key = candidate
  const settle = (next: ClosedAnswer) => setAnswer({ for: key, value: next })
  // `open` checks again that the session still exists: the cached answer can
  // be older than a delete. Gone since: the target drops. Check failed: the
  // inline notice comes up at the spot the reader clicked.
  const openRow = (row: ClosedRow) => {
    void openClosed(row).then((outcome) => {
      if (outcome === 'gone') {
        markGone(key)
        setNotice(null)
        settle(null)
      } else if (outcome === 'failed') {
        // Local to this chip: the shared answer stays, so another chip for the
        // same key is not dropped by this one's failed check.
        settle('failed')
        setNotice({ for: key, busy: false })
      } else {
        setNotice(null)
      }
    })
  }
  // `open` gets the reader's name only for its "was deleted" notice; the page
  // resumes from the row it fetches again, never from this name.
  if (value !== 'failed') {
    const named = namedRow(value)
    return { key, title: named.title, open: () => openRow(named) }
  }
  // Named for the reader: the row a probe found (a click-time check can fail
  // after the background probe answered), else the visible label, else the key.
  const cached = closedAnswers.get(key)
  const title = cached ? sessionDisplayTitle(cached) : (label?.trim() || key)
  const shown = notice?.for === key
  const failure: ClosedSessionFailure = {
    shown,
    busy: shown && notice!.busy,
    retry: () => {
      setNotice({ for: key, busy: true })
      void probeClosed(key, lookup, true).then((next) => {
        if (next === 'failed') { setNotice({ for: key, busy: false }); return }
        // A definite "no such session" on retry: the session was deleted since
        // the reader first saw the link. Hand it to the page's `open` under the
        // name the reader knows, so its check says "was deleted" the way a click
        // on a deleted chip does, and the target then drops through `gone`.
        // Dropping here silently would leave Try again answered by nothing.
        if (next === null) { openRow({ key, title }); return }
        settle(next)
        openRow(namedRow(next))
      })
    },
  }
  return { key, title, open: () => setNotice({ for: key, busy: false }), failure }
}

/**
 * Split a span into folder-path segments, or null when it is not folder-shaped.
 *
 * Two spellings are read, both of which the product itself writes: the `/`-joined
 * human path the folder tools take and return (`goal/worker`), and the ` › `
 * breadcrumb the chat header and the `[FOLDER]` context line show
 * (`kirocrew › oss`). One trailing separator is tolerated -- `goal/worker/` is how
 * a directory-minded author spells a folder. An empty segment anywhere else
 * (`a//b`, a leading `/`) is refused: a leading slash is a filesystem path, which
 * the path chip owns, and a doubled separator names nothing in the tree.
 *
 * At least TWO segments: a separator is the one shape signal a folder path has.
 * A session key has key shape and a filesystem path has a stat probe, but a bare
 * word (`test`, `docs`, `main`) has nothing to say it means a folder, and with a
 * top-level folder of that name every such span in every message would lose
 * its click-to-copy for a click that jerks the sidebar. The bare name stays the
 * plain span; a top-level folder is reached by its row.
 */
export function folderSegmentsOf(raw: string): string[] | null {
  const trimmed = raw.trim().replace(/(?:\s*›\s*|\/)$/, '')
  if (!trimmed) return null
  const parts = trimmed.includes('›') ? trimmed.split(/\s*›\s*/) : trimmed.split('/')
  if (parts.length < 2 || parts.some(p => p.trim() === '')) return null
  return parts.map(p => p.trim())
}

/**
 * The NORMALISED human path of every reachable folder row, root to leaf: names
 * joined with `/`, and every ` › ` inside a name read as `/` too, so a rendered
 * path is keyed exactly the way `folderSegmentsOf` keys a span.
 *
 * Rendered from the rows themselves rather than walked segment by segment from
 * the span, because a folder NAME may contain either separator: `chat_folders.py`
 * only strips and caps a name, so a root folder literally called `goal/worker`,
 * or `goal › worker`, can sit beside a nested `goal` -> `worker`, and all three
 * normalise to the same key. Indexing under that one key is what makes such a
 * collision visible; a per-segment walk could only ever find the nested one, and
 * indexing the `/` rendering alone would miss the breadcrumb-spelled name. An orphan row (a
 * `parent_id` naming no row, or a cycle) renders to nothing, which matches the
 * sidebar: it draws such rows under a recovery heading, not at any path.
 */
function renderedFolderPaths(rows: readonly { id: string; name: string; parent_id?: string }[]): Map<string, string[]> {
  const byId = new Map(rows.map(r => [r.id, r]))
  const out = new Map<string, string[]>()
  for (const row of rows) {
    const names: string[] = []
    let cur: { id: string; name: string; parent_id?: string } | undefined = row
    let ok = true
    for (let guard = 0; cur; guard += 1) {
      if (guard > rows.length) { ok = false; break }
      names.unshift(cur.name)
      if (!cur.parent_id) break
      cur = byId.get(cur.parent_id)
      if (!cur) ok = false
    }
    if (!ok) continue
    const path = names.map(n => n.replace(/\s*›\s*/g, '/')).join('/')
    const ids = out.get(path)
    if (ids) ids.push(row.id)
    else out.set(path, [row.id])
  }
  return out
}

/**
 * Whether a span names a sidebar folder by its FULL human path, and which one.
 *
 * Same discipline as `resolveSessionChip`: the folder roster confirms the target
 * the way the slot roster confirms a session, so the chip is offered only for a
 * folder that exists, and stays plain text otherwise. The span's normalised
 * `/`-joined path is compared, case-sensitive, against every folder's rendered
 * path (`renderedFolderPaths`), so the match is the whole ancestry root to leaf.
 * Two folders can share a leaf name under different parents; the full path is
 * what tells them apart, so a partial match is never offered.
 *
 * Refusals, each of which must stay plain text: no handler or no roster wired
 * (see `SidebarFolderActions`); a shape that is not a folder path
 * (`folderSegmentsOf`); a path no folder renders to; a path that MORE than one
 * folder renders to. The sidebar lets a person hold two sibling folders of one
 * name (`chat_folders.py`), and a folder name may itself contain `/` or ` › `,
 * so `goal/worker` can name two siblings, or a nested folder and a root folder
 * spelled with either separator -- the same ambiguity `_ensure_chat_folder_path`
 * refuses on the server, and a chip that picked one would flash an arbitrary
 * folder with no sign the reference was ambiguous.
 */
export function resolveFolderChip(raw: string, actions: SidebarFolderActions): { id: string; path: string } | null {
  if (!actions.onFolderReveal || !actions.folders) return null
  const segments = folderSegmentsOf(raw)
  if (!segments) return null
  const path = segments.join('/')
  const ids = renderedFolderPaths(actions.folders).get(path)
  if (!ids || ids.length !== 1) return null
  return { id: ids[0], path }
}
