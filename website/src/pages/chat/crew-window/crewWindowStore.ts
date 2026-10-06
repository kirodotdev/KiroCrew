/** Which crew session the chat pane shows as a window, if any.
 *
 *  A crew session is owned by the peer: the hub keeps no slot for it, so the
 *  open window is not a Redux slot key but a `(instanceId, key)` pair naming
 *  the PEER's slot. Kept in sessionStorage so a reload of this tab reopens the
 *  same window and re-reads the peer's own state (a turn still running on the
 *  peer shows as running, not interrupted). */
import { useSyncExternalStore } from 'react'
import { safeGetSessionItem, safeSetSessionItem } from '../../../utils/safeStorage'
import { createSlotDraftStore } from '../../../utils/slotDraftStore'

export interface CrewWindowTarget {
  instanceId: string
  /** The peer's slot key. Meaningful only through that instance's proxy. */
  key: string
}

const STORAGE_KEY = 'kirocrew.crewWindow'
const listeners = new Set<() => void>()

function read(): CrewWindowTarget | null {
  const raw = safeGetSessionItem(STORAGE_KEY)
  if (!raw) return null
  try {
    const v = JSON.parse(raw) as Partial<CrewWindowTarget>
    return typeof v.instanceId === 'string' && v.instanceId && typeof v.key === 'string' && v.key
      ? { instanceId: v.instanceId, key: v.key }
      : null
  } catch {
    return null
  }
}

// Cached so `useSyncExternalStore` sees a stable snapshot between writes.
let current = read()

function write(next: CrewWindowTarget | null) {
  current = next
  safeSetSessionItem(STORAGE_KEY, next ? JSON.stringify(next) : '')
  listeners.forEach(l => l())
}

export function openCrewWindow(target: CrewWindowTarget) {
  if (current?.instanceId === target.instanceId && current.key === target.key) return
  write({ instanceId: target.instanceId, key: target.key })
}

export function closeCrewWindow() {
  if (current) write(null)
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// The crew window on screen in THIS document, as its own close (an embedded
// host closes by navigating, not by clearing the store). The stored target can
// be set where no window renders (a popout copies sessionStorage), so a guard
// about what the user sees reads this, not the target.
let shownClose: (() => void) | null = null

/** Called by the mounted window with its close; returns its unmount. */
export function markCrewWindowShown(close: () => void): () => void {
  shownClose = close
  return () => { if (shownClose === close) shownClose = null }
}

/** Whether a crew window is on screen in this document. */
export function crewWindowShown(): boolean {
  return shownClose !== null
}

/** Close the crew window on screen, the way its own Close button does. */
export function closeShownCrewWindow() {
  shownClose?.()
}

/** The open window right now, for a guard read outside React. */
export function currentCrewWindow(): CrewWindowTarget | null {
  return current
}

export function useCrewWindow(): CrewWindowTarget | null {
  return useSyncExternalStore(subscribe, () => current, () => null)
}

// Unsent composer text per crew session, on the shared slot-draft store so
// switching sessions, or reloading this tab, keeps it. Keyed by machine and
// peer slot: a peer key can equal a local one.
const draftStore = createSlotDraftStore<string>({
  key: 'kirocrew.crewDrafts',
  storage: 'session',
  maxEntries: 50,
  sanitize: (v: unknown) => (typeof v === 'string' && v ? v : null),
})
const drafts = draftStore.load()
const draftKey = (t: CrewWindowTarget) => JSON.stringify([t.instanceId, t.key])

export function readCrewDraft(target: CrewWindowTarget): string {
  return drafts[draftKey(target)] ?? ''
}

const draftListeners = new Set<(key: string, text: string) => void>()

export function writeCrewDraft(target: CrewWindowTarget, text: string) {
  const key = draftKey(target)
  if ((drafts[key] ?? '') === text) return
  draftStore.set(drafts, key, text)
  draftStore.save(drafts)
  draftListeners.forEach(l => l(key, text))
}

/** Follow one session's draft, so a recovery landing after a reopen shows. */
export function subscribeCrewDraft(target: CrewWindowTarget, onChange: (text: string) => void) {
  const want = draftKey(target)
  const l = (key: string, text: string) => { if (key === want) onChange(text) }
  draftListeners.add(l)
  return () => { draftListeners.delete(l) }
}

/** Make everything the window covers `inert`, so Tab/Enter or a scripted
 *  `.focus()` cannot reach the hidden local composer (an approval behind the
 *  window must not be approvable unseen). Siblings mounted later are covered
 *  too; only the `inert` this call added is removed on cleanup. */
export function coverSiblings(cover: Element): () => void {
  const pane = cover.parentElement
  if (!pane) return () => {}
  const mine = new Set<Element>()
  const apply = () => {
    for (const el of Array.from(pane.children)) {
      if (el === cover || el.hasAttribute('inert')) continue
      el.setAttribute('inert', '')
      mine.add(el)
    }
  }
  apply()
  const mo = new MutationObserver(apply)
  mo.observe(pane, { childList: true })
  return () => {
    mo.disconnect()
    mine.forEach(el => el.removeAttribute('inert'))
  }
}

/** Test seam: re-read storage, as a page reload would. */
export function reloadCrewWindowForTest() {
  current = read()
  listeners.forEach(l => l())
}
