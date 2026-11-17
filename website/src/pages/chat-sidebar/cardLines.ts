import { useSyncExternalStore } from 'react'
import { loadChatConfig } from '../chat/ChatSettings'
import { isCardLineCount, type CardLineCount } from '../chat/cardLineCount'

/**
 * How a session card lays out its lines.
 *
 * Settings → Chat → Sessions → "Title lines" / "Preview lines". One line is the
 * default and keeps today's single-line ellipsis. More lines wrap the text and
 * cut it off with an ellipsis after the last allowed line, so a list of titles
 * that share a long prefix can still be told apart at rest.
 *
 * Settings → Chat → Sessions → "Show Agent Name on Session Cards". On (the
 * default) keeps the agent name as the card's first line. Off drops the name
 * and floats the rest of that line onto the title row (see AGENT_LINE_INLINE_CLS).
 *
 * One snapshot is shared by every row: a sidebar can hold hundreds of cards,
 * and each one subscribing to `mc-config-changed` on its own would re-read
 * localStorage once per row on every settings change.
 */
export interface CardLines { title: CardLineCount; preview: CardLineCount; agent: boolean }

let snapshot: CardLines | null = null
const listeners = new Set<() => void>()

function read(): CardLines {
  const cfg = loadChatConfig()
  const one = (v: unknown): CardLineCount => (isCardLineCount(v) ? v : 1)
  return { title: one(cfg.sessionTitleLines), preview: one(cfg.sessionPreviewLines), agent: cfg.showAgentNameOnCards !== false }
}

function onConfigChanged() {
  const next = read()
  if (snapshot && next.title === snapshot.title && next.preview === snapshot.preview && next.agent === snapshot.agent) return
  snapshot = next
  for (const l of listeners) l()
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) {
    window.addEventListener('mc-config-changed', onConfigChanged)
    // A change that landed between the first render and this subscribe had no
    // listener to hear it.
    onConfigChanged()
  }
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) {
      window.removeEventListener('mc-config-changed', onConfigChanged)
      snapshot = null
    }
  }
}

function getSnapshot(): CardLines {
  if (!snapshot) snapshot = read()
  return snapshot
}

export function useSessionCardLines(): CardLines {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/** Literal class names, so Tailwind's scanner emits every one of them. */
const CLAMP: Record<CardLineCount, string> = {
  1: 'truncate',
  2: 'line-clamp-2 break-words',
  3: 'line-clamp-3 break-words',
}

/** The text-overflow class for a card line allowed `n` lines. */
export function cardLineClass(n: CardLineCount): string {
  return CLAMP[n]
}

/**
 * The agent line's extra classes once its agent name is hidden: what is left
 * (the timestamp, tags, channel and state markers) floats to the right edge of
 * the title row instead of taking a row of its own. The title is an
 * `overflow-hidden` block (`truncate` or `line-clamp-*`), so it starts a block
 * formatting context and narrows beside the float rather than running under
 * it. `mt-1` centres the 12px meta line in the title's 20px line box, and the
 * width cap keeps a long tag run from squeezing the title out.
 */
export const AGENT_LINE_INLINE_CLS = 'float-right ml-2 mt-1 max-w-[60%]'
