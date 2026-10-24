import { useSyncExternalStore } from 'react'
import { eventKeyToken } from './shortcutRegistry'

/** Cards the overlay shows. A hold-to-switch surface, not the glance flyout, so
 *  it caps shallow: five is the whole recent set a thumb walks without the grid
 *  becoming a list to read. */
export const MAX_SWITCHER_SESSIONS = 5

export type HoldKey = 'Alt' | 'Control' | 'Meta'

export interface SessionSwitcherState {
  keys: string[]
  index: number
  holdKeys: HoldKey[]
  stepKey: string
  onCommit: (key: string) => void
}

export function sessionSwitcherOrder(
  available: readonly string[],
  activeSlot: string | null,
  slotHistory: readonly string[],
): string[] {
  const allowed = new Set(available)
  const seen = new Set<string>()
  const ordered: string[] = []
  const add = (key: string | null | undefined) => {
    if (!key || seen.has(key) || !allowed.has(key)) return
    seen.add(key)
    ordered.push(key)
  }

  add(activeSlot)
  for (let index = slotHistory.length - 1; index >= 0; index--) add(slotHistory[index])
  for (const key of available) add(key)
  return ordered.slice(0, MAX_SWITCHER_SESSIONS)
}

export function heldModifiers(
  event: Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey'>,
): HoldKey[] {
  const held: HoldKey[] = []
  if (event.altKey) held.push('Alt')
  if (event.ctrlKey) held.push('Control')
  if (event.metaKey) held.push('Meta')
  return held
}

let state: SessionSwitcherState | null = null
const subscribers = new Set<() => void>()

function emit(): void {
  for (const subscriber of subscribers) subscriber()
}

export function getSessionSwitcher(): SessionSwitcherState | null {
  return state
}

function subscribe(subscriber: () => void): () => void {
  subscribers.add(subscriber)
  return () => subscribers.delete(subscriber)
}

export function useSessionSwitcher(): SessionSwitcherState | null {
  return useSyncExternalStore(subscribe, getSessionSwitcher, getSessionSwitcher)
}

export function openSessionSwitcher(
  options: Omit<SessionSwitcherState, 'index'> & { backwards?: boolean },
): boolean {
  if (options.keys.length < 2 || options.holdKeys.length === 0) return false
  const { backwards, ...rest } = options
  if (!state) attachListeners()
  state = {
    ...rest,
    index: backwards ? rest.keys.length - 1 : 1,
  }
  emit()
  return true
}

export function stepSessionSwitcher(delta: number): void {
  if (!state) return
  const count = state.keys.length
  state = {
    ...state,
    index: ((state.index + delta) % count + count) % count,
  }
  emit()
}

export function setSessionSwitcherIndex(index: number): void {
  if (!state || index < 0 || index >= state.keys.length || index === state.index) return
  state = { ...state, index }
  emit()
}

export function commitSessionSwitcher(index?: number): void {
  if (!state) return
  const { keys, onCommit } = state
  const target = keys[index ?? state.index]
  closeSessionSwitcher()
  if (target && target !== keys[0]) onCommit(target)
}

export function cancelSessionSwitcher(): void {
  if (state) closeSessionSwitcher()
}

function closeSessionSwitcher(): void {
  state = null
  detachListeners()
  emit()
}

const MODIFIER_KEYS = new Set(['Shift', 'Alt', 'Control', 'Meta', 'AltGraph'])
const ARROW_STEPS: Readonly<Record<string, number>> = {
  ArrowDown: 1,
  ArrowRight: 1,
  ArrowUp: -1,
  ArrowLeft: -1,
}

function claim(event: Event): void {
  event.preventDefault()
  event.stopPropagation()
}

function onKeyDown(event: KeyboardEvent): void {
  if (!state || MODIFIER_KEYS.has(event.key)) return
  if (event.key === 'Escape') {
    claim(event)
    cancelSessionSwitcher()
    return
  }
  if (event.key === 'Enter') {
    claim(event)
    commitSessionSwitcher()
    return
  }

  const keyToken = eventKeyToken(event)
  if (keyToken === state.stepKey || keyToken === 'Tab' || keyToken === '`') {
    claim(event)
    stepSessionSwitcher(event.shiftKey ? -1 : 1)
    return
  }

  const arrowStep = ARROW_STEPS[event.key]
  if (arrowStep) {
    claim(event)
    stepSessionSwitcher(arrowStep)
    return
  }

  // A dead key or bare compose keystroke (IME) yields no token and is not an
  // arrow; ignore it so a stray compose press on such a layout does not cancel
  // the switch while the modifier is still held.
  if (!keyToken && !arrowStep) return

  cancelSessionSwitcher()
}

function onKeyUp(event: KeyboardEvent): void {
  const releasedKey = event.key === 'AltGraph' ? 'Alt' : event.key
  if (!state || !(state.holdKeys as string[]).includes(releasedKey)) return
  event.preventDefault()
  commitSessionSwitcher()
}

function onBeforeInput(event: Event): void {
  if (state) event.preventDefault()
}

function attachListeners(): void {
  document.addEventListener('keydown', onKeyDown, true)
  document.addEventListener('keyup', onKeyUp, true)
  document.addEventListener('beforeinput', onBeforeInput, true)
  window.addEventListener('blur', cancelSessionSwitcher)
  document.addEventListener('visibilitychange', cancelSessionSwitcher)
}

function detachListeners(): void {
  document.removeEventListener('keydown', onKeyDown, true)
  document.removeEventListener('keyup', onKeyUp, true)
  document.removeEventListener('beforeinput', onBeforeInput, true)
  window.removeEventListener('blur', cancelSessionSwitcher)
  document.removeEventListener('visibilitychange', cancelSessionSwitcher)
}
