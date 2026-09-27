import { useContext } from 'react'
import type { Element as HastElement } from 'hast'
import { safeHttpUrl } from '../../lib/safeUrl'
import { sessionKeyFrom, sessionKeyFromShort } from '../../utils/sessionKeys'
import { LinkUnfurlCtx, type SessionActions } from './contexts'

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
