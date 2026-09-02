/**
 * Bubble slot — decides what the companion shows when a notification arrives.
 *
 * Pure logic, free of Electron so it can be unit-tested; the polling loop in
 * `pet.tsx` owns the actual display and calls this to decide.
 *
 * Ported from the desktop app's `src/shared/bubbleSlot.ts`. The one change from the
 * original is `collapsedText`, which now returns a translated string instead of a
 * hardcoded English one with an emoji.
 *
 * Rules, each written against a failure that reached the user:
 *  - ONE slot, never a queue. A pile of stacked toasts reads as a workload, so a
 *    second completion replaces the first and collapses into a count.
 *  - A sticky (blocked / needs-approval) bubble takes the slot and holds it,
 *    because unresolved work must not be displaced by routine chatter.
 *  - …but that hold is BOUNDED. Sticky bubbles have no ✕ and never auto-expire,
 *    so with no cap a single unclicked approval silently muted every later
 *    notification — the Activity feed kept filling while no bubble ever appeared
 *    again. The real release is `approval_resolved`; the cap is the safety net.
 */

import { i18nT } from '../../i18n/t'
import type { NotifKind } from './notificationPolicy'

/** How long a sticky bubble keeps the slot before routine ones may take it. */
export const STICKY_HOLD_MS = 90_000

export interface PendingBubble {
  /** Behavioural kind, so a drained bubble keeps its timing and CTA. */
  kind?: NotifKind
  text: string
  sticky: boolean
  /** How many completions have collapsed into this bubble. */
  count: number
  /** When it claimed the slot (ms epoch). */
  at: number
}

export interface SlotResult {
  /** New slot contents, or null when the slot is freed. */
  pending: PendingBubble | null
  /** Text to display now, or null to display nothing. */
  show: string | null
}

/** Copy for N collapsed completions, translated with a {{count}} placeholder. */
export function collapsedText(count: number): string {
  return i18nT('apps.crewCompanion.notif.jobs_finished', { count })
}

/**
 * Decide the slot's next state when a bubble arrives.
 *
 * @param current  What currently occupies the slot (null when free).
 * @param incoming The arriving bubble.
 * @param now      ms epoch.
 */
export function nextBubble(
  current: PendingBubble | null,
  incoming: { text: string; sticky?: boolean; kind?: NotifKind },
  now: number,
): SlotResult {
  const sticky = !!incoming.sticky

  // Blocked work always wins the slot and is shown verbatim.
  if (sticky) {
    const pending: PendingBubble = { text: incoming.text, sticky: true, count: 1, at: now, kind: incoming.kind }
    return { pending, show: pending.text }
  }

  // Collapse with a routine bubble already in the slot instead of stacking.
  if (current && !current.sticky) {
    const count = current.count + 1
    const pending: PendingBubble = { text: collapsedText(count), sticky: false, count, at: now }
    return { pending, show: pending.text }
  }

  // A sticky holds the slot, but only for a bounded window.
  if (current?.sticky && now - current.at < STICKY_HOLD_MS) {
    return { pending: current, show: null }
  }

  const pending: PendingBubble = { text: incoming.text, sticky: false, count: 1, at: now }
  return { pending, show: pending.text }
}
