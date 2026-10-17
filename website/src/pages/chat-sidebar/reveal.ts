/** Reveal-in-sidebar: consumes the store-held reveal request for a session or a
 *  folder, clears what hides the target, opens its ancestors, then scrolls to and
 *  flashes it with a bounded retry. */
import { useRef, useState, useEffect, useCallback, type Dispatch, type SetStateAction, type RefObject } from 'react'
import { useAppSelector, type AppDispatch } from '../../store'
import { clearSlotReveal } from '../../store/chatSlice'
import type { ChatFolder } from '../../types'
import { safeSetItem } from '../../utils/safeStorage'
import { sessionRowIdentity } from './rowIdentity'
import { HIDDEN_FOLDERS_LS_KEY } from './persistence'
import type { SortKey } from '../chat/sessionOrder'
import type { Slot, RevealBlockingFilter } from './types'

/**
 * Row markers a reveal targets, by the kind of thing being revealed.
 *
 * Held as attribute NAMES rather than finished selectors because a reveal targets
 * one specific row, so the selector has to carry an escaped value. Keeping the
 * names here and composing in {@link rowSelector} means the two reveal callers
 * name a kind and never spell a selector, so the "targeting goes through
 * `data-session-row`, never `data-slot-key`" invariant lives in one place instead
 * of in a comment at each call site.
 */
const REVEAL_ROW_ATTR = { session: 'data-session-row', folder: 'data-folder-row' } as const

/**
 * Second-choice marker for a FOLDER reveal, consulted only when the folder has no
 * header row. That is the board lane: it renders a folder as a column body with
 * this drop marker and no `data-folder-row` anywhere, so the primary selector
 * cannot match and the reveal would expire silently. It is deliberately NOT in
 * `REVEAL_ROW_ATTR` — this marker is ambiguous by design (the tree lane renders it
 * too, on the folder BODY, and every board column renders its own copy), so it is
 * only ever reached after the unambiguous header lookup has already missed.
 */
const REVEAL_FOLDER_FALLBACK_ATTR = 'data-folder-drop'

/** Attribute selector for one row, with the value escaped for `querySelector`. */
const rowSelector = (attr: string, value: string) => `[${attr}="${window.CSS.escape(value)}"]`

/** Reveal-in-sidebar retry budget: ancestor expansion and filter resets land
 *  through mutations and re-renders, so the target row can enter the DOM
 *  several frames after the request is consumed. 20 × 100 ms ≈ 2 s, then the
 *  reveal gives up (the row genuinely isn't renderable, e.g. board lane with
 *  no matching column). */
const REVEAL_RETRY_MS = 100
const REVEAL_MAX_ATTEMPTS = 20
/** How long the reveal confirmation outline holds before fading out. */
const REVEAL_FLASH_HOLD_MS = 1600
/** Must cover the CSS fade on .session-reveal-flash-fade in index.css (.4s):
 *  the classes are removed at HOLD + FADE + slack, so shortening this below
 *  the CSS duration snaps the outline off mid-fade. */
const REVEAL_FLASH_FADE_MS = 500

/** Consumes session and folder reveal requests and flashes the target row. */
export function useSidebarReveal({ sidebarRootRef, dispatch, localSlots, revealBlockingFilters, staleCollapseMs, sortKey, isStaleExempt, slotFolders, setStaleExpanded, expandFolderAncestors, expandConductorAncestors, folders, setSlotFilter, setFilterHiddenFolders, setRevealForcedVisible, setFlatView }: {
  sidebarRootRef: RefObject<HTMLDivElement>
  dispatch: AppDispatch
  localSlots: Slot[]
  revealBlockingFilters: RevealBlockingFilter[]
  staleCollapseMs: number
  sortKey: SortKey
  isStaleExempt: (s: Slot) => boolean
  slotFolders: Record<string, string>
  setStaleExpanded: Dispatch<SetStateAction<Set<string>>>
  expandFolderAncestors: (folderId: string) => void
  expandConductorAncestors: (key: string) => void
  folders: ChatFolder[]
  setSlotFilter: Dispatch<SetStateAction<string>>
  setFilterHiddenFolders: Dispatch<SetStateAction<Set<string>>>
  setRevealForcedVisible: Dispatch<SetStateAction<Set<string>>>
  setFlatView: (on: boolean) => void
}) {
  // Reveal-in-sidebar: consume the pending request held in the store (set by
  // the session header menu). Store state rather than a window event on
  // purpose: this component is unmounted while the drawer is collapsed, so an
  // event dispatched before the mount commits had no listener and was silently
  // dropped — the request waiting here is picked up by this effect on mount as
  // well as on change (#912 D1). The nonce makes repeat reveals of the same
  // row distinct requests, so the effect re-fires even when the key repeats.
  const revealRequest = useAppSelector(s => s.chat.revealRequest)
  // Serial + pending timer for the in-flight reveal: a newer reveal cancels
  // the older retry loop, and unmount stops the pending timer outright.
  const revealRunRef = useRef<{ seq: number; timer: number | null }>({ seq: 0, timer: null })
  // Row currently flashing as reveal confirmation. Rendered into the row's
  // className (not imperative classList mutation) so the highlight survives row
  // remounts — list reorders and re-keyed renders would silently drop a
  // manually-added DOM class.
  //
  // `kind` is part of the identity, not decoration: a session is keyed by slot key
  // and a folder by folder id, two namespaces that can collide, so without it a
  // folder reveal could light up a session row that happens to share the string.
  const [revealFlash, setRevealFlash] = useState<{ kind: 'session' | 'folder'; key: string; fading: boolean } | null>(null)
  const revealFlashTimersRef = useRef<number[]>([])
  useEffect(() => () => {
    const run = revealRunRef.current
    if (run.timer != null) clearTimeout(run.timer)
    revealFlashTimersRef.current.forEach(clearTimeout)
  }, [])
  /**
   * Scroll a revealed row into view and flash it, retrying until the row exists.
   *
   * Shared by the session reveal and the folder reveal because the hard parts are
   * identical for both: the target may not be in the DOM yet (ancestor expansion
   * and filter resets land through mutations and re-renders, so a single
   * fixed-delay attempt silently loses the race — #912 D3), the retry must be
   * bounded, and a newer reveal must cancel an older one's pending loop. The two
   * callers share ONE `revealRunRef` serial for that last reason: a folder reveal
   * arriving mid-session-reveal supersedes it rather than racing it.
   *
   * The selector is derived from `kind` via {@link REVEAL_ROW_ATTR} and queried
   * against this sidebar's subtree, never `document`: other surfaces and
   * board-view duplicate renders carry the same row markers (#912 D5).
   *
   * A FOLDER has a second marker, and it is not redundant. `data-folder-row` is
   * the tree lane's folder header and exists nowhere else, but the BOARD lane
   * renders a folder as a column body carrying `data-folder-drop` and no header
   * row at all — so in board view the primary selector matches nothing and the
   * bounded retry loop just expires, leaving the click with no scroll and no
   * flash. The fallback is ordered, never merged into one selector: in the tree
   * lane `data-folder-drop` is ALSO rendered (the folder body wrapper), so a
   * combined query could return whichever sorts first in the DOM. Trying the
   * header first means the ambiguous marker is only ever consulted in the lane
   * that has no header to be ambiguous with.
   */
  const runReveal = useCallback((kind: 'session' | 'folder', key: string, target: string) => {
    const selectors = [rowSelector(REVEAL_ROW_ATTR[kind], target)]
    if (kind === 'folder') selectors.push(rowSelector(REVEAL_FOLDER_FALLBACK_ATTR, target))
    const run = revealRunRef.current
    run.seq += 1
    const seq = run.seq
    if (run.timer != null) { clearTimeout(run.timer); run.timer = null }
    let attempt = 0
    const tryScroll = () => {
      if (revealRunRef.current.seq !== seq) return
      let el: HTMLElement | null = null
      for (const sel of selectors) {
        const found = sidebarRootRef.current?.querySelector<HTMLElement>(sel) ?? null
        // A row inside a collapsed folder stays MOUNTED — FolderBody animates
        // height rather than unmounting, and marks the body aria-hidden + inert
        // (contract at the FolderBody call site). `[inert]` is the sidebar's
        // canonical "hidden row" spelling, the same filter the digit-target scan
        // and sessionRowsInScope apply.
        //
        // Skipping it is what makes the retry loop correct rather than decorative.
        // The first attempt runs SYNCHRONOUSLY, before the `expandFolderAncestors`
        // setState has committed, so on the common path — search a folder, jump to
        // it, ancestors still collapsed — the target is present and inert. Accepting
        // it scrolled a height-0 collapsed row into view and returned, and the
        // retry never fired because it only fires when nothing was found at all.
        el = found && !found.closest('[inert]') ? found : null
        if (el) break
      }
      if (!el) {
        attempt += 1
        if (attempt <= REVEAL_MAX_ATTEMPTS) run.timer = window.setTimeout(tryScroll, REVEAL_RETRY_MS)
        // Row never became visible: either it never rendered (board lane with no
        // matching column) or it stayed inert for the whole budget (an ancestor
        // that never expanded). Not user-visible either way, so leave a trace for
        // bug reports instead of vanishing.
        // eslint-disable-next-line no-console -- records that the bounded retry loop exhausted REVEAL_MAX_ATTEMPTS; without it an unrendered row is indistinguishable from a reveal that worked
        else console.debug('reveal-in-sidebar: row never became visible for', kind, key)
        return
      }
      const reduce = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' })
      // Visible confirmation even when the row never moved (#912 D4): an
      // accent outline that fades out (classes in index.css, rendered via
      // revealFlash state). Outline, not background — the target is usually
      // the ACTIVE row, which already carries the accent-subtle background —
      // and not box-shadow, which the recency tint drives inline. The fade is
      // a non-spatial color transition, so it needs no reduced-motion branch
      // (same treatment as MarkdownPanel's flashCommentRow); the scroll above
      // handles the spatial half. A newer flash replaces the older one
      // immediately, so two rows are never highlighted at once.
      revealFlashTimersRef.current.forEach(clearTimeout)
      const same = (f: { kind: string; key: string } | null) => !!f && f.kind === kind && f.key === key
      setRevealFlash({ kind, key, fading: false })
      const t1 = window.setTimeout(() => setRevealFlash(f => (same(f) ? { kind, key, fading: true } : f)), REVEAL_FLASH_HOLD_MS)
      const t2 = window.setTimeout(() => setRevealFlash(f => (same(f) ? null : f)), REVEAL_FLASH_HOLD_MS + REVEAL_FLASH_FADE_MS)
      revealFlashTimersRef.current = [t1, t2]
    }
    tryScroll()
  }, [sidebarRootRef])
  useEffect(() => {
    if (!revealRequest) return
    // One field carries both kinds, so each effect answers for its own and leaves
    // the other's request alone. Two pending requests can no longer exist, which is
    // what removes the ordering hazard the old field pair had.
    if (revealRequest.kind !== 'session') return
    const key = revealRequest.target
    // Consume immediately: the request must not survive to a later remount.
    dispatch(clearSlotReveal())
    const slot = localSlots.find(s => s.key === key)
    if (!slot) {
      // Stale key or a session outside this surface's slot list. Not user-visible
      // (there is nothing to highlight), so leave a trace for bug reports.
      // eslint-disable-next-line no-console -- a reveal's only success signal is the scroll+flash, so this early return is the one path where the user's click provably did nothing and nothing else records it
      console.debug('reveal-in-sidebar: no session for key', key)
      return
    }
    // Reveal means "show me this row", so drop every filter hiding the target
    // rather than scrolling to nothing (#912 D5). Registered in one list: `revealBlockingFilters`.
    for (const dim of revealBlockingFilters) if (dim.hides(slot)) dim.clear(slot)
    // The stale-session collapse is a per-container disclosure, not a filter
    // dimension (clearing a dimension drops it everywhere; a reveal should
    // open ONE dormant section, not all of them), so it is handled here
    // rather than in the registry: pre-expand the target's container when the
    // row is stale-collapsible, or the retry loop below scrolls to a row that
    // never rendered (#6479). Gated on the collapse being ACTIVE (same gate
    // as the narrow-bridge consumer in ./stale): with the feature off or under a
    // non-date sort the row renders anyway, and the write would leave that
    // container's section pre-opened whenever the collapse next re-engages.
    // Deliberately NOT gated on `listNarrowed`: the filter clearing two lines
    // up has not committed yet, so it would still read true here.
    if (staleCollapseMs > 0 && sortKey === 'date-desc' && !isStaleExempt(slot)) {
      const container = slotFolders[key] || 'root'
      setStaleExpanded(prev => (prev.has(container) ? prev : new Set(prev).add(container)))
    }
    if (slot.folder_id) expandFolderAncestors(slot.folder_id)
    // Same need, the other axis: in the conductor lane the target may sit inside a
    // collapsed conductor (and inside one collapsed inside another), and the retry
    // loop below would scroll to a row that never rendered. Unconditional rather than
    // gated on the lane being active -- a reveal arriving while the user is in the
    // tree lane should leave the conductor lane already open at the right place for
    // when they switch back, and for a row with no creator it is a no-op.
    //
    // Takes the ORIGIN-QUALIFIED identity for the same reason `runReveal` does below:
    // the conductor tree is keyed that way, so a raw slot key would miss the row (or,
    // on a collision, name the peer's).
    expandConductorAncestors(sessionRowIdentity(slot))
    // Targeted by the `session` row marker (the ORIGIN-QUALIFIED identity), not
    // `data-slot-key`. Once peer rows are merged into this list the raw slot
    // key is no longer a unique namespace — a remote row with a byte-identical
    // deterministic key carries the same `data-slot-key`, and `querySelector`
    // returns whichever sorts first in the DOM, so a reveal aimed at the local
    // session could scroll to the peer's row instead. `slot` above is resolved
    // from the LOCAL `slots` prop, so its identity is the right target.
    runReveal('session', key, sessionRowIdentity(slot))
  }, [revealRequest, dispatch, localSlots, revealBlockingFilters, expandFolderAncestors, expandConductorAncestors, runReveal, isStaleExempt, slotFolders, staleCollapseMs, sortKey, setStaleExpanded])
  // ── Reveal a FOLDER row ───────────────────────────────────────────────────
  // The folder twin of the session reveal above, driven by the command launcher's
  // Folders group and the palette's Folders tab ("search a folder, land on it").
  // Same store-held request + replay guarantee, because either surface can be opened
  // from a page where this sidebar is not mounted at all. It reads the SAME
  // `revealRequest` field and answers only for `kind === 'folder'`; the effects stay
  // separate because this one clears the text filter and the folder hides, which the
  // session effect must not name (it consults the filter registry instead --
  // ChatSidebar.revealFilterDimensions pins that).
  useEffect(() => {
    if (!revealRequest) return
    if (revealRequest.kind !== 'folder') return
    const folderId = revealRequest.target
    // Consume immediately: the request must not survive to a later remount.
    dispatch(clearSlotReveal())
    const target = folders.find(f => f.id === folderId)
    if (!target) {
      // Deleted folder, or a request that outlived the folder list it named.
      // eslint-disable-next-line no-console -- a reveal's only success signal is the scroll+flash, so this early return is the one path where the user's click provably did nothing and nothing else records it
      console.debug('reveal-in-sidebar: no folder for id', folderId)
      return
    }
    // A text filter hides every folder whose subtree has no match, so a reveal
    // arriving while the box holds an unrelated query would scroll to nothing.
    // Clearing it is the same "reveal means show me this row" rule the session
    // reveal applies to its own filter dimensions.
    setSlotFilter('')
    // Un-hide the folder and its ancestors from the flat lane. Hiding a parent
    // hides the subtree (`filterHiddenSubtree`), so clearing only the target
    // itself would leave it hidden behind an ancestor.
    const chain = new Set<string>()
    for (let cur: ChatFolder | undefined = target, guard = 0; cur && guard < folders.length + 1; guard += 1) {
      chain.add(cur.id)
      cur = cur.parent_id ? folders.find(f => f.id === cur!.parent_id) : undefined
    }
    setFilterHiddenFolders(prev => {
      if (![...chain].some(id => prev.has(id))) return prev
      const next = new Set(prev)
      for (const id of chain) next.delete(id)
      safeSetItem(HIDDEN_FOLDERS_LS_KEY, JSON.stringify([...next]))
      return next
    })
    // "Hide when empty" is a second, independent reason a row can be absent, and it
    // is a SERVER field: an empty hidden folder is dropped from the lane with no
    // disclosure row listing it, so there is nothing in the DOM for the retry loop
    // to find. It is force-shown for this reveal instead of being un-hidden on the
    // server. The rule still describes what the user wants on their next visit --
    // they asked to see this folder now, not to stop hiding it -- so a persisted
    // write would answer a question they did not ask, and could not be undone from
    // the row it reveals.
    setRevealForcedVisible(chain)
    // Expand the folder and every collapsed ancestor. The folder ITSELF opening is
    // part of the destination here: landing on a folder means seeing what is in it.
    expandFolderAncestors(folderId)
    // A folder row only exists in the tree lane -- the flat lane renders sessions
    // with no folder blocks at all -- so a reveal has to leave it. Deliberately
    // WITHOUT writing `FLAT_VIEW_LS_KEY`: the lane is a persisted preference, and a
    // flat-lane user who jumps to one folder has asked to see that folder, not to
    // change which lane they open the app in. The switch lasts for this visit and
    // their preference comes back on reload.
    setFlatView(false)
    runReveal('folder', folderId, folderId)
  }, [revealRequest, dispatch, folders, expandFolderAncestors, runReveal, setSlotFilter, setFilterHiddenFolders, setFlatView, setRevealForcedVisible])
  return { revealFlash }
}
