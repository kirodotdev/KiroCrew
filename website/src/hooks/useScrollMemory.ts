import { useCallback, useEffect, useRef } from 'react'

/**
 * In-memory scroll-position memory for side-panel document tabs.
 *
 * Why this exists: panel tabs live in per-chat-slot buckets (`usePanelTabs`),
 * so switching chat sessions unmounts the previous slot's tab bodies entirely
 * — a document the user had scrolled remounts at `scrollTop = 0` when they
 * return. Within a session this never happens: SidePanel hides inactive
 * document tabs with `display: none`, and scroll survives natively. This hook
 * covers only the cross-session remount, mirroring the module-scope store
 * pattern the tab buckets themselves use.
 *
 * In-memory only (no localStorage) — parity with document-tab CONTENT, which
 * `serializeBucket` likewise strips on persist: after a page reload the
 * content is re-fetched and may not match, so a persisted pixel offset would
 * restore into a different document. A reload starting at the top is correct.
 *
 * Deliberately out of scope (see issue #5701): widget/html artifacts render
 * inside a sandboxed `srcdoc` iframe whose internal scroll cannot be observed
 * from here and which fully reloads on remount; restoring it would need a
 * postMessage bridge extension that is not worth the complexity.
 */

const positions = new Map<string, number>()

/** Separator for slot-scoped keys: \u001F (unit separator) cannot appear in a
 * chat-slot key or a tab id — same convention as `mcpAppKey` in chatSlice. */
const KEY_SEP = '\u001F'

/** Canonical cross-remount identity for a side-panel document tab. */
export const scrollMemoryKeyFor = (slot: string, tabId: string): string =>
  `${slot}${KEY_SEP}${tabId}`

/** FIFO insertion cap. Entries are one number each, keyed by slot + tab id,
 * so this is generous — it exists only so a dashboard left open for weeks can
 * never grow the map unbounded as sessions and tabs come and go. */
const MAX_ENTRIES = 500

function remember(key: string, top: number): void {
  if (!positions.has(key) && positions.size >= MAX_ENTRIES) {
    const oldest = positions.keys().next().value
    if (oldest !== undefined) positions.delete(oldest)
  }
  positions.set(key, top)
}

/** Test-only: reset the module-scope store between cases. */
export function _resetScrollMemory(): void {
  positions.clear()
}

/**
 * Remember and restore a scroll container's `scrollTop` across remounts.
 *
 * @param key   Stable identity of the document across remounts — callers pass
 *              `slot + tab id`. `undefined`/`null` disables the hook (e.g. a
 *              host that renders outside the side panel).
 * @param ref   The scroll container. The SAME ref the caller already owns —
 *              the hook only reads `ref.current` inside the restore effect.
 * @param ready True once the real content is committed (not a loading state).
 *              Restoring earlier would write into an empty container and be
 *              clamped to 0 by the browser.
 * @param opts  `suppressRestore`: true when the mount carries an explicit
 *              scroll target of its own (a `file.py:447` line reveal). The
 *              restore is skipped — and the one-shot latch burned, so it can
 *              never fire later and yank the reveal — while recording
 *              continues untouched.
 * @returns     `onScroll` — attach to the scroll container as a React prop.
 *              React re-attaches it across conditional remounts (e.g. the
 *              fullscreen round-trip) with no listener bookkeeping here.
 *
 * Restore is one-shot per mount: it fires when `ready` first holds with a
 * remembered position, and never again for that mount, so later content
 * refreshes (file watch, artifact version bumps) cannot yank a position the
 * user has since chosen. If the document shrank while unmounted, the browser
 * clamps the write to the real scroll range.
 */
export function useScrollMemory(
  key: string | null | undefined,
  ref: React.RefObject<HTMLElement | null>,
  ready: boolean,
  opts?: { suppressRestore?: boolean },
): { onScroll: React.UIEventHandler<HTMLElement> } {
  const suppressRestore = opts?.suppressRestore ?? false
  const restoredRef = useRef(false)
  // A rail navigation re-targets a file tab in place (same mount, new
  // document, new key) — re-arm the one-shot latch for the new identity.
  useEffect(() => { restoredRef.current = false }, [key])

  useEffect(() => {
    if (!key || !ready || restoredRef.current) return
    const el = ref.current
    if (!el) return
    restoredRef.current = true
    if (suppressRestore) return
    const saved = positions.get(key)
    if (saved !== undefined && saved > 0) el.scrollTop = saved
  }, [key, ready, suppressRestore, ref])

  const onScroll = useCallback<React.UIEventHandler<HTMLElement>>(e => {
    if (key) remember(key, e.currentTarget.scrollTop)
  }, [key])

  return { onScroll }
}
