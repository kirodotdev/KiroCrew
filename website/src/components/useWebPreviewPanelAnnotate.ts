import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { safeSetSessionItem } from '../utils/safeStorage'
import {
  OPAQUE_ROLE_KEYS, PREVIEW_ANNOTATE_EVENT, annotationScreenshotFile, annotationStamp,
  type AnnotationItem, type PreviewAnnotateDetail,
} from '../utils/browserAnnotations'
import { formatAnnotationDraft } from '../utils/browserAnnotations.prompt'

import { i18nT } from '../i18n/t'

/**
 * Annotate for the Browser panel: the user picks elements IN the native page
 * and types a note on each HERE. Owns the per-slot mirror of the in-page
 * overlay (persisted in sessionStorage), the overlay bridge calls and the
 * 150 ms poll, the note editor, two-step Clear and Add to chat.
 * `WebPreviewPanel` calls it once per render and hands the result to
 * `WebPreviewPanelNativeSurface`, which draws it.
 */

/** The panel's mirror of the in-page pick overlay plus the notes it owns,
 *  keyed by the slot it belongs to (see the Annotate section in the component). */
interface AnnotateMirror {
  slot: string
  /** An overlay session is alive on the page (poll it). */
  live: boolean
  picking: boolean
  /** Picks on the current page, as the overlay reports them. */
  targets: BrowserAnnotationTarget[]
  /** Notes by pick id. Panel-owned; never sent to the page. */
  notes: Record<number, string>
  /** Noted picks kept from a page that navigated away (detached). */
  retained: AnnotationItem[]
  page: { url: string; title: string }
  /** The note editor: which pick, and the text being typed. */
  editing: { id: number; text: string } | null
}
const EMPTY_TARGETS: BrowserAnnotationTarget[] = []
const EMPTY_ANNOTATE: AnnotateMirror = {
  slot: '', live: false, picking: false, targets: EMPTY_TARGETS, notes: {}, retained: [], page: { url: '', title: '' }, editing: null,
}
/** One mirror per chat slot. Kept as a map (not "the current slot's mirror")
 *  so switching sessions and coming back finds the notes where they were. */
type AnnotateMirrors = Record<string, AnnotateMirror>
/** sessionStorage key: typed prose must survive a dashboard reload too (the
 *  native view -- and the overlay in it -- outlives the renderer). Session
 *  scoped, not local: notes are about a page that is open right now. */
const ANNOTATE_STORE_KEY = 'mc-browser-annotations'
function loadAnnotateMirrors(): AnnotateMirrors {
  try {
    const raw = sessionStorage.getItem(ANNOTATE_STORE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object') return {}
    const out: AnnotateMirrors = {}
    for (const [slot, m] of Object.entries(parsed as Record<string, Partial<AnnotateMirror>>)) {
      if (!m || typeof m !== 'object') continue
      out[slot] = {
        slot,
        live: !!m.live,
        // Pick mode is a live gesture, never resumed from storage.
        picking: false,
        targets: Array.isArray(m.targets) ? m.targets : EMPTY_TARGETS,
        notes: m.notes && typeof m.notes === 'object' ? m.notes : {},
        retained: Array.isArray(m.retained) ? m.retained : [],
        page: m.page && typeof m.page === 'object' ? { url: String(m.page.url ?? ''), title: String(m.page.title ?? '') } : { url: '', title: '' },
        editing: m.editing && typeof m.editing === 'object' && typeof m.editing.id === 'number' ? { id: m.editing.id, text: String(m.editing.text ?? '') } : null,
      }
    }
    return out
  } catch {
    return {}
  }
}
/** Keep the text of an open editor as its pick's note (live picks only; a
 *  retained pick's draft is written by the caller). Never drops prose. */
function flushDraft(
  editing: AnnotateMirror['editing'],
  notes: Record<number, string>,
  targets: BrowserAnnotationTarget[],
): Record<number, string> {
  if (!editing) return notes
  const text = editing.text.replace(/\s+/g, ' ').trim()
  if (!text || !targets.some(t => t.id === editing.id)) return notes
  return { ...notes, [editing.id]: text }
}
/** The page went away (navigation, view lost): stop the live session but keep
 *  every noted pick -- including a note still being typed -- as detached. */
function detachAll(prev: AnnotateMirror): AnnotateMirror {
  const notes = { ...prev.notes }
  // Text in the open editor is the user's too; an uncommitted draft is kept
  // as that pick's note rather than lost with the page.
  if (prev.editing && prev.editing.text.trim() && prev.targets.some(t => t.id === prev.editing!.id)) {
    notes[prev.editing.id] = prev.editing.text.replace(/\s+/g, ' ').trim()
  }
  return {
    ...prev,
    live: false,
    picking: false,
    editing: prev.editing && prev.retained.some(a => a.id === prev.editing!.id) ? prev.editing : null,
    retained: [
      ...prev.retained,
      ...prev.targets.filter(t => (notes[t.id] ?? '').trim()).map(t => ({ ...t, note: notes[t.id] ?? '', detached: true })),
    ],
    targets: EMPTY_TARGETS,
    notes: {},
  }
}
function saveAnnotateMirrors(mirrors: AnnotateMirrors): void {
  const kept = Object.fromEntries(Object.entries(mirrors).filter(([, m]) => m.live || m.targets.length || m.retained.length || m.editing))
  // The WRITE goes through the helper: a full quota would otherwise raise
  // QuotaExceededError on the render path. The delete stays a plain call —
  // removing a key frees space rather than needing it, so it is not the quota
  // failure mode this migration is about (it keeps its own guard for a denied
  // store). Either way the in-memory map still covers this tab.
  if (Object.keys(kept).length) safeSetSessionItem(ANNOTATE_STORE_KEY, JSON.stringify(kept))
  else { try { sessionStorage.removeItem(ANNOTATE_STORE_KEY) } catch { /* storage unavailable */ } }
}

type AnnotateResult = Awaited<ReturnType<NonNullable<BrowserAPI['annotate']>>>
/** Explicit guards, not `in` narrowing: the bridge returns a wide union and
 *  `tsc -b` under the app config does not narrow `'items' in res` usefully. */
function isAnnotatePoll(res: AnnotateResult): res is BrowserAnnotatePoll {
  return !!res && res.ok === true && Array.isArray((res as { items?: unknown }).items) && typeof (res as { picking?: unknown }).picking === 'boolean'
}
function isAnnotateCapture(res: AnnotateResult): res is BrowserAnnotateCapture {
  return !!res && res.ok === true && typeof (res as { png?: unknown }).png === 'string'
}
/** Plain names for the opaque ARIA roles, in the UI language (the on-page
 *  hover tag gets the same map, since the page realm has no catalog). */
export function annotateRoleNames(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [role, key] of Object.entries(OPAQUE_ROLE_KEYS)) out[role] = i18nT(key)
  return out
}
function annotateFailureText(res: AnnotateResult): string {
  return res && res.ok === false && typeof res.error === 'string' ? res.error : ''
}

export function useWebPreviewPanelAnnotate(sessionKey: string | null | undefined, nativeOpen: boolean) {
  // ── Annotate: element notes on the live page ──────────────────────────────
  // The user points at elements IN the native page (hover highlight, click to
  // select); that surface is an overlay injected into the page by the main
  // process, because the native view is composited above this DOM. The NOTE
  // is typed HERE, in the panel: the page is external content, and a text
  // input inside it would let any page script read the user's prose. So the
  // overlay reports picked elements (id, number, ref, description, liveness)
  // and the panel owns the notes, the editor, the list and Send/Clear.
  // Native-only by construction: the remote/CLI transport has no in-process
  // view to inject into, so the button is not rendered there.
  const annotateBridge = window.browserAPI?.annotate
  const canAnnotate = nativeOpen && typeof annotateBridge === 'function'
  // One mirror per slot, read through the CURRENT slot key in the same
  // render, so a session switch never shows -- or sends -- another session's
  // notes, and coming back finds this session's notes where they were. The
  // map also lives in sessionStorage so a dashboard reload keeps typed prose.
  const [annotateMirrors, setAnnotateMirrors] = useState<AnnotateMirrors>(loadAnnotateMirrors)
  const mirror = annotateMirrors[sessionKey || ''] ?? EMPTY_ANNOTATE
  useEffect(() => { saveAnnotateMirrors(annotateMirrors) }, [annotateMirrors])
  const mirrorsRef = useRef(annotateMirrors)
  mirrorsRef.current = annotateMirrors
  const annotateOn = mirror.live
  const picking = mirror.picking
  const editing = mirror.editing
  /** Everything listed: notes kept from a page that navigated away, then the
   *  current page's picks with their (panel-owned) notes. */
  const annotations = useMemo<AnnotationItem[]>(() => [
    ...mirror.retained,
    ...mirror.targets.map(t => ({ ...t, note: mirror.notes[t.id] ?? '' })),
  ], [mirror.retained, mirror.targets, mirror.notes])
  /** What Send hands over: only picks that actually carry a note. */
  const noted = useMemo(() => annotations.filter(a => a.note.trim()), [annotations])
  /** Add to chat has something to send once a note is saved -- or typed and
   *  still open in the editor (it is committed on the way out). */
  const canSend = noted.length > 0 || !!editing?.text.trim()
  const [annotateBusy, setAnnotateBusy] = useState(false)
  const [annotateError, setAnnotateError] = useState('')
  /** The agent hand-off is offered on errors it could act on -- not on the
   *  capture-failed notice, which already says the notes were added and what
   *  did not happen; there is nothing for the agent to fix there. */
  const annotateErrorAskAgent = annotateError !== i18nT('components.webPreviewPanel.annotate_capture_failed')
  /** Brief confirmation after Add to chat: the result lands in the composer,
   *  away from the button, so say here that nothing was sent yet. */
  const [addedAck, setAddedAck] = useState(false)
  /** Two-step Clear: first click arms (label restates the count), second within
   *  the window executes. Typed prose must not vanish on one click. */
  const [clearArmed, setClearArmed] = useState(false)
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** Consecutive poll failures that are NOT "the overlay is gone". */
  const pollFailures = useRef(0)
  /**
   * Picks the poll updater abandons (nothing typed, editor moved on) and the
   * page has not been told to drop yet, keyed by the slot they belong to. The
   * updater fills it; the effect below drains it after the commit. React runs
   * a state updater lazily whenever the hook already has an update queued, so
   * a decision made inside it is not readable on the line after setState --
   * only after the render it produces. The slot travels with the id because
   * that render can also be the one that switches sessions: a remove bound to
   * the session current at drain time would reach the other slot's page, where
   * the same marker number may be a live pick.
   */
  const abandonedPicks = useRef<Map<string, Set<number>>>(new Map())
  const noteInputRef = useRef<HTMLInputElement>(null)

  const callAnnotate = useCallback(async (op: BrowserAnnotateOp, args?: Record<string, unknown>) => {
    if (!sessionKey || typeof annotateBridge !== 'function') return null
    return annotateBridge(sessionKey, op, args)
  }, [annotateBridge, sessionKey])

  /** Update ONE slot's mirror; every other slot's state is left alone. */
  const patchSlot = useCallback((slot: string, fn: (prev: AnnotateMirror) => AnnotateMirror) => {
    setAnnotateMirrors(prev => {
      const cur = prev[slot] ?? { ...EMPTY_ANNOTATE, slot }
      const next = fn(cur)
      return next === cur ? prev : { ...prev, [slot]: next }
    })
  }, [])
  /** Update the current slot's mirror. */
  const patchMirror = useCallback((fn: (prev: AnnotateMirror) => AnnotateMirror) => {
    patchSlot(sessionKey || '', fn)
  }, [patchSlot, sessionKey])

  // Tell each slot's overlay to drop the picks the poll updater abandoned. This
  // runs after the commit that removed them from the list, so list, count and
  // page highlight agree, and it addresses the bridge by the recorded slot
  // rather than through callAnnotate, which is bound to whichever session is
  // current now. The Set dedupes the updater's second run under StrictMode.
  useEffect(() => {
    if (!abandonedPicks.current.size) return
    const pending = [...abandonedPicks.current]
    abandonedPicks.current.clear()
    if (typeof annotateBridge !== 'function') return
    for (const [slot, ids] of pending) for (const id of ids) void annotateBridge(slot, 'remove', { id })
  }, [annotateMirrors, annotateBridge])

  // A session switch resets the per-turn transients: poll failure count and an
  // armed Clear -- a confirmation armed for one session's notes must never fire
  // on another session's.
  useEffect(() => {
    pollFailures.current = 0
    setClearArmed(false)
    if (clearTimer.current) { clearTimeout(clearTimer.current); clearTimer.current = null }
  }, [sessionKey])

  /** Toggle pick mode. First use installs the overlay and starts polling. */
  const toggleAnnotate = useCallback(async () => {
    if (annotateBusy) return
    setAnnotateError('')
    if (annotateOn && picking) {
      // Only a confirmed stop flips the local state; a failed one is surfaced
      // and `picking` is left for the poll to reconcile with the page.
      const stopped = await callAnnotate('stop')
      if (!stopped || !stopped.ok) {
        setAnnotateError(annotateFailureText(stopped) || i18nT('components.webPreviewPanel.annotate_update_failed'))
        return
      }
      patchMirror(prev => ({ ...prev, picking: false }))
      return
    }
    // Continue numbering after notes retained from a page that navigated
    // away, so on-page markers, the list and the draft agree, and ids never
    // collide with the retained ones.
    const res = await callAnnotate('start', {
      editHint: i18nT('components.webPreviewPanel.annotate_click_to_edit'),
      roleNames: annotateRoleNames(),
      seq: mirror.retained.reduce((m, a) => Math.max(m, a.n), 0),
      idStart: mirror.retained.reduce((m, a) => Math.max(m, a.id), 0),
    })
    if (!res || !res.ok) {
      setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_failed'))
      return
    }
    const page = { url: res.url ?? '', title: res.title ?? '' }
    pollFailures.current = 0
    // Starting never wipes: picks and notes from a paused session (Done,
    // Add to chat, or a transient poll failure) stay exactly as they are. If
    // the overlay really is gone, the first poll answers `no_overlay` and
    // `detachAll` moves the noted picks to `retained` -- nothing typed is
    // discarded on any path.
    patchMirror(prev => ({ ...prev, live: true, picking: true, page }))
  }, [annotateBusy, annotateOn, picking, callAnnotate, patchMirror, mirror.retained])

  // Mirror the overlay while a session is alive. The overlay lives in the
  // page, so the picks the user makes there reach this panel only by asking;
  // 150ms keeps it feeling live without measurable cost (one tiny
  // executeJavaScript per tick; state is replaced only when something
  // changed). Two failure shapes, two answers:
  //   • `no_overlay` -- the document navigated and took the overlay with it.
  //     Polling stops, but the notes the user typed are KEPT (moved to
  //     `retained`, marked detached), so Send still works: the draft flags
  //     them, no screenshot. Annotating a dev app the agent is editing means
  //     HMR reloads mid-session are routine; wiping typed prose on each one
  //     would be the wrong default.
  //   • anything else (timeout, exception) -- transient. Retry; after three in
  //     a row surface it and pause polling, still keeping the notes.
  useEffect(() => {
    if (!annotateOn || !canAnnotate) return
    let stopped = false
    const slot = sessionKey || ''
    const tick = async () => {
      const res = await callAnnotate('poll')
      if (stopped) return
      if (isAnnotatePoll(res)) {
        pollFailures.current = 0
        patchSlot(slot, prev => {
          // A live pick can never share an id with a retained note (the overlay
          // is started past the highest retained id); one that does is a
          // page-controlled reply and is ignored rather than aliasing a note.
          const incoming = prev.retained.length
            ? res.items.filter(t => !prev.retained.some(a => a.id === t.id))
            : res.items
          const sameTargets = JSON.stringify(incoming) === JSON.stringify(prev.targets)
          let targets = sameTargets ? prev.targets : incoming
          let notes = prev.notes
          let retained = prev.retained
          if (!sameTargets) {
            // A pick the reply no longer lists: the honest overlay only drops
            // one on the user's own remove/clear (both already handled here),
            // so anything else -- a page tampering with the reply -- must not
            // cost a note. Noted picks that went missing are kept as detached.
            const keep = new Set(targets.map(t => t.id))
            const missingNoted = prev.targets.filter(t => !keep.has(t.id) && (prev.notes[t.id] ?? '').trim() && !retained.some(a => a.id === t.id))
            if (missingNoted.length) retained = [...retained, ...missingNoted.map(t => ({ ...t, note: prev.notes[t.id], detached: true }))]
            notes = Object.fromEntries(Object.entries(prev.notes).filter(([id]) => keep.has(Number(id))))
          }
          let editing = prev.editing
          const moveTo = res.picked !== undefined ? res.picked : res.edit
          if (moveTo !== undefined && (!editing || editing.id !== moveTo)) {
            // The editor moves to another pick. Whatever was typed for the one
            // it leaves is kept as that pick's note -- live or retained -- and
            // never dropped (same rescue as detachAll on navigation). A pick
            // left with NOTHING typed is abandoned: it goes, like Esc does, so
            // the list, the count and the draft never disagree about it.
            notes = flushDraft(editing, notes, targets)
            if (editing && editing.text.trim() && retained.some(a => a.id === editing!.id)) {
              const text = editing.text.replace(/\s+/g, ' ').trim()
              retained = retained.map(a => (a.id === editing!.id ? { ...a, note: text } : a))
            }
            if (editing && !editing.text.trim() && !(notes[editing.id] ?? '').trim() && targets.some(t => t.id === editing!.id) && editing.id !== moveTo) {
              const ids = abandonedPicks.current.get(slot) ?? new Set<number>()
              ids.add(editing.id)
              abandonedPicks.current.set(slot, ids)
              targets = targets.filter(t => t.id !== editing!.id)
            }
            editing = { id: moveTo, text: notes[moveTo] ?? '' }
          }
          else if (editing && !targets.some(t => t.id === editing!.id) && !retained.some(a => a.id === editing!.id)) editing = null
          const page = { url: res.url, title: res.title }
          if (sameTargets && notes === prev.notes && retained === prev.retained && editing === prev.editing && prev.picking === res.picking
            && prev.page.url === page.url && prev.page.title === page.title) return prev
          return { ...prev, picking: res.picking, targets, notes, retained, editing, page }
        })
        return
      }
      // The overlay is gone (navigation) or the view itself is (closed,
      // destroyed): either way the page is not coming back for these picks.
      if (res && !res.ok && (res.code === 'no_overlay' || res.code === 'no_view')) {
        patchSlot(slot, detachAll)
        return
      }
      pollFailures.current += 1
      if (pollFailures.current >= 3) {
        setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_failed'))
        patchSlot(slot, prev => ({ ...prev, live: false, picking: false }))
      }
    }
    void tick()
    const t = setInterval(() => { void tick() }, 150)
    return () => {
      stopped = true
      clearInterval(t)
    }
  }, [annotateOn, canAnnotate, callAnnotate, sessionKey, patchSlot])

  // The pick happened in the page; the note is typed here. Focus the editor
  // as soon as it opens (the main process has already handed keyboard focus
  // back to this renderer on the poll that reported the pick).
  const editingId = editing?.id
  useEffect(() => {
    if (editingId === undefined) return
    const el = noteInputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [editingId])

  // Losing the native view (transport switch, panel close) ends the live
  // session; noted picks are kept as detached until cleared.
  useEffect(() => {
    if (canAnnotate) return
    patchMirror(prev => (prev.live ? detachAll(prev) : prev))
  }, [canAnnotate, patchMirror])
  // Unmount (the panel itself closes) tears every live overlay down. A session
  // SWITCH does not: the other session's native view -- and the overlay in it
  // -- stays alive, so its picks are still there when the user comes back.
  useEffect(() => () => {
    if (typeof annotateBridge !== 'function') return
    for (const [slot, m] of Object.entries(mirrorsRef.current)) {
      if (slot && m.live) void annotateBridge(slot, 'teardown')
    }
  }, [annotateBridge])

  useEffect(() => {
    if (!annotateError) return
    const t = setTimeout(() => setAnnotateError(''), 8000)
    return () => clearTimeout(t)
  }, [annotateError])
  useEffect(() => {
    if (!addedAck) return
    const t = setTimeout(() => setAddedAck(false), 5000)
    return () => clearTimeout(t)
  }, [addedAck])
  useEffect(() => () => { if (clearTimer.current) clearTimeout(clearTimer.current) }, [])

  const dropLocally = useCallback((id: number) => {
    patchMirror(prev => {
      const isTarget = prev.targets.some(t => t.id === id)
      const notes = { ...prev.notes }
      delete notes[id]
      return {
        ...prev,
        notes,
        targets: isTarget ? prev.targets.filter(t => t.id !== id) : prev.targets,
        retained: prev.retained.filter(a => a.id !== id),
        editing: prev.editing?.id === id ? null : prev.editing,
      }
    })
  }, [patchMirror])
  /** Remove a pick. On a live page the overlay is asked FIRST and the note is
   *  dropped only once it confirms: a refused removal must not leave a marker
   *  on the page whose note has already vanished from here. */
  const removeAnnotation = useCallback(async (id: number) => {
    const live = annotateOn && mirror.targets.some(t => t.id === id)
    if (!live) {
      dropLocally(id)
      return
    }
    const res = await callAnnotate('remove', { id })
    if (res && res.ok) dropLocally(id)
    else setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_update_failed'))
  }, [annotateOn, mirror.targets, callAnnotate, dropLocally])
  const editAnnotation = useCallback((id: number) => {
    patchMirror(prev => {
      if (prev.editing?.id === id) return prev
      // Moving the editor keeps the in-flight text of the pick it leaves.
      const notes = flushDraft(prev.editing, prev.notes, prev.targets)
      const retained = prev.editing && prev.editing.text.trim() && prev.retained.some(a => a.id === prev.editing!.id)
        ? prev.retained.map(a => (a.id === prev.editing!.id ? { ...a, note: prev.editing!.text.replace(/\s+/g, ' ').trim() } : a))
        : prev.retained
      const existing = notes[id] ?? retained.find(a => a.id === id)?.note ?? ''
      return { ...prev, notes, retained, editing: { id, text: existing } }
    })
  }, [patchMirror])
  const setEditingText = useCallback((text: string) => {
    patchMirror(prev => (prev.editing ? { ...prev, editing: { ...prev.editing, text } } : prev))
  }, [patchMirror])
  /** Enter / Save: keep the note; an empty note on a fresh pick removes the pick. */
  const commitNote = useCallback(() => {
    const cur = editing
    if (!cur) return
    const text = cur.text.replace(/\s+/g, ' ').trim()
    if (!text) {
      const had = mirror.notes[cur.id] ?? mirror.retained.find(a => a.id === cur.id)?.note ?? ''
      if (!had) void removeAnnotation(cur.id)
      else patchMirror(prev => ({ ...prev, editing: null }))
      return
    }
    patchMirror(prev => ({
      ...prev,
      notes: prev.targets.some(t => t.id === cur.id) ? { ...prev.notes, [cur.id]: text } : prev.notes,
      retained: prev.retained.map(a => (a.id === cur.id ? { ...a, note: text } : a)),
      editing: null,
    }))
  }, [editing, mirror.notes, mirror.retained, removeAnnotation, patchMirror])
  /** Esc / Cancel: drop the edit; a fresh pick with nothing typed is removed. */
  const cancelNote = useCallback(() => {
    const cur = editing
    if (!cur) return
    const had = mirror.notes[cur.id] ?? mirror.retained.find(a => a.id === cur.id)?.note ?? ''
    if (!had) void removeAnnotation(cur.id)
    else patchMirror(prev => ({ ...prev, editing: null }))
  }, [editing, mirror.notes, mirror.retained, removeAnnotation, patchMirror])
  const clearAnnotations = useCallback(() => {
    if (!clearArmed) {
      setClearArmed(true)
      if (clearTimer.current) clearTimeout(clearTimer.current)
      // Long enough to read the restated label, short enough that a stale
      // armed state cannot ambush a later click.
      clearTimer.current = setTimeout(() => setClearArmed(false), 4000)
      return
    }
    if (clearTimer.current) clearTimeout(clearTimer.current)
    setClearArmed(false)
    const wipe = () => patchMirror(prev => ({ ...prev, targets: EMPTY_TARGETS, notes: {}, retained: [], editing: null }))
    if (!annotateOn) {
      wipe()
      return
    }
    // Live page: clear the markers first; only a confirmed clear drops the notes.
    void callAnnotate('clear').then(res => {
      if (res && res.ok) wipe()
      else setAnnotateError(annotateFailureText(res) || i18nT('components.webPreviewPanel.annotate_update_failed'))
    })
  }, [clearArmed, annotateOn, callAnnotate, patchMirror])

  /** Add to chat: screenshot with the markers + the numbered draft, both into
   *  the composer of THIS session (nothing is sent). Notes stay so the user
   *  can keep adding and add again. */
  const sendAnnotations = useCallback(async () => {
    if (annotateBusy) return
    // Text still open in the editor is the user's newest words for that pick:
    // commit it first so the draft carries it, not the previously saved note.
    const openText = editing?.text.replace(/\s+/g, ' ').trim() ?? ''
    const outgoing = annotations
      .map(a => (editing && a.id === editing.id && openText ? { ...a, note: openText } : a))
      .filter(a => a.note.trim())
    if (!outgoing.length) return
    if (editing && openText) {
      patchMirror(prev => ({
        ...prev,
        notes: prev.targets.some(t => t.id === editing.id) ? { ...prev.notes, [editing.id]: openText } : prev.notes,
        retained: prev.retained.map(a => (a.id === editing.id ? { ...a, note: openText } : a)),
        editing: null,
      }))
    }
    setAnnotateBusy(true)
    setAnnotateError('')
    try {
      const cap = annotateOn ? await callAnnotate('capture') : null
      const files: File[] = []
      let page = mirror.page
      let screenshotName: string | undefined
      if (isAnnotateCapture(cap)) {
        const file = annotationScreenshotFile(cap.png, annotationStamp())
        files.push(file)
        screenshotName = file.name
        page = { url: cap.url, title: cap.title }
      } else if (annotateOn) {
        // The notes still go into the composer; say precisely what did not.
        setAnnotateError(i18nT('components.webPreviewPanel.annotate_capture_failed'))
      }
      const draft = formatAnnotationDraft(outgoing, { url: page.url, title: page.title, screenshotName })
      const detail: PreviewAnnotateDetail = { slot: sessionKey || '', files, draft }
      window.dispatchEvent(new CustomEvent(PREVIEW_ANNOTATE_EVENT, { detail }))
      setAddedAck(true)
      if (annotateOn && picking) {
        // Pick mode ends only when the page confirms; otherwise say so and let
        // the poll reconcile `picking` with the page's real state.
        const stopped = await callAnnotate('stop')
        if (stopped && stopped.ok) patchMirror(prev => ({ ...prev, picking: false }))
        else setAnnotateError(annotateFailureText(stopped) || i18nT('components.webPreviewPanel.annotate_update_failed'))
      }
    } catch (e) {
      setAnnotateError(e instanceof Error && e.message ? e.message : i18nT('components.webPreviewPanel.annotate_failed'))
    } finally {
      setAnnotateBusy(false)
    }
  }, [annotateBusy, annotations, editing, annotateOn, picking, mirror.page, callAnnotate, sessionKey, patchMirror])

  return {
    canAnnotate, annotateOn, picking, editing, annotations, canSend, annotateBusy,
    annotateError, setAnnotateError, annotateErrorAskAgent, addedAck, clearArmed, noteInputRef,
    toggleAnnotate, removeAnnotation, editAnnotation, setEditingText, commitNote, cancelNote,
    clearAnnotations, sendAnnotations,
  }
}
