import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, Info, Layers, PencilLine, Repeat2, Send, Workflow, X } from 'lucide-react'
import { api, type DashboardLineage, type DashboardLineageHop } from '../../api/client'
import { refreshDashboardReads } from '../../api/dashboardRefresh'
import { Btn, IconButton, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { MEMBER_DASHBOARD_QUERY_PREFIX, subscribeDashboardFields } from '../../hooks/useWebSocket'
import { isEditableElement } from '../../utils/editableTarget'
import { timeAgo } from '../../utils/timeAgo'
import { fmtDateTime } from '../../i18n/format'
import {
  ACTIVATE_MESSAGE_TYPE, EDIT_KEY_MESSAGE_TYPE, EDIT_MESSAGE_TYPE, ESCAPE_MESSAGE_TYPE, FOCUS_SEQ, POINT_MESSAGE_TYPE, WHEEL_MESSAGE_TYPE,
  CORRECTION_REASON_LIMIT, HOVER_DWELL_MS, HOVER_LEAVE_MS, TAP_SLOP_PX, declaredRoot, holdDuration, manifestFields, parseCorrection, pickedMessage, placePopover, popoverMaxHeight, popoverWidth, scriptLabel, shownValue,
  type ChangeRequestOutcome, type FieldSource, type Picked, type Rect,
} from './dashboardEdit'
import { changeRequestMessage } from './dashboardChangeRequest.prompt'

/** The page's readiness beacon. Mirrors `dashboard_frame.READY_MESSAGE_TYPE`. */
const READY_MESSAGE_TYPE = 'kirocrew-dashboard:ready'

/** True when *el* is kept mounted but not shown: under a `hidden` ancestor, or under
 *  one laid out as `display: none` (the side panel collapses that way while a live
 *  app tab keeps it mounted). A Dashboard nobody can see hears no shortcut keys, so
 *  a key pressed elsewhere cannot close, and so drop, a draft in it. */
function hiddenFromView(el: Element | null | undefined): boolean {
  for (let node = el; node; node = node.parentElement) {
    if ((node as HTMLElement).hidden || getComputedStyle(node).display === 'none') return true
  }
  return false
}
/** Popover size used for placement before it has measured itself. */
const POPOVER_SIZE = { w: 360, h: 380 }

/** `true` while the calling component is mounted: the fence for a completion that
 *  can land after the box that started it is gone. */
function useAlive(): { current: boolean } {
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  return alive
}

/**
 * THE UNSENT DRAFTS LIVE WITH THE HOST, NOT IN THIS LAYER.
 *
 * The dashboard unmounts this layer whenever the page under it is replaced: a Bot
 * applying a package (or moving to another one) clears the page on screen and shows
 * the loading skeleton, and a withdrawn script grant makes the page uneditable. A
 * request or correction typed into local state would be gone with it. So the open
 * pick, the popover's Edit mode and every typed draft are also written to *drafts*, a
 * map the dashboard owns, and a remounted layer reopens on them. A draft the person
 * closes is dropped from the map, so a later open still starts empty.
 */
export type EditDrafts = Map<string, unknown>
const DraftsContext = createContext<EditDrafts | null>(null)
/** Separates the parts of a draft key; a field path never holds it. */
const NS = '\u0000'
/**
 * NO NOTICE IN THIS LAYER OFFERS THE "ASK THE AGENT" HAND-OFF. The layer sits on the
 * Members page, beside the crewmate's DM composer, the Profile editor and the crew
 * editor, each of which keeps an unsent draft in local state. The hand-off navigates
 * to /chat, unmounting that page and its drafts, so every notice here withholds it,
 * as the rest of the Dashboard tab's notices do. Each notice below says so where it
 * stands ("No hand-off").
 */
const popoverScope = (path: string) => `popover${NS}${path}${NS}`
const flipScope = (flip: { block: string; path: string }) => `flip${NS}${flip.block}${NS}${flip.path}${NS}`

/** `useState` whose value is also kept in *drafts* under *key*, and read back from it
 *  on mount (through *restore*).
 *
 *  A WRITE AFTER UNMOUNT LANDS ONLY IN ITS OWN DRAFT. Each kept value carries an owner
 *  token beside it (`<key>#owner`): a remount of the same draft inherits the token, and
 *  a draft opened after its person closed the last one (which drops both) mints a new
 *  one. A late reply from the closed draft then finds another owner, or none, and is
 *  dropped, so it never overwrites what was typed since. */
let keptOwners = 0
function useKeptIn<T>(drafts: EditDrafts | null, key: string, initial: T, restore?: (kept: T) => T): [T, (next: T) => void] {
  const ownerKey = `${key}#owner`
  const [owner] = useState(() => (drafts?.get(ownerKey) as number | undefined) ?? ++keptOwners)
  const [value, setValue] = useState<T>(() => {
    if (!drafts?.has(key)) return initial
    const kept = drafts.get(key) as T
    return restore ? restore(kept) : kept
  })
  const alive = useAlive()
  const set = useCallback((next: T) => {
    if (alive.current) {
      drafts?.set(key, next)
      drafts?.set(ownerKey, owner)
      setValue(next)
    } else if (drafts?.get(ownerKey) === owner) {
      drafts.set(key, next)
    }
  }, [drafts, key, ownerKey, owner, alive])
  return [value, set]
}

function useKept<T>(key: string, initial: T, restore?: (kept: T) => T): [T, (next: T) => void] {
  return useKeptIn(useContext(DraftsContext), key, initial, restore)
}

/** Drop every kept draft under *prefix*, except those under *except*. */
function dropDrafts(drafts: EditDrafts | null | undefined, prefix: string, except?: string | null): void {
  if (!drafts) return
  for (const key of [...drafts.keys()]) {
    if (key.startsWith(prefix) && !(except && key.startsWith(except))) drafts.delete(key)
  }
}

/** One value's lineage, keyed under the Bot's dashboard read; no *path* is all of them. */
function lineageQueryKey(slug: string, member: string, path?: string): unknown[] {
  return [MEMBER_DASHBOARD_QUERY_PREFIX, slug, member, 'lineage', ...(path === undefined ? [] : [path])]
}

/** Post to the dashboard frame. `'*'` because its origin is opaque (see CrewDynamicDashboard). */
function postToFrame(frame: HTMLIFrameElement | null, message: Record<string, unknown>): void {
  // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
  frame?.contentWindow?.postMessage(message, '*')
}

/** What the person is looking at: a value from the page, or one picked from Sources. */
/** The page a selection was made on. `page` names it for the gateway
 *  (`package:<slug>` or `template:<id>`): a version alone does not. */
interface PageCtx { board: string; page: string; version: number }

interface Target {
  path: string
  rect: Rect
  field: FieldSource | null
  block: string
  /** Taken when picked, so a draft keeps naming the page version it was made on. */
  ctx: PageCtx
}

/** A component the person flipped over. */
interface Flip {
  block: string
  rect: Rect
  fields: FieldSource[]
  path: string
  /** Where the card turns over from: the open popover, else the component itself. */
  from: Rect
  /** Taken when opened, so a draft keeps naming the page version it was made on. */
  ctx: PageCtx
}

/**
 * Dashboard edit mode, drawn by the host over the Bot's sandboxed page
 * (preview `dashboard_edit_mode`).
 *
 * The page runs on an opaque origin, so the host never reads its DOM. In Edit mode a
 * capture layer over the frame takes the pointer and posts each point in; the page's
 * bootstrap answers with the field path under it (`dashboard_frame._EDIT_BRIDGE_JS`).
 * The host checks that path against the manifest it already holds and asks the gateway
 * for the lineage. Every word in the popover, the flipped card and Sources is the
 * gateway's or the host's, never the page's.
 *
 * Only the person can send a change request or correct a value: both are host UI, and
 * the page has no message that reaches either.
 */
export default function CrewDashboardEditLayer({ frameRef, frameKey, slug, member, botName, manifest, board, page, instanceVersion, onSend, onDone, paused = false, drafts = null }: {
  frameRef: RefObject<HTMLIFrameElement | null>
  /** Changes when the frame mounts a new document, so Edit mode is posted to it again. */
  frameKey: string
  slug: string
  member: string
  botName: string
  manifest: unknown
  /** The page's name in a change request: its template id or package slug. */
  board: string
  /** The page on screen as the gateway names it: `package:<slug>` or `template:<id>`. */
  page: string
  instanceVersion: number
  /** Send the person's text as their own chat message to this Bot. Resolves whether it went. */
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onDone: () => void
  /** An older page is kept on screen while a newer version waits: the gateway traces
   *  against the newer manifest, so no new value is picked and no lineage is re-read.
   *  What is open stays open, drafts included. */
  paused?: boolean
  /** The host's copy of the open pick and its drafts, which outlives this layer. */
  drafts?: EditDrafts | null
}) {
  const { t } = useTranslation()
  const reduced = useReducedMotion()
  const fields = useMemo(() => manifestFields(manifest as Record<string, unknown>), [manifest])
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const [box, setBox] = useState<Rect | null>(null)
  const [hover, setHover] = useState<Picked | null>(null)
  const [target, setTarget] = useKeptIn<Target | null>(drafts, 'target', null)
  const [refused, setRefused] = useState<string | null>(null)
  const [flip, setFlip] = useKeptIn<Flip | null>(drafts, 'flip', null)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  // A pick closed or replaced takes its drafts with it; the one open keeps its own.
  const openPopover = target && !flip ? popoverScope(target.path) : null
  useEffect(() => { dropDrafts(drafts, `popover${NS}`, openPopover) }, [drafts, openPopover])
  const openFlip = flip ? flipScope(flip) : null
  useEffect(() => { dropDrafts(drafts, `flip${NS}`, openFlip) }, [drafts, openFlip])
  // THE PAGE A SELECTION WAS MADE ON: the Bot can apply a new version while a draft is
  // open, and the layer stays mounted, so each selection copies this when it is made.
  const pageRef = useRef<PageCtx>({ board, page, version: instanceVersion })
  useLayoutEffect(() => { pageRef.current = { board, page, version: instanceVersion } }, [board, page, instanceVersion])
  const seqRef = useRef(0)
  const rafRef = useRef(0)
  /** True while the open popover's Edit is open: hover then leaves it alone. */
  const pinnedRef = useRef(false)
  const escRef = useRef<(() => boolean) | null>(null)
  const editKeyRef = useRef<(() => void) | null>(null)
  /** True while the pointer is inside the open popover. */
  const occupiedRef = useRef(false)
  const onPinned = useCallback((pinned: boolean) => {
    pinnedRef.current = pinned
    // Opening Edit cancels a dismissal already on its way, and a hover still dwelling
    // that would land after the pin and swap the popover out.
    if (pinned) { window.clearTimeout(leaveRef.current); window.clearTimeout(dwellRef.current) }
  }, [])
  /** Close the popover after the leave delay, unless by then it is pinned or the
   *  pointer is inside it. A late "nothing under the pointer" reply from the frame
   *  can arrive after the pointer entered the popover, so both are asked when the
   *  timer fires, not only when it is set. */
  const dismissLater = useCallback(() => {
    window.clearTimeout(leaveRef.current)
    leaveRef.current = window.setTimeout(() => {
      if (pinnedRef.current || occupiedRef.current) return
      setTarget(null)
      setRefused(null)
    }, HOVER_LEAVE_MS)
  }, [setTarget])
  /** An editor opened by a key pressed INSIDE the frame took the focus from it, so
   *  closing that editor hands the focus back to the value the person was on. */
  const fromFrameRef = useRef(false)
  const restoreFocus = useCallback(() => {
    if (!fromFrameRef.current) return
    fromFrameRef.current = false
    frameRef.current?.focus()
  }, [frameRef])
  const escape = useCallback(() => {
    if (escRef.current?.()) { restoreFocus(); return }
    occupiedRef.current = false
    setTarget(null)
    setFlip(null)
    setRefused(null)
    setSourcesOpen(false)
    restoreFocus()
  }, [restoreFocus, setTarget, setFlip])
  const dwellRef = useRef<number | undefined>(undefined)
  const leaveRef = useRef<number | undefined>(undefined)
  /** Activation as of the last render: of the current hover (the keyboard's), and of
   *  a given pick (a tap's answer). */
  const activateRef = useRef<() => void>(() => {})
  const activateAtRef = useRef<(at: Picked | null) => void>(() => {})
  /** The point request a tap is waiting on, or `null`. */
  const tapSeqRef = useRef<number | null>(null)

  // THE FRAME'S BOX inside the tab, so frame coordinates and host coordinates meet.
  // A passive effect: the frame renders after this layer, and a new frameKey's iframe
  // is only on the ref once every layout effect and ref of the commit has run.
  useEffect(() => {
    const frame = frameRef.current
    if (!frame) return
    const measure = () => setBox({ x: frame.offsetLeft, y: frame.offsetTop, w: frame.offsetWidth, h: frame.offsetHeight })
    measure()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    ro?.observe(frame)
    if (frame.parentElement) ro?.observe(frame.parentElement)
    window.addEventListener('resize', measure)
    return () => { ro?.disconnect(); window.removeEventListener('resize', measure) }
  }, [frameRef, frameKey])

  // EDIT ON for this document, and again for each document the frame mounts.
  useEffect(() => {
    postToFrame(frameRef.current, { type: EDIT_MESSAGE_TYPE, on: true })
    const frame = frameRef.current
    return () => postToFrame(frame, { type: EDIT_MESSAGE_TYPE, on: false })
  }, [frameRef, frameKey])

  const pausedRef = useRef(paused)
  useLayoutEffect(() => { pausedRef.current = paused }, [paused])
  const showPicked = useCallback((picked: Picked) => {
    if (pinnedRef.current) return
    window.clearTimeout(dwellRef.current)
    if (!picked.path || !picked.rect) {
      dismissLater()
      return
    }
    window.clearTimeout(leaveRef.current)
    const path = picked.path
    const rect = picked.rect
    dwellRef.current = window.setTimeout(() => {
      if (pinnedRef.current || pausedRef.current) return
      const field = declaredRoot(path, fields)
      if (!field) {
        // THE PAGE POINTED AT A FIELD IT DOES NOT DECLARE: said, and nothing traced.
        setTarget(null)
        setRefused(path)
        return
      }
      setRefused(null)
      setTarget({ path, rect, field, block: picked.block, ctx: pageRef.current })
    }, HOVER_DWELL_MS)
  }, [fields, dismissLater, setTarget])

  // THE FRAME'S ANSWERS. Only from this frame's window; a stale point's answer is dropped.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const win = frameRef.current?.contentWindow
      if (!win || event.source !== win) return
      const kind = (event.data ?? {}) as { type?: unknown }
      if (kind.type === READY_MESSAGE_TYPE) {
        postToFrame(frameRef.current, { type: EDIT_MESSAGE_TYPE, on: true })
        return
      }
      // A FRAME MESSAGE NEVER DISCARDS A DRAFT. The page's own script posts from the
      // same window as the bridge, so the host cannot tell the two apart; a frame Esc,
      // click or Edit key therefore acts only while nothing typed can be lost: no flip
      // card open and no Edit open in the popover (`pinnedRef`). With a draft open,
      // only the person's own key or click in the host closes or replaces it.
      const draftOpen = pinnedRef.current || flip !== null
      if (kind.type === ESCAPE_MESSAGE_TYPE) { if (!draftOpen) escape(); return }
      // THE KEYBOARD'S CLICK: Enter or Space on the value focus picked opens its
      // component, through the same handler the pointer's click uses.
      // A key in the frame opens a host editor, which takes the focus (FlipCard and the
      // popover's Edit focus themselves), so the person's next Esc reaches this window.
      if (kind.type === ACTIVATE_MESSAGE_TYPE) { if (!draftOpen) { fromFrameRef.current = true; activateRef.current() } return }
      if (kind.type === EDIT_KEY_MESSAGE_TYPE) { if (!draftOpen && editKeyRef.current) { fromFrameRef.current = true; editKeyRef.current() } return }
      const picked = pickedMessage(event.data)
      if (!picked) return
      if (picked.seq === tapSeqRef.current) {
        // The answer for a tap: open what is under it. Checked before the stale-hover
        // test below, since a pointer move, leave or scroll after the click asks again
        // and advances `seqRef` while this answer is in flight. Consumed either way,
        // but an answer that lands after the person opened Ask or Correct opens
        // nothing: the card or popover it would open replaces, and drops, what they typed.
        tapSeqRef.current = null
        if (picked.seq === seqRef.current) setHover(picked)
        if (!draftOpen) activateAtRef.current(picked)
        return
      }
      if (picked.seq !== FOCUS_SEQ && picked.seq !== seqRef.current) return
      setHover(picked)
      if (!flip) showPicked(picked)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [frameRef, escape, showPicked, flip])

  useEffect(() => {
    // A HIDDEN DASHBOARD HEARS NO KEYS: the side panel keeps a visited Dashboard
    // mounted behind another tab, or collapsed, so its drafts survive, and an Escape
    // pressed elsewhere must not close, and so drop, a draft nobody can see.
    // An Escape typed in a text box outside this layer (the chat composer beside the
    // Dashboard) is that box's, as the E shortcut below already treats it, and one
    // pressed inside another dialog (a Modal lets Escape reach window) is that dialog's.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || hiddenFromView(overlayRef.current)) return
      const t = e.target instanceof Element ? e.target : null
      const outside = !overlayRef.current?.contains(t)
      if (outside && (isEditableElement(e.target) || t?.closest('[role="dialog"], [role="alertdialog"], [aria-modal="true"]'))) return
      escape()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [escape])

  useEffect(() => () => {
    window.clearTimeout(dwellRef.current)
    window.clearTimeout(leaveRef.current)
    cancelAnimationFrame(rafRef.current)
  }, [])

  const point = (x: number | null, y: number | null) => {
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(() => {
      seqRef.current += 1
      postToFrame(frameRef.current, { type: POINT_MESSAGE_TYPE, seq: seqRef.current, x, y })
    })
  }
  const local = (e: { clientX: number; clientY: number }) => {
    const r = frameRef.current?.getBoundingClientRect()
    return r ? { x: e.clientX - r.left, y: e.clientY - r.top } : null
  }

  // A SCROLL MOVES THE PAGE UNDER THE POINTER. The pending dwell and an unpinned
  // lineage name what was there, so both go and the frame is asked again; the point
  // request follows the scroll message, so the frame answers after it has scrolled.
  // An open draft (Edit pinned, or a flipped card) stays where it is. A ctrl-wheel
  // zooms a chart and moves nothing, so it keeps the pick.
  const scrollFrame = (dy: number, p: { x: number; y: number } | null, ctrl: boolean) => {
    postToFrame(frameRef.current, { type: WHEEL_MESSAGE_TYPE, dy, x: p?.x, y: p?.y, ctrl })
    if (ctrl || pinnedRef.current || flip) return
    window.clearTimeout(dwellRef.current)
    setTarget(null)
    if (p) point(p.x, p.y)
  }

  // A TOUCH DRAG SCROLLS THE PAGE. The capture layer covers the frame, so the
  // browser cannot pan it: a vertical drag is sent as the same scroll a wheel sends,
  // and a drag past the slop is not then read as a tap.
  const dragRef = useRef<{ id: number; y: number; moved: number } | null>(null)
  const draggedRef = useRef(false)

  /** The open popover's box inside the overlay, the card's start when it is showing. */
  const popoverBox = (): Rect | null => {
    const overlay = overlayRef.current
    const el = overlay?.querySelector<HTMLElement>('[data-testid="lineage-popover"]')
    if (!overlay || !el) return null
    const a = el.getBoundingClientRect()
    const b = overlay.getBoundingClientRect()
    return a.width && a.height ? { x: a.left - b.left, y: a.top - b.top, w: a.width, h: a.height } : null
  }

  /** Open the component (or the value) *at* names: the frame's answer for a point. */
  const activate = (at: Picked | null) => {
    if (!at || pausedRef.current) return
    const shown = popoverBox()
    if (at.block && at.blockRect) {
      const named = at.blockFields.map(name => fields.find(f => f.name === name)).filter((f): f is FieldSource => !!f)
      const picked = at.path ? declaredRoot(at.path, fields) : null
      const list = named.length ? named : picked ? [picked] : []
      setTarget(null)
      setFlip({ block: at.block, rect: at.blockRect, fields: list, path: at.path ?? '', from: shown ?? at.blockRect, ctx: pageRef.current })
      return
    }
    if (at.path && at.rect) {
      const field = declaredRoot(at.path, fields)
      if (field) setFlip({ block: '', rect: { x: at.rect.x - 8, y: at.rect.y - 8, w: Math.max(320, at.rect.w + 16), h: 240 }, fields: [field], path: at.path, from: shown ?? at.rect, ctx: pageRef.current })
    }
  }

  // A TAP PICKS ITS OWN POINT. A touch, or a click with no move before it, has no
  // hover yet, and a hover answer still in flight names the previous spot. So the
  // tap asks the frame what is under it and opens that once the matching answer
  // arrives (`tapSeqRef`), never the cached hover.
  const onTap = (e: { clientX: number; clientY: number }) => {
    const p = local(e)
    if (!p) return
    cancelAnimationFrame(rafRef.current)
    seqRef.current += 1
    tapSeqRef.current = seqRef.current
    postToFrame(frameRef.current, { type: POINT_MESSAGE_TYPE, seq: seqRef.current, x: p.x, y: p.y })
  }

  // The keyboard activates what focus picked, which is the current hover.
  useLayoutEffect(() => {
    activateRef.current = () => activate(hover)
    activateAtRef.current = activate
  })

  return (
    <DraftsContext.Provider value={drafts}>
      <div className="shrink-0 border-b border-border bg-accent-subtle px-3 py-2 flex items-center gap-2 text-[13px]" data-testid="dashboard-edit-bar" role="status">
        <Info size={14} className="shrink-0 text-accent" aria-hidden />
        <span className="flex-1 min-w-0 text-text">
          <strong className="font-semibold">{t('pages.membersPage.dashboardEdit.mode')}</strong>{' '}
          {t('pages.membersPage.dashboardEdit.bar', { bot: botName })}
        </span>
        <Btn onClick={() => setSourcesOpen(o => !o)} aria-expanded={sourcesOpen} data-testid="dashboard-edit-sources-button" className="shrink-0">
          <Layers size={13} className="lucide-inline" aria-hidden />
          {t('pages.membersPage.dashboardEdit.sources')}
          <span className="ml-1 text-muted tabular-nums">{fields.length}</span>
        </Btn>
        <Btn primary onClick={onDone} data-testid="dashboard-edit-done" className="shrink-0">
          {t('pages.membersPage.dashboardEdit.done')}
        </Btn>
      </div>
      {box && (
        <div ref={overlayRef} className="absolute z-20 pointer-events-none" style={{ left: box.x, top: box.y, width: box.w, height: box.h }} data-testid="dashboard-edit-overlay">
          {/* THE CAPTURE LAYER: the pointer cannot be seen over a cross-origin frame. */}
          {/* The pointer's half only: a keyboard focuses a value inside the frame (the
              bridge makes each one tabbable), and Enter or Space there posts
              ACTIVATE_MESSAGE_TYPE, which opens the same component a tap opens. */}
          <div
            role="presentation"
            className="absolute inset-0 pointer-events-auto cursor-crosshair"
            style={{ touchAction: 'none' }}
            data-testid="dashboard-edit-capture"
            onPointerDown={e => {
              draggedRef.current = false
              dragRef.current = e.pointerType === 'mouse' ? null : { id: e.pointerId, y: e.clientY, moved: 0 }
            }}
            onPointerMove={e => {
              const drag = dragRef.current
              if (drag && drag.id === e.pointerId) {
                const dy = drag.y - e.clientY
                drag.y = e.clientY
                drag.moved += Math.abs(dy)
                if (drag.moved > TAP_SLOP_PX) {
                  draggedRef.current = true
                  scrollFrame(dy, local(e), false)
                }
                return
              }
              const p = local(e)
              if (p) point(p.x, p.y)
            }}
            onPointerUp={e => { if (dragRef.current?.id === e.pointerId) dragRef.current = null }}
            onPointerCancel={() => { dragRef.current = null }}
            onPointerLeave={() => point(null, null)}
            onWheel={e => scrollFrame(e.deltaY, local(e), e.ctrlKey || e.metaKey)}
            onClick={e => {
              if (draggedRef.current) { draggedRef.current = false; return }
              onTap(e)
            }}
          />
          {hover?.block && hover.blockRect && !flip && (
            <div
              aria-hidden
              className="absolute rounded-lg border-2 border-dashed border-accent/70 bg-accent/5"
              style={{ left: hover.blockRect.x, top: hover.blockRect.y, width: hover.blockRect.w, height: hover.blockRect.h }}
            />
          )}
          {refused && hover?.rect && (
            <div className="absolute pointer-events-auto rounded-md bg-bg-elevated px-2 py-1 shadow" style={{ left: hover.rect.x, top: hover.rect.y + hover.rect.h + 6 }}>
              {/* A HINT, not an error: the host checked the page's own markup against
                  its declared fields and asked nobody, so nothing failed. */}
              <p className="m-0 text-[12px] text-muted" role="status" data-testid="dashboard-edit-refused">{t('pages.membersPage.dashboardEdit.refused', { path: refused })}</p>
            </div>
          )}
          {target && !flip && (
            <LineagePopover
              key={target.path}
              target={target}
              bounds={box}
              slug={slug}
              member={member}
              botName={botName}
              reduced={reduced}
              ctx={target.ctx}
              onSend={onSend}
              onEnter={() => { occupiedRef.current = true; window.clearTimeout(leaveRef.current) }}
              onLeave={() => { occupiedRef.current = false; dismissLater() }}
              onClose={() => { occupiedRef.current = false; setTarget(null); restoreFocus() }}
              onPinned={onPinned}
              escRef={escRef}
              editKeyRef={editKeyRef}
              paused={paused}
              shownVersion={instanceVersion}
              shownPage={page}
            />
          )}
          {flip && (
            <FlipCard
              // One card per component: switching components mounts a fresh card,
              // so an earlier submission's lifetime never reaches the new one.
              key={`${flip.block}\u0000${flip.path}`}
              flip={flip}
              bounds={box}
              botName={botName}
              reduced={reduced}
              ctx={flip.ctx}
              onSend={onSend}
              onClose={() => { setFlip(null); restoreFocus() }}
            />
          )}
          {sourcesOpen && (
            <SourcesPanel
              fields={fields}
              botName={botName}
              // A pick never replaces an open draft: switching would remount the
              // popover and drop it. The person closes Edit first, as with a frame pick.
              onPick={(field, rect) => { if (!pinnedRef.current && !flip && !pausedRef.current) setTarget({ path: field.name, rect, field, block: '', ctx: pageRef.current }) }}
              onClose={() => setSourcesOpen(false)}
            />
          )}
        </div>
      )}
    </DraftsContext.Provider>
  )
}

/** Which part of the popover's Edit is open. */
type EditMode = 'none' | 'menu' | 'ask' | 'correct' | 'source'

/** One Edit option that does not apply here, with the one-line reason. */
function Greyed({ label, reason, testId }: { label: string; reason: string; testId: string }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-2.5 py-1.5 opacity-70" data-testid={testId} aria-disabled="true">
      {/* Dimmed, never struck through: a struck-out option reads as deleted, and
          this one is only unavailable here, for the reason under it. */}
      <div className="text-[12.5px] text-muted">{label}</div>
      <div className="text-[11.5px] text-muted">{reason}</div>
    </div>
  )
}

/**
 * Edit on one value, by what kind of value it is (the demo's table):
 * - any value: ask the Bot to change it;
 * - a value the Bot writes: correct it (not a whole list or record);
 * - a script's record: the job behind it, Run now (asked first), Open Schedule;
 * - a folded value: ask the Bot to change how it is shown.
 * An option that does not apply is shown greyed with its reason.
 */
function EditPanel({ lineage, mode, setMode, slug, member, botName, ctx, target, onSend, onClose, paused = false, shownVersion, shownPage }: {
  paused?: boolean
  shownVersion?: number
  shownPage?: string
  lineage: DashboardLineage
  mode: EditMode
  setMode: (m: EditMode) => void
  slug: string
  member: string
  botName: string
  ctx: PageCtx
  target: Target
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onClose: () => void
}) {
  const { t } = useTranslation()
  const edit = lineage.edit
  // The declared type the open correction was started for, held across refreshes.
  const scope = `${popoverScope(target.path)}draft${NS}`
  const [correctType, setCorrectType] = useKept<string | null>(`${scope}correctType`, null)
  const ask = edit.kind === 'fold' ? t('pages.membersPage.dashboardEdit.change_shown', { bot: botName }) : t('pages.membersPage.dashboardEdit.ask', { bot: botName })
  if (mode === 'ask') return <AskBox compact botName={botName} ctx={ctx} block={target.block} path={target.path} onSend={onSend} onSent={onClose} keepAs={`${scope}ask${NS}`} />
  // THE OPEN CORRECTION OUTLIVES A REFRESH: the Bot can rebind the field while a
  // person types (a new page makes it counted or script-written), and the refreshed
  // lineage then offers no correction. The box stays with its draft, Save off and the
  // reason said, until the person closes it.
  if (mode === 'correct' && correctType !== null) {
    // Paused: a newer page waits, and the field may mean something else there. The draft
    // stays; Save waits for the newer page, and the gateway refuses an older version too.
    const blocked = paused ? t('pages.membersPage.dashboardEdit.paused_newer_page')
      : edit.kind === 'correct' ? undefined
        : edit.kind === 'fold' ? t('pages.membersPage.dashboardEdit.no_edit_fold', { bot: botName })
          : t('pages.membersPage.dashboardEdit.no_edit_cron')
    // The type the refreshed lineage gives the field now; confirming adopts it.
    const shownType = edit.kind === 'correct' ? edit.type : undefined
    return <CorrectBox slug={slug} member={member} botName={botName} field={lineage.field} type={correctType} shownType={shownType} onAdoptType={setCorrectType} blocked={blocked} typedOn={{ page: ctx.page, version: ctx.version }} shown={{ page: shownPage ?? ctx.page, version: shownVersion ?? ctx.version }} onDone={() => setMode('none')} keepAs={`${scope}correct${NS}`} />
  }
  // Keyed by job: a refresh that names another job starts a fresh box, so a confirm
  // or run result given for one job never applies to the next.
  if (mode === 'source' && edit.kind === 'source') return <SourceBox key={edit.job} job={edit.job} lineage={lineage} />
  return (
    <div className="space-y-1.5" data-testid="lineage-edit-menu">
      <Btn onClick={() => setMode('ask')} data-testid="lineage-ask" className="w-full justify-start">{ask}</Btn>
      {edit.kind === 'correct' && (edit.scalar
        ? <Btn onClick={() => { setCorrectType(edit.type); setMode('correct') }} data-testid="lineage-correct" className="w-full justify-start">{t('pages.membersPage.dashboardEdit.correct')}</Btn>
        : <Greyed testId="lineage-correct-greyed" label={t('pages.membersPage.dashboardEdit.correct')} reason={t('pages.membersPage.dashboardEdit.no_correct_shape', { bot: botName })} />)}
      {edit.kind === 'correct' && <Greyed testId="lineage-source-greyed" label={t('pages.membersPage.dashboardEdit.edit_source')} reason={t('pages.membersPage.dashboardEdit.no_source_agentic', { bot: botName })} />}
      {edit.kind === 'source' && (edit.job
        ? <Btn onClick={() => setMode('source')} data-testid="lineage-source" className="w-full justify-start">{t('pages.membersPage.dashboardEdit.edit_source')}</Btn>
        : <Greyed testId="lineage-source-greyed" label={t('pages.membersPage.dashboardEdit.edit_source')} reason={t('pages.membersPage.dashboardEdit.no_source_job')} />)}
      {edit.kind === 'source' && <Greyed testId="lineage-correct-greyed" label={t('pages.membersPage.dashboardEdit.correct')} reason={t('pages.membersPage.dashboardEdit.no_edit_cron')} />}
      {edit.kind === 'fold' && <Greyed testId="lineage-open-item-greyed" label={t('pages.membersPage.dashboardEdit.open_item')} reason={t('pages.membersPage.dashboardEdit.not_tracked')} />}
      {edit.kind === 'fold' && <Greyed testId="lineage-correct-greyed" label={t('pages.membersPage.dashboardEdit.correct')} reason={t('pages.membersPage.dashboardEdit.no_edit_fold', { bot: botName })} />}
    </div>
  )
}

/** The script job behind a record: what runs it, how long a record holds, how the last
 *  run went, and Run now, which asks first because a run costs one. */
function SourceBox({ job, lineage }: { job: string; lineage: DashboardLineage }) {
  const { t } = useTranslation()
  // Under ['crons'], so every cron refresh frame re-reads the job's last run here too.
  const { data, isError: cronsFailed } = useQuery({ queryKey: ['crons', 'dashboard-edit', job], queryFn: () => api.crons(), staleTime: 15_000 })
  const found = data?.jobs?.find(j => j.id === job)
  const freshness = lineage.hops.find(h => h.kind === 'freshness')
  const script = lineage.hops.find(h => h.kind === 'script')
  const [confirming, setConfirming] = useState(false)
  const [run, setRun] = useState<'idle' | 'running' | 'started' | 'failed'>('idle')
  // ONE RUN PER CONFIRM: a second click while the first is out would start nothing
  // (the gateway answers 409) and its late refusal would overwrite "started".
  const runningRef = useRef(false)
  const runNow = async () => {
    if (runningRef.current) return
    runningRef.current = true
    setRun('running')
    try { await api.runCron(job); setRun('started') } catch { setRun('failed') } finally { runningRef.current = false }
    setConfirming(false)
  }
  const row = (label: string, value: ReactNode) => (
    <div className="grid grid-cols-[6.5rem_1fr] gap-2"><dt className="text-muted">{label}</dt><dd className="m-0 min-w-0 break-words font-mono text-text">{value}</dd></div>
  )
  return (
    <div className="space-y-2" data-testid="lineage-source-box">
      {/* Named for what the person opened ("See the job behind this"): a read of the
          job, with Run now and Open Schedule as its only actions. */}
      <h3 className="m-0 text-[12.5px] font-semibold text-text-strong" data-testid="lineage-job-heading">{t('pages.membersPage.dashboardEdit.job_heading')}</h3>
      {/* A failed read of the schedules, said, so a blank row is not read as "no
          schedule". No hand-off: the Members page's DM composer, Profile and
          crew-editor drafts sit beside this tab, and the hand-off would drop them. */}
      {cronsFailed && <ErrorNotice variant="inline" message={t('pages.membersPage.schedule_load_failed')} testId="lineage-crons-failed" />}
      <dl className="m-0 space-y-1 rounded-lg border border-border p-2 text-[12px]">
        {row(t('pages.membersPage.dashboardEdit.src_job'), found?.name || job)}
        {row(t('pages.membersPage.dashboardEdit.src_schedule'), found?.schedule || '\u2014')}
        {row(t('pages.membersPage.dashboardEdit.src_max_age'), typeof freshness?.max_age_s === 'number' ? holdDuration(freshness.max_age_s) : '\u2014')}
        {row(t('pages.membersPage.dashboardEdit.src_script'), scriptLabel(String(script?.script || found?.script || '')) || '\u2014')}
        {row(t('pages.membersPage.dashboardEdit.src_last_run'), found?.last_error ? t('pages.membersPage.schedule_failed') : (found?.last_status || '\u2014'))}
      </dl>
      {/* The job's last run failed. No hand-off: the Members page's DM composer,
          Profile and crew-editor drafts would be dropped by it. */}
      {found?.last_error && <ErrorNotice variant="inline" message={found.last_error} testId="lineage-job-failed" />}
      {confirming ? (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-warn bg-warn-subtle px-2 py-1.5 text-[12px]" role="alertdialog" data-testid="lineage-run-confirm">
          <span className="flex-1">{t('pages.membersPage.dashboardEdit.run_confirm')}</span>
          <Btn onClick={() => setConfirming(false)}>{t('pages.membersPage.dashboardEdit.cancel')}</Btn>
          <Btn primary onClick={() => void runNow()} disabled={run === 'running'} data-testid="lineage-run-yes">{t('pages.membersPage.dashboardEdit.run_now')}</Btn>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Btn onClick={() => setConfirming(true)} disabled={run === 'running' || run === 'started'} data-testid="lineage-run">{t('pages.membersPage.dashboardEdit.run_now')}</Btn>
          <a href={`/schedule?job=${encodeURIComponent(job)}`} target="_blank" rel="noopener noreferrer" className="text-[12.5px] text-accent hover:underline" data-testid="lineage-open-schedule">{t('pages.membersPage.dashboardEdit.open_schedule')}</a>
          <span className="text-[11.5px] text-muted" role="status">
            {run === 'started' ? t('pages.membersPage.dashboardEdit.run_started') : ''}
          </span>
          {/* The run request failed. No hand-off: the Members page's DM composer,
              Profile and crew-editor drafts would be dropped by it. */}
          {run === 'failed' && <ErrorNotice variant="inline" message={t('pages.membersPage.dashboardEdit.run_failed')} testId="lineage-run-failed" />}
        </div>
      )}
    </div>
  )
}

/** The kind tag beside a field: where its value comes from, in one word. */
function KindTag({ kind, botName }: { kind: FieldSource['kind'] | DashboardLineage['kind']; botName: string }) {
  const { t } = useTranslation()
  return (
    <span className="shrink-0 rounded-full border border-accent/40 bg-accent-subtle px-2 py-px font-mono text-[11px] text-accent">
      {kind === 'agentic' ? t('pages.membersPage.dashboardEdit.tag_agentic', { bot: botName }) : kind === 'custom' ? t('pages.membersPage.dashboardEdit.tag_script') : t('pages.membersPage.dashboardEdit.tag_counted')}
    </span>
  )
}

/** A fold field's source as code: `fold → path`. */
function SourceLine({ field }: { field: FieldSource }) {
  return <>{field.fold} {'\u2192'} {field.path}</>
}

/** Where a value came from, in one plain sentence ahead of the steps behind it. */
function LineageSummary({ lineage, botName }: { lineage: DashboardLineage; botName: string }) {
  const { t } = useTranslation()
  const hop = (kind: string) => lineage.hops.find(h => h.kind === kind)
  const record = lineage.kind === 'custom' ? hop('record') : undefined
  const jobId = typeof record?.writer === 'string' && record.writer.startsWith('cron:') ? record.writer.slice('cron:'.length) : ''
  // The same read the job panel makes, so the two share one cached answer. A job the
  // read cannot name is shown by its id.
  const { data: crons, isError: cronsFailed } = useQuery({ queryKey: ['crons', 'dashboard-edit', jobId], queryFn: () => api.crons(), staleTime: 15_000, enabled: !!jobId })
  const jobName = jobId ? (crons?.jobs?.find(j => j.id === jobId)?.name || jobId) : ''
  const staleSince = staleSinceSecs(hop('freshness')?.stale_after)
  const when = (at: unknown) => {
    const ago = typeof at === 'string' && at ? timeAgo(Date.parse(at) / 1000) : '--'
    return ago === '--' ? '' : ago
  }
  let text: string
  if (lineage.kind === 'agentic') {
    const write = hop('write')
    if (!write || write.found === false) text = t('pages.membersPage.dashboardEdit.hop_write_missing')
    else if (write.actor === 'person') text = t('pages.membersPage.dashboardEdit.summary_person', { when: when(write.at) })
    else text = t('pages.membersPage.dashboardEdit.summary_bot', { bot: botName, when: when(write.at) })
  } else if (lineage.kind === 'custom') {
    const writer = jobName || (typeof record?.writer === 'string' ? record.writer : '')
    text = !record || record.found === false
      ? t('pages.membersPage.dashboardEdit.hop_record_missing')
      : t('pages.membersPage.dashboardEdit.summary_record', { writer: writer || botName, when: when(record.at) })
  } else {
    text = t('pages.membersPage.dashboardEdit.summary_fold', { bot: botName })
  }
  return (
    <>
      <p className="m-0 text-[13px] text-text-strong" data-testid="lineage-summary">{text}</p>
      {/* Past its stale limit: said next to the summary, so the number does not read
          as current. */}
      {staleSince !== null && (
        <p className="m-0 flex items-center gap-1 text-[12px] text-warn" data-testid="lineage-out-of-date">
          <AlertTriangle size={12} aria-hidden />{t('pages.membersPage.dashboardEdit.out_of_date', { when: timeAgo(staleSince) })}
        </p>
      )}
      {/* A failed schedule read, said, so the job id is not read as "no such job".
          No hand-off: this popover can hold an unsent Ask or Correct draft. */}
      {cronsFailed && <ErrorNotice variant="inline" message={t('pages.membersPage.schedule_load_failed')} testId="lineage-summary-crons-failed" />}
    </>
  )
}

/** A hop's time as the reader's local date, never the gateway's raw UTC ISO; `''` stays `''`. */
function when(iso: string): string {
  return iso ? fmtDateTime(iso) : ''
}

/** When a freshness hop's `stale_after` has already passed, in epoch seconds; else `null`. */
function staleSinceSecs(staleAfter: unknown): number | null {
  const at = typeof staleAfter === 'string' ? Date.parse(staleAfter) : NaN
  return Number.isFinite(at) && at <= Date.now() ? at / 1000 : null
}

/** One hop of a lineage: its label, what it says, and who vouches for it. */
function Hop({ hop, botName, index, reduced }: { hop: DashboardLineageHop; botName: string; index: number; reduced: boolean }) {
  const { t } = useTranslation()
  const s = (key: string) => (typeof hop[key] === 'string' ? (hop[key] as string) : '')
  let label = ''
  let body: ReactNode = null
  switch (hop.kind) {
    case 'page':
      label = t('pages.membersPage.dashboardEdit.hop_page')
      body = <><code className="text-accent">{s('path')}</code><Sub>{t('pages.membersPage.dashboardEdit.hop_page_note')}</Sub></>
      break
    case 'field':
      label = t('pages.membersPage.dashboardEdit.hop_field')
      body = <><span className="font-mono text-accent">{s('field')}</span><Sub>{t('pages.membersPage.dashboardEdit.hop_type', { type: s('type') })}</Sub></>
      break
    case 'fold':
      label = t('pages.membersPage.dashboardEdit.hop_counted')
      body = <><Sub><span className="font-mono">{s('fold')} {'\u2192'} {s('path')}</span></Sub>{hop.found === false && <Sub warn>{t('pages.membersPage.dashboardEdit.hop_not_counted')}</Sub>}</>
      break
    case 'crew_log':
      label = t('pages.membersPage.dashboardEdit.hop_crew_log')
      body = <Sub>{t('pages.membersPage.dashboardEdit.hop_crew_log_desc', { fold: s('fold') })}</Sub>
      break
    case 'record':
      label = t('pages.membersPage.dashboardEdit.hop_record')
      body = hop.found === false
        ? <Sub warn>{t('pages.membersPage.dashboardEdit.hop_record_missing')}</Sub>
        : <><span><code className="text-accent">{s('writer')}</code> {when(s('at'))}</span>{typeof hop.seq === 'number' && <Sub>{t('pages.membersPage.dashboardEdit.hop_seq', { seq: hop.seq })}</Sub>}</>
      break
    case 'script':
      label = t('pages.membersPage.dashboardEdit.hop_script')
      body = (
        <>
          <code className="text-accent break-all">{scriptLabel(s('script')) || s('job')}</code>
          <Sub>{[s('job'), when(s('run_at'))].filter(Boolean).join(' \u00b7 ')}</Sub>
        </>
      )
      break
    case 'source': {
      label = t('pages.membersPage.dashboardEdit.hop_source')
      const points = Array.isArray(hop.points) ? (hop.points as unknown[]).map(p => (Array.isArray(p) ? Number(p[1]) : NaN)).filter(Number.isFinite) : []
      const source = (hop.source && typeof hop.source === 'object' ? hop.source : {}) as Record<string, unknown>
      const url = typeof source.console_url === 'string' && source.console_url.startsWith('https://') ? source.console_url : ''
      body = (
        <>
          <dl className="m-0 mt-0.5 space-y-0.5 font-mono text-[11.5px]">
            {Object.entries(source).filter(([k]) => k !== 'console_url').map(([k, v]) => (
              <div key={k} className="flex gap-2"><dt className="shrink-0 text-muted">{k}</dt><dd className="m-0 min-w-0 break-all">{shownValue(v)}</dd></div>
            ))}
          </dl>
          {url && <a href={url} target="_blank" rel="noopener noreferrer" className="text-[11.5px] text-accent hover:underline">{t('pages.membersPage.dashboardEdit.open_console')}</a>}
          {points.length > 0 && <Spark points={points} />}
          <Sub>{t('pages.membersPage.dashboardEdit.cannot_check_source')}</Sub>
        </>
      )
      break
    }
    case 'freshness':
      label = t('pages.membersPage.dashboardEdit.hop_freshness')
      body = hop.needs ? null : (
        <span>
          {/* Past the limit it says so; before it, a local date, never the raw ISO. */}
          {staleSinceSecs(s('stale_after')) !== null
            ? <span className="text-warn">{t('pages.membersPage.dashboardEdit.out_of_date', { when: timeAgo(staleSinceSecs(s('stale_after')) as number) })}</span>
            : s('stale_after') ? t('pages.membersPage.dashboardEdit.stale_after', { at: fmtDateTime(s('stale_after')) })
              : t('pages.membersPage.dashboardEdit.no_stale_limit')}
          {/* Only a script declares its own hold; any other limit is the page's. */}
          {typeof hop.max_age_s === 'number' && <Sub>{hop.by === 'script' ? t('pages.membersPage.dashboardEdit.max_age', { duration: holdDuration(hop.max_age_s) }) : t('pages.membersPage.dashboardEdit.max_age_limit', { duration: holdDuration(hop.max_age_s) })}</Sub>}
        </span>
      )
      break
    case 'run':
      label = t('pages.membersPage.dashboardEdit.hop_run')
      // No hand-off: this popover can hold an unsent change request under Edit, which
      // the hand-off's navigation would drop.
      body = <ErrorNotice variant="inline" message={s('last_error') || when(s('failed_at'))} title={s('last_error') ? when(s('failed_at')) : undefined} testId="lineage-hop-run-failed" />
      break
    case 'write':
    case 'earlier_write':
      label = hop.kind === 'write' ? t('pages.membersPage.dashboardEdit.hop_write') : t('pages.membersPage.dashboardEdit.hop_earlier')
      body = hop.found === false
        ? <Sub warn>{t('pages.membersPage.dashboardEdit.hop_write_missing')}</Sub>
        : <><span>{hop.actor === 'person' ? t('pages.membersPage.dashboardEdit.by_person', { at: when(s('at')) }) : t('pages.membersPage.dashboardEdit.by_bot', { bot: botName, at: when(s('at')) })}</span>{s('reason') && <Sub>{'\u201c'}{s('reason')}{'\u201d'}</Sub>}</>
      break
    case 'looked_at':
      label = t('pages.membersPage.dashboardEdit.hop_looked_at', { bot: botName })
      break
  }
  return (
    <li
      className={`relative pl-5 pb-3 ${reduced ? '' : 'motion-safe:animate-rise'}`}
      style={reduced ? undefined : { animationDelay: `${index * 250}ms` }}
      data-testid={`lineage-hop-${hop.kind}`}
    >
      <span aria-hidden className="absolute left-0 top-1 h-2.5 w-2.5 rounded-full border-2 border-accent bg-bg-elevated" />
      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
        <span>{label}</span>
        {/* What is missing stays in the title for whoever builds it next. */}
        {hop.needs && <span className="rounded border border-border px-1 normal-case tracking-normal" title={String(hop.needs)}>{t('pages.membersPage.dashboardEdit.not_tracked')}</span>}
        {hop.by && <span className="normal-case tracking-normal">{hop.by === 'script' ? t('pages.membersPage.dashboardEdit.by_script') : t('pages.membersPage.dashboardEdit.by_kiro')}</span>}
      </div>
      {body && <div className="text-[12.5px] text-text">{body}</div>}
    </li>
  )
}

function Sub({ children, warn = false }: { children: ReactNode; warn?: boolean }) {
  return <div className={`text-[11.5px] ${warn ? 'text-warn-text' : 'text-muted'}`}>{children}</div>
}

/** The raw readings a script declared, as a small bar row. */
function Spark({ points }: { points: number[] }) {
  const max = Math.max(...points.map(Math.abs), 1)
  return (
    <div aria-hidden className="mt-1 flex h-6 items-end gap-px">
      {points.slice(-40).map((p, i) => (
        <span key={i} className="w-1 rounded-sm bg-accent/70" style={{ height: `${Math.max(8, (Math.abs(p) / max) * 100)}%` }} />
      ))}
    </div>
  )
}

/** The comment box shared by the flipped card and the popover's "ask" action. */
function AskBox({ botName, ctx, block, path, onSend, onSent, onDelivered, compact = false, keepAs }: {
  botName: string
  ctx: { board: string; version: number }
  block: string
  path: string
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onSent: () => void
  /** Told once the request is sent, so the card around it can drop its own Cancel. */
  onDelivered?: () => void
  compact?: boolean
  /** Where the host keeps this box's draft (see `EditDrafts`). */
  keepAs: string
}) {
  const { t } = useTranslation()
  const [text, setText] = useKept(`${keepAs}text`, '')
  // A send still out when the box was unmounted is unknown to the remounted box: it
  // may have gone, so it reads as unconfirmed and Send stays off until the text changes.
  const [state, setState] = useKept<'idle' | 'sending' | 'sent' | 'failed' | 'unconfirmed'>(
    `${keepAs}state`, 'idle', kept => (kept === 'sending' ? 'unconfirmed' : kept),
  )
  // THIS BOX'S LIFETIME FENCES ITS SEND. A completion that lands after this box
  // closed is ignored, so it never changes ANOTHER component's open card.
  const alive = useAlive()
  // THE BOX TAKES THE FOCUS when it opens, however it was opened: a key pressed in
  // the frame leaves focus there otherwise, and the next Esc never reaches the host.
  const commentRef = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => { commentRef.current?.focus({ preventScroll: true }) }, [])
  const send = async () => {
    if (!onSend || !text.trim() || state === 'sending' || state === 'unconfirmed') return
    setState('sending')
    // A throw is as unknown as a lost answer: the request may have gone out.
    const outcome = await onSend(changeRequestMessage(text, { ...ctx, block, path })).catch(() => 'unconfirmed' as const)
    // A sent request keeps the card open on "Sent to {bot}." until the person
    // closes it, so they see where it went instead of a card that just vanished.
    // Written even when this box is gone: the host's copy is what a remount shows,
    // and `useKept` drops it if the person closed this draft meanwhile.
    setState(outcome === 'sent' ? 'sent' : outcome === 'refused' ? 'failed' : 'unconfirmed')
    if (!alive.current) return
    if (outcome === 'sent') onDelivered?.()
  }
  return (
    <div className="space-y-1.5">
      <label id={`ask-${block}-${path}-label`} className="block text-[12.5px] font-medium text-text-strong" htmlFor={`ask-${block}-${path}`}>
        {t('pages.membersPage.dashboardEdit.comment_label')}
      </label>
      <div className="flex flex-wrap gap-1 font-mono text-[11px] text-muted" data-testid="dashboard-edit-chip">
        <span className="rounded border border-border px-1">{ctx.board}</span>
        {block && <span className="rounded border border-border px-1">{block}</span>}
        {path && <span className="rounded border border-border px-1">{path}</span>}
        <span className="rounded border border-border px-1">v{ctx.version}</span>
      </div>
      <textarea
        id={`ask-${block}-${path}`}
        aria-labelledby={`ask-${block}-${path}-label`}
        value={text}
        // Editing the text makes it a new request, which may be sent.
        onChange={e => { setText(e.target.value); if (state === 'unconfirmed') setState('idle') }}
        // LOCKED FROM SEND TO CLOSE: the text on screen is the text sent, so the close
        // that follows a sent request can never take an edit nobody sent with it.
        readOnly={state === 'sending' || state === 'sent'}
        aria-readonly={state === 'sending' || state === 'sent' || undefined}
        rows={compact ? 2 : 3}
        ref={commentRef}
        className="w-full resize-y rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus:border-accent focus:outline-none"
        data-testid="dashboard-edit-comment"
      />
      <div className="flex items-center gap-2">
        {state === 'failed' ? (
          // No hand-off: the request typed above is unsent local state, which the
          // hand-off's navigation would drop.
          <ErrorNotice variant="inline" className="flex-1" message={t('pages.membersPage.dashboardEdit.send_failed')} testId="dashboard-edit-send-failed" />
        ) : state === 'unconfirmed' ? (
          // No hand-off, for the same unsent draft. Send stays off: the request may
          // already be in the chat, and sending it again would ask twice.
          <ErrorNotice variant="inline" className="flex-1" message={t('pages.membersPage.dashboardEdit.send_unconfirmed', { bot: botName })} testId="dashboard-edit-send-unconfirmed" />
        ) : (
          <span className="flex-1 text-[11.5px] text-muted" role="status">
            {state === 'sent' ? t('pages.membersPage.dashboardEdit.sent', { bot: botName })
              : !onSend ? t('pages.membersPage.dashboardEdit.no_chat')
                : t('pages.membersPage.dashboardEdit.comment_hint', { bot: botName })}
          </span>
        )}
        {state === 'sent' ? (
          <Btn onClick={onSent} data-testid="dashboard-edit-sent-done">{t('pages.membersPage.dashboardEdit.done')}</Btn>
        ) : (
          <Btn primary onClick={() => void send()} disabled={!onSend || !text.trim() || state === 'sending' || state === 'unconfirmed'} data-testid="dashboard-edit-send">
            <Send size={13} className="lucide-inline" aria-hidden />
            {t('pages.membersPage.dashboardEdit.send', { bot: botName })}
          </Btn>
        )}
      </div>
    </div>
  )
}

/** "Correct the value" for a field the Bot writes. */
function CorrectBox({ slug, member, botName, field, type, shownType, onAdoptType, blocked, typedOn, shown, onDone, keepAs }: { slug: string; member: string; botName: string; field: string; type: string; shownType?: string; onAdoptType?: (type: string) => void; blocked?: string; typedOn: { page: string; version: number }; shown: { page: string; version: number }; onDone: () => void; keepAs: string }) {
  const { t } = useTranslation()
  // THE PAGE THIS DRAFT IS FOR, by name and version. Another page shown since it
  // opened, or a newer version of it, may give the field another meaning, so Save
  // waits until the person confirms the draft against the page on screen; the
  // gateway refuses any other page anyway (409 stale_page).
  const [page, setPage] = useKept(`${keepAs}page`, typedOn)
  const needsConfirm = shown.page !== page.page || shown.version !== page.version
    || (shownType !== undefined && shownType !== type)
  // Confirming takes the page on screen as it is: its name, version and the field's type there.
  const reconfirm = () => {
    setPage(shown)
    if (shownType !== undefined && shownType !== type) onAdoptType?.(shownType)
  }
  const qc = useQueryClient()
  const [text, setText] = useKept(`${keepAs}text`, '')
  const [reason, setReason] = useKept(`${keepAs}reason`, '')
  /** The typed value does not read as the field's type: a check of the input, kept
   *  apart from a request the gateway refused or that failed. */
  const [invalid, setInvalid] = useState(false)
  const [failed, setFailed] = useState(false)
  const [saving, setSaving] = useState(false)
  // Fenced like the Ask box: `onDone` resets the popover's Edit mode, so a save that
  // completes after this box closed must not reset a newer Ask draft opened since.
  const alive = useAlive()
  const save = async () => {
    const parsed = parseCorrection(text, type)
    setFailed(false)
    if (!parsed) { setInvalid(true); return }
    setInvalid(false)
    setSaving(true)
    try {
      await api.memberDashboardCorrect(slug, member, field, parsed.value, reason.trim(), page)
      // The pages re-read either way: the correction is written whether or not
      // this box is still open.
      void qc.invalidateQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, slug] })
      // The saved draft is spent: a box remounted from the host's copy opens empty.
      setText('')
      setReason('')
      if (!alive.current) return
      onDone()
    } catch {
      if (!alive.current) return
      // The gateway's refusal is a failed request, said through the notice. The
      // "not a valid" hint is only for the local parse above: the value already
      // passed the type this box knows.
      setFailed(true)
    } finally {
      if (alive.current) setSaving(false)
    }
  }
  const hintId = `correct-${field}-hint`
  return (
    <div className="space-y-1.5" data-testid="dashboard-edit-correct">
      {/* Visible labels: a typed value replaces the placeholder, and the box would
          otherwise no longer say what it is. */}
      <label htmlFor={`${hintId}-value`} className="block text-[12px] font-medium text-text-strong">{t('pages.membersPage.dashboardEdit.correct_value')}</label>
      <Input
        id={`${hintId}-value`}
        value={text}
        onChange={e => { setText(e.target.value); setInvalid(false) }}
        // Locked while the save is out, for the same reason as the Ask box.
        readOnly={saving}
        aria-invalid={invalid || undefined}
        aria-describedby={invalid ? hintId : undefined}
        placeholder={t('pages.membersPage.dashboardEdit.correct_value')}
        className="w-full px-2.5 py-1.5 font-mono text-[13px]"
      />
      <label htmlFor={`${hintId}-reason`} className="block text-[12px] font-medium text-text-strong">{t('pages.membersPage.dashboardEdit.correct_reason')}</label>
      <Input
        id={`${hintId}-reason`}
        value={reason}
        onChange={e => setReason(e.target.value.slice(0, CORRECTION_REASON_LIMIT))}
        readOnly={saving}
        className="w-full px-2.5 py-1.5 text-[13px]"
      />
      <p className="m-0 text-[11.5px] text-muted" data-testid="dashboard-edit-correct-effect">{t('pages.membersPage.dashboardEdit.correct_effect', { bot: botName })}</p>
      {/* No hand-off: the value and reason typed above are unsaved, and the
          hand-off's navigation would drop them. */}
      {failed && <ErrorNotice variant="inline" message={t('pages.membersPage.dashboardEdit.correct_failed')} testId="dashboard-edit-correct-failed" />}
      <div className="flex items-center gap-2">
        <span id={hintId} className="flex-1 text-[11.5px] text-muted" role="status" data-testid="dashboard-edit-correct-hint">
          {blocked || (needsConfirm ? t('pages.membersPage.dashboardEdit.page_changed_confirm') : '') || (invalid ? t('pages.membersPage.dashboardEdit.correct_bad', { type }) : '')}
        </span>
        {!blocked && needsConfirm && (
          <Btn onClick={reconfirm} data-testid="dashboard-edit-correct-reconfirm">
            {t('pages.membersPage.dashboardEdit.page_changed_use')}
          </Btn>
        )}
        <Btn primary onClick={() => void save()} disabled={!!blocked || needsConfirm || saving || (!text.trim() && !parseCorrection('', type))} data-testid="dashboard-edit-correct-save">
          {t('pages.membersPage.dashboardEdit.correct_save')}
        </Btn>
      </div>
    </div>
  )
}

/** The lineage popover beside one value, with its line to the value and its Edit row. */
function LineagePopover({ target, bounds, slug, member, botName, reduced, ctx, onSend, onEnter, onLeave, onClose, onPinned, escRef, editKeyRef, paused = false, shownVersion, shownPage }: {
  paused?: boolean
  /** The page version on screen now; `ctx.version` is the one the popover opened on. */
  shownVersion?: number
  /** The page on screen now; `ctx.page` is the one the popover opened on. */
  shownPage?: string
  target: Target
  bounds: Rect
  slug: string
  member: string
  botName: string
  reduced: boolean
  ctx: PageCtx
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onEnter: () => void
  onLeave: () => void
  onClose: () => void
  /** Told whether Edit is open: while it is, the popover stays put. */
  onPinned: (pinned: boolean) => void
  /** The layer's Esc asks this first: true means Esc closed Edit and nothing else. */
  escRef: { current: (() => boolean) | null }
  /** The layer calls this when the frame forwards the Edit shortcut. */
  editKeyRef: { current: (() => void) | null }
}) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState(POPOVER_SIZE)
  const drafts = useContext(DraftsContext)
  const scope = popoverScope(target.path)
  const [mode, setMode] = useKept<EditMode>(`${scope}mode`, 'none')
  // Edit closed drops what was typed in it, so opening it again starts empty.
  useEffect(() => { if (mode === 'none') dropDrafts(drafts, `${scope}draft${NS}`) }, [drafts, scope, mode])
  // EDIT PINS THE POPOVER: moving away or hovering another value leaves it open, and
  // the first Esc closes Edit while a second closes the popover.
  useEffect(() => { onPinned(mode !== 'none') }, [mode, onPinned])
  useEffect(() => () => onPinned(false), [onPinned])
  useEffect(() => {
    escRef.current = () => {
      if (mode === 'none') return false
      setMode('none')
      return true
    }
    return () => { escRef.current = null }
  }, [escRef, mode, setMode])
  // The same shortcut pressed on a value focused INSIDE the frame, forwarded by the
  // bridge (EDIT_KEY_MESSAGE_TYPE) because that key never reaches this window.
  useEffect(() => {
    editKeyRef.current = () => { if (mode === 'none') setMode('menu') }
    return () => { editKeyRef.current = null }
  }, [editKeyRef, mode, setMode])
  // Edit opened takes the focus to its first option, so a keyboard that opened it
  // from inside the frame is now in the host and its Esc closes Edit again.
  const editRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (mode === 'menu') editRef.current?.querySelector<HTMLElement>('button:not([disabled])')?.focus({ preventScroll: true })
  }, [mode])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never a letter typed anywhere: not in a field (the chat composer is a
      // contenteditable editor), and not with a modifier held.
      if (isEditableElement(e.target) || e.ctrlKey || e.metaKey || e.altKey) return
      // Nor while the Dashboard is kept mounted but not shown.
      if (hiddenFromView(ref.current)) return
      if (mode === 'none' && (e.key === 'e' || e.key === 'E')) { e.preventDefault(); setMode('menu') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mode, setMode])
  const qc = useQueryClient()
  // UNDER THE PAGE'S OWN PREFIX, so every signal that re-reads the page (a value
  // written, the page moved, a refresh frame) re-reads the open lineage with it.
  const lineageKey = lineageQueryKey(slug, member, target.path)
  // A field push refills only the frame, so it re-reads the open lineage itself:
  // one read at a time, so frames faster than a read never stack reads on the gateway.
  useEffect(
    () => subscribeDashboardFields(slug, () => { refreshDashboardReads(qc, lineageQueryKey(slug, member)) }),
    [qc, slug, member],
  )
  const { data, isLoading, isError } = useQuery({
    queryKey: lineageKey,
    queryFn: () => api.memberDashboardLineage(slug, member, target.path),
    staleTime: 15_000,
    retry: false,
    // Paused: the lineage on screen is kept as read; a re-read would trace the newer page.
    enabled: !paused,
  })
  useLayoutEffect(() => {
    const el = ref.current
    if (el) setSize({ w: el.offsetWidth || POPOVER_SIZE.w, h: el.offsetHeight || POPOVER_SIZE.h })
  }, [data, mode])
  const width = popoverWidth(bounds.w)
  const place = placePopover(target.rect, { w: width, h: size.h }, bounds)
  const anchorX = place.side === 'left' ? target.rect.x : place.side === 'right' ? target.rect.x + target.rect.w : target.rect.x + target.rect.w / 2
  const anchorY = place.side === 'above' ? target.rect.y : place.side === 'below' ? target.rect.y + target.rect.h : target.rect.y + target.rect.h / 2
  const endX = place.side === 'left' ? place.x + width : place.side === 'right' ? place.x : anchorX
  const endY = place.side === 'above' ? place.y + size.h : place.side === 'below' ? place.y : Math.min(Math.max(anchorY, place.y + 16), place.y + size.h - 16)
  const lineage = data as DashboardLineage | undefined
  return (
    <>
      <svg aria-hidden className="pointer-events-none absolute inset-0 h-full w-full overflow-visible" style={{ zIndex: Z_PICKED }} data-testid="lineage-connector">
        <rect x={target.rect.x - 3} y={target.rect.y - 3} width={target.rect.w + 6} height={target.rect.h + 6} rx={6} className="fill-none stroke-accent" strokeWidth={2} />
        <line x1={anchorX} y1={anchorY} x2={endX} y2={endY} className="stroke-accent" strokeWidth={1.5} strokeDasharray="4 3" />
      </svg>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- the mouse handlers only hold the hover popover open while the pointer is over it; a keyboard opens it pinned and closes it with Escape, and every action inside is a real button */}
      <div
        ref={ref}
        role="dialog"
        aria-label={t('pages.membersPage.dashboardEdit.where_from')}
        className={`absolute pointer-events-auto max-h-[70vh] overflow-y-auto rounded-xl border border-accent/50 bg-bg-elevated p-3 shadow-xl ${reduced ? '' : 'motion-safe:animate-scale-in'}`}
        style={{ left: place.x, top: place.y, width, maxHeight: Math.min(popoverMaxHeight(bounds.h), window.innerHeight * 0.7), zIndex: Z_PICKED }}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        data-testid="lineage-popover"
        data-side={place.side}
      >
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-[22px] font-semibold text-text-strong tabular-nums" data-testid="lineage-value">{lineage ? shownValue(lineage.value) : '\u2026'}</div>
            <div className="truncate text-[12px] text-muted">{target.field?.label || target.path}</div>
          </div>
          {lineage && <KindTag kind={lineage.kind} botName={botName} />}
          <IconButton onClick={onClose} aria-label={t('pages.membersPage.dashboardEdit.close')}>
            <X size={14} aria-hidden />
          </IconButton>
        </div>
        <div className="mt-2 mb-1.5 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-wider text-accent">
          <Workflow size={12} aria-hidden />{t('pages.membersPage.dashboardEdit.where_from')}
        </div>
        {isLoading && <p className="text-[12px] text-muted" role="status">{t('pages.membersPage.dashboardEdit.tracing')}</p>}
        {/* A failed read can be a refetch that keeps the last lineage, with Ask or
            Correct still open beside this notice. No hand-off: it would drop those
            drafts and the Members page's DM composer, Profile and crew-editor drafts. */}
        {isError && <ErrorNotice variant="inline" message={t('pages.membersPage.dashboardEdit.trace_failed')} testId="lineage-trace-failed" />}
        {lineage && <LineageSummary lineage={lineage} botName={botName} />}
        {/* The steps behind the sentence, in the system's own terms: kept for whoever
            asks, closed by default so a first reader meets the sentence only. */}
        {lineage && (
          <details className="mt-1.5 group" data-testid="lineage-details">
            <summary className="cursor-pointer select-none text-[11.5px] text-muted hover:text-text">{t('pages.membersPage.dashboardEdit.details')}</summary>
            <ol className="relative mt-1.5 list-none m-0 p-0 before:absolute before:left-[4px] before:top-2 before:bottom-3 before:w-px before:bg-accent/40">
              <Hop hop={{ kind: 'page', path: target.path }} botName={botName} index={0} reduced={reduced} key="page" />
              {lineage.hops.map((hop, i) => <Hop key={i} hop={hop} botName={botName} index={i + 1} reduced={reduced} />)}
            </ol>
            <p className="m-0 flex items-center gap-1.5 text-[11px] text-muted">
              <Info size={12} aria-hidden />
              <span className="flex-1">{t('pages.membersPage.dashboardEdit.shown_by')}</span>
            </p>
          </details>
        )}
        {lineage && (
          // PINNED TO THE BOTTOM: the steps above scroll, the actions never do, so
          // "Save correction" and Send stay in view however long the lineage is.
          <div ref={editRef} className="sticky -bottom-3 -mx-3 -mb-3 mt-1 space-y-2 border-t border-border bg-bg-elevated px-3 pb-3 pt-2" data-testid="lineage-edit">
            {mode === 'none' ? (
              <Btn onClick={() => setMode('menu')} data-testid="lineage-edit-open">
                <PencilLine size={13} className="lucide-inline" aria-hidden />
                {/* Named for what it opens, one action: the menu behind it names each
                    way to change THIS value (ask, correct, see the job) as its own row. */}
                {t('pages.membersPage.dashboardEdit.change_value')}
                <kbd className="ml-1 rounded border border-border px-1 font-mono text-[10px]">E</kbd>
              </Btn>
            ) : (
              <EditPanel
                lineage={lineage}
                mode={mode}
                setMode={setMode}
                slug={slug}
                member={member}
                botName={botName}
                ctx={ctx}
                target={target}
                onSend={onSend}
                onClose={onClose}
                paused={paused}
                shownVersion={shownVersion}
                shownPage={shownPage}
              />
            )}
            <p className="m-0 flex items-center justify-end text-[11px] text-muted">
              <kbd className="rounded border border-border px-1 font-mono text-[10px]">Esc</kbd>
            </p>
          </div>
        )}
      </div>
    </>
  )
}

/** The component turned over: its provider, and the box that asks the Bot to change it. */
function FlipCard({ flip, bounds, botName, reduced, ctx, onSend, onClose }: {
  flip: Flip
  bounds: Rect
  botName: string
  reduced: boolean
  ctx: { board: string; version: number }
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [turned, setTurned] = useState(reduced)
  const scope = flipScope(flip)
  const [delivered, setDelivered] = useKept(`${scope}delivered`, false)
  const shellRef = useRef<HTMLDivElement | null>(null)
  /** Where the card is drawn on its first frame: on the box it replaces. */
  const [start, setStart] = useState<string | null>(null)
  const w = Math.min(Math.max(flip.rect.w, 340), bounds.w - 8)
  const left = Math.max(4, Math.min(flip.rect.x, bounds.w - w - 4))
  const top = Math.max(4, Math.min(flip.rect.y, bounds.h - 300))
  // ONE ELEMENT MOVES: the card starts on the box it replaces (the popover, else the
  // component) and travels to its own place while it turns over, so the click reads
  // as that box flipping. With reduced motion it is simply there.
  useLayoutEffect(() => {
    const el = shellRef.current
    if (reduced || !el || w <= 0 || flip.from.w <= 0) return
    const h = el.offsetHeight || flip.from.h
    const sy = h ? flip.from.h / h : 1
    setStart(`translate(${flip.from.x - left}px, ${flip.from.y - top}px) scale(${flip.from.w / w}, ${sy})`)
    // Two frames: the start must paint before the transition to the place begins.
    let second = 0
    const first = requestAnimationFrame(() => { second = requestAnimationFrame(() => setStart(null)) })
    return () => { cancelAnimationFrame(first); cancelAnimationFrame(second) }
    // The card is placed once, on mount: a later resize moves it without replaying.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (reduced) return
    const id = requestAnimationFrame(() => setTurned(true))
    return () => cancelAnimationFrame(id)
  }, [reduced])
  return (
    <div
      ref={shellRef}
      className="absolute pointer-events-auto"
      style={{
        left, top, width: w, perspective: 1200, zIndex: Z_PICKED,
        transform: start ?? 'none',
        transformOrigin: '0 0',
        transition: start || reduced ? 'none' : 'transform 260ms cubic-bezier(.16,1,.3,1)',
      }}
      data-testid="flip-card-shell"
    >
      <div
        role="dialog"
        aria-label={t('pages.membersPage.dashboardEdit.flip_title', { block: flip.block || t('pages.membersPage.dashboardEdit.this_value') })}
        className="rounded-xl border-2 border-accent/60 bg-bg-elevated p-3 shadow-2xl"
        style={{ transform: turned ? 'rotateY(0deg)' : 'rotateY(90deg)', transition: reduced ? 'none' : 'transform 260ms cubic-bezier(.16,1,.3,1)' }}
        data-testid="dashboard-edit-flip"
      >
        <div className="mb-2 flex items-center gap-2">
          <Repeat2 size={15} className="text-accent" aria-hidden />
          <span className="flex-1 text-[13.5px] font-semibold text-text-strong">
            {t('pages.membersPage.dashboardEdit.flip_title', { block: flip.block || t('pages.membersPage.dashboardEdit.this_value') })}
          </span>
          <IconButton onClick={onClose} aria-label={t('pages.membersPage.dashboardEdit.close')}>
            <X size={13} aria-hidden />
          </IconButton>
        </div>
        <dl className="mb-3 space-y-1.5 rounded-lg border border-border p-2.5 text-[12.5px]" data-testid="dashboard-edit-provider">
          {flip.fields.length === 0 && <p className="text-muted">{t('pages.membersPage.dashboardEdit.no_fields')}</p>}
          {flip.fields.map(field => (
            <div key={field.name} className="grid grid-cols-[minmax(0,7rem)_1fr_auto] items-center gap-2">
              <dt className="truncate font-mono text-muted">{field.name}</dt>
              <dd className="m-0 truncate font-mono text-accent">
                {field.kind === 'agentic' ? t('pages.membersPage.dashboardEdit.kind_agentic', { bot: botName }) : <SourceLine field={field} />}
              </dd>
              <KindTag kind={field.kind} botName={botName} />
            </div>
          ))}
        </dl>
        <AskBox botName={botName} ctx={ctx} block={flip.block} path={flip.path} onSend={onSend} onSent={onClose} onDelivered={() => setDelivered(true)} keepAs={`${scope}ask${NS}`} />
        {/* No Cancel once sent: a sent request cannot be taken back, and Done closes. */}
        {!delivered && (
          <div className="mt-2 flex justify-end">
            <Btn onClick={onClose} data-testid="dashboard-edit-flip-cancel">{t('pages.membersPage.dashboardEdit.cancel')}</Btn>
          </div>
        )}
      </div>
    </div>
  )
}

/** Stacking inside the layer: what the person picked paints over the Sources list, which
 *  stays open beside it, so a narrow pane never hides the lineage under the list. */
const Z_SOURCES = 1
const Z_PICKED = 2

/** Every field the page reads, for a page that draws in script and answers no point. */
function SourcesPanel({ fields, botName, onPick, onClose }: { fields: FieldSource[]; botName: string; onPick: (field: FieldSource, rect: Rect) => void; onClose: () => void }) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement | null>(null)
  const pick = (field: FieldSource, el: HTMLElement) => {
    const host = ref.current?.offsetParent as HTMLElement | null
    const a = el.getBoundingClientRect()
    const b = host?.getBoundingClientRect()
    if (!b) return
    onPick(field, { x: a.left - b.left, y: a.top - b.top, w: a.width, h: a.height })
  }
  return (
    <div ref={ref} style={{ zIndex: Z_SOURCES }} className="absolute right-2 top-2 pointer-events-auto w-[300px] max-h-[80%] overflow-y-auto rounded-xl border border-border bg-bg-elevated p-3 shadow-xl" role="dialog" aria-label={t('pages.membersPage.dashboardEdit.sources')} data-testid="dashboard-edit-sources">
      <div className="mb-1 flex items-center gap-2">
        <Layers size={14} className="text-accent" aria-hidden />
        <span className="flex-1 text-[13px] font-semibold text-text-strong">{t('pages.membersPage.dashboardEdit.sources')}</span>
        <IconButton onClick={onClose} aria-label={t('pages.membersPage.dashboardEdit.close')}>
          <X size={13} aria-hidden />
        </IconButton>
      </div>
      <p className="mb-2 text-[11.5px] text-muted">{t('pages.membersPage.dashboardEdit.sources_desc')}</p>
      <ul className="m-0 list-none space-y-1.5 p-0">
        {fields.map(field => (
          <li key={field.name}>
            <Btn
              type="button"
              onClick={e => pick(field, e.currentTarget)}
              onFocus={e => pick(field, e.currentTarget)}
              className="block w-full rounded-lg px-2.5 py-2 text-left hover:border-accent focus-visible:border-accent"
              data-testid={`dashboard-edit-source-${field.name}`}
            >
              <span className="flex items-center gap-2">
                <span className="flex-1 truncate font-mono text-[12.5px] text-text-strong">{field.name}</span>
                <KindTag kind={field.kind} botName={botName} />
              </span>
              <span className="block truncate text-[11.5px] text-muted">
                {field.kind === 'agentic' ? t('pages.membersPage.dashboardEdit.kind_agentic', { bot: botName }) : <SourceLine field={field} />}
              </span>
            </Btn>
          </li>
        ))}
      </ul>
    </div>
  )
}
