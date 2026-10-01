import { useContext } from 'react'
import type { Element as HastElement } from 'hast'
import { safeHttpUrl } from '../../lib/safeUrl'
import { sessionKeyFrom, sessionKeyFromShort } from '../../utils/sessionKeys'
import { LinkUnfurlCtx, type SessionActions, type SidebarFolderActions } from './contexts'

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
  if (!actions.onSessionOpen || !actions.sessions) return null
  const key = sessionKeyFrom(raw)
    ?? sessionKeyFromShort(raw, actions.sessions.keys(), actions.writtenAtEpoch)
  if (!key || key === actions.activeSession) return null
  const title = actions.sessions.get(key)
  if (title === undefined) return null
  return { key, title }
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
