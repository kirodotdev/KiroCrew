import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ChatMessage } from '../types'
import { extractChatLinks, type ExtractedLink } from '../utils/extractChatLinks'
import { api } from '../api/client'
import { isSystemNoticeKind } from '../lib/systemNotice'
import { TURN_OPENER_ROLES } from '../pages/chat/groupDisplayItems'

export interface ChatSection {
  /** Stable identity for the turn: the message's `meta.clientTs` / `meta.mid` / `ts`, falling back to its index. */
  id: string
  /** Short label for list UIs: the compact prompt cut to 60 chars with an ellipsis. */
  label: string
  /** The full prompt with whitespace collapsed. */
  prompt: string
  /** The turn's final non-empty assistant reply as written, or '' when none has landed. Consumers compact it at render time. */
  response: string
  /** ISO timestamp of the user message, for the preview card's relative time. */
  ts?: string
  msgIdx: number
  displayIdx: number
}

export interface ChatNavigationData {
  links: ExtractedLink[]
  sections: ChatSection[]
  resolving: boolean
}

/** Rows that begin a new transcript turn: the display layer's opener roles
 *  (user, nudge, subagent) plus injected prompts (cron, note, synthesis). A
 *  `recovery` inject resumes the SAME turn after a tool stall, so it is not an
 *  opener and the resumed reply still belongs to the turn it continues. */
function opensTurn(m: ChatMessage): boolean {
  if (m.role === 'inject') return (m.meta as Record<string, unknown> | undefined)?.injectKind !== 'recovery'
  return TURN_OPENER_ROLES.has(m.role)
}

/** Collapse runs of whitespace (including newlines) to one space. */
function compactText(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

/** Extract ~3 lines of context around a URL from the message text */
function extractContext(text: string, url: string): string {
  const idx = text.indexOf(url)
  if (idx < 0) return ''
  const start = text.lastIndexOf('\n', Math.max(0, idx - 150))
  const end = text.indexOf('\n', idx + url.length + 150)
  return text.slice(start === -1 ? 0 : start, end === -1 ? undefined : end).trim().slice(0, 400)
}

/** Bare-URL labels already resolved in this tab, keyed by URL and context.
 *  shortcut: tab-local, so a reload or another tab labels again; move to a
 *  bounded gateway cache if that repeat shows up in the request metrics. */
const LABEL_STORE_MAX = 500
/** The resolve-links handler's per-request cap (chat_nav.py `links[:20]`). */
const NAV_LINK_BATCH_MAX = 20
const labelStore = new Map<string, string>()

function labelKey(l: { url: string; context: string }): string {
  return JSON.stringify([l.url, l.context])
}

function rememberLabel(key: string, label: string): void {
  labelStore.delete(key)
  labelStore.set(key, label)
  // Map keeps insertion order, so the first key is the oldest.
  if (labelStore.size > LABEL_STORE_MAX) labelStore.delete(labelStore.keys().next().value as string)
}

/** Test seam: drop every stored label. */
export function resetNavLinkLabelsForTest(): void {
  labelStore.clear()
}

export function useChatNavigation(
  messages: ChatMessage[],
  messageToDisplayIdx: Map<number, number>,
): ChatNavigationData {
  const links = useMemo(() => extractChatLinks(messages), [messages])

  // Compute which links need LLM resolution (bare URLs, not from markdown)
  const linksToResolve = useMemo(() => {
    return links
      .map((link, i) => ({ url: link.url, context: extractContext(messages[link.msgIdx]?.content || '', link.url), idx: i }))
      .filter((_, i) => !links[i].fromMarkdown)
  }, [links, messages])

  // Post only the links with no stored label, so a new URL does not re-label
  // the chat's earlier ones (#33708). Not memoized: the label store fills when
  // the query below resolves, and that resolution is what re-renders us.
  // The gateway answers at most NAV_LINK_BATCH_MAX links, so send that many at
  // a time; the rest go out once this batch lands and the key moves on.
  const misses = linksToResolve.filter(l => !labelStore.has(labelKey(l))).slice(0, NAV_LINK_BATCH_MAX)
  const batchKey = misses.map(labelKey).join('\u0001')

  const { isLoading: resolving } = useQuery({
    queryKey: ['nav-link-summaries', batchKey],
    queryFn: async () => {
      const r = await api.resolveNavLinks(misses.map(({ url, context }) => ({ url, context })))
      // Write each line into its own miss's slot. A short reply or an empty or
      // too-short line stays unstored, so a transient failure is retried later.
      if (r.summaries.length === misses.length) {
        misses.forEach((l, i) => {
          const summary = r.summaries[i]
          if (summary && summary.length >= 3) rememberLabel(labelKey(l), summary)
        })
      }
      return r.summaries
    },
    staleTime: Infinity,
    enabled: misses.length > 0,
  })

  // Merge stored labels back into links
  const resolvedLinks = useMemo(() => {
    const result = [...links]
    for (const l of linksToResolve) {
      const summary = labelStore.get(labelKey(l))
      if (summary) result[l.idx] = { ...result[l.idx], label: summary }
    }
    return result
    // batchKey changes when a landed batch fills the store, so we re-read it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [links, linksToResolve, batchKey])

  const sections = useMemo(() => {
    const result: ChatSection[] = []
    // Legacy rows may share a `ts`; suffix repeats so ids stay unique (they are
    // React keys and the minimap's selection identity).
    const seen = new Map<string, number>()
    for (let i = 0; i < messages.length; i++) {
      if (messages[i].role !== 'user') continue
      const content = messages[i].content || ''
      if (!content.trim()) continue
      const displayIdx = messageToDisplayIdx.get(i) ?? -1
      if (displayIdx < 0) continue
      const prompt = compactText(content)
      const label = prompt.slice(0, 60) + (prompt.length > 60 ? '…' : '')
      // The turn's reply is the last non-empty assistant message before the
      // next turn opener (user / nudge / subagent, or an injected prompt),
      // skipping compaction / reload notices that
      // share the assistant role but are not replies.
      let response = ''
      for (let next = i + 1; next < messages.length && !opensTurn(messages[next]); next++) {
        const m = messages[next]
        if (m.role !== 'assistant') continue
        if (isSystemNoticeKind(m.kind ?? (m.meta?.kind as string | undefined))) continue
        const text = m.content || ''
        if (text.trim()) response = text
      }
      const meta = messages[i].meta as Record<string, unknown> | undefined
      // `clientTs` is set at send time and never changes; `mid` arrives once the
      // send is accepted, so preferring it would re-identify the turn mid-flight.
      const explicitId = meta?.clientTs ?? meta?.mid ?? messages[i].ts
      const baseId = typeof explicitId === 'string' && explicitId ? explicitId : `turn-${i}`
      const dup = seen.get(baseId) ?? 0
      seen.set(baseId, dup + 1)
      result.push({
        id: dup === 0 ? baseId : `${baseId}#${dup}`,
        label,
        prompt,
        response,
        ts: messages[i].ts || undefined,
        msgIdx: i,
        displayIdx,
      })
    }
    return result
  }, [messages, messageToDisplayIdx])

  return { links: resolvedLinks, sections, resolving }
}
