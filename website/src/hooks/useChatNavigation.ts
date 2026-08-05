import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ChatMessage } from '../types'
import { extractChatLinks, type ExtractedLink } from '../utils/extractChatLinks'
import { api } from '../api/client'

export interface ChatSection {
  label: string
  msgIdx: number
  displayIdx: number
}

export interface ChatNavigationData {
  links: ExtractedLink[]
  sections: ChatSection[]
  resolving: boolean
}

/** Extract ~3 lines of context around a URL from the message text */
function extractContext(text: string, url: string): string {
  const idx = text.indexOf(url)
  if (idx < 0) return ''
  const start = text.lastIndexOf('\n', Math.max(0, idx - 150))
  const end = text.indexOf('\n', idx + url.length + 150)
  return text.slice(start === -1 ? 0 : start, end === -1 ? undefined : end).trim().slice(0, 400)
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

  // Stable batch key for the entire set of links to resolve
  const batchKey = useMemo(
    () => linksToResolve.map(l => l.url).join('|'),
    [linksToResolve],
  )

  // Single batched query for all bare URLs
  const { data: summaries = [], isLoading: resolving } = useQuery({
    queryKey: ['nav-link-summaries', batchKey],
    queryFn: () =>
      api.resolveNavLinks(linksToResolve.map(({ url, context }) => ({ url, context }))).then(r => r.summaries),
    staleTime: Infinity,
    enabled: linksToResolve.length > 0,
  })

  // Merge resolved summaries back into links
  const resolvedLinks = useMemo(() => {
    const result = [...links]
    for (let i = 0; i < linksToResolve.length; i++) {
      const summary = summaries[i]
      if (summary && summary.length >= 3) {
        result[linksToResolve[i].idx] = { ...result[linksToResolve[i].idx], label: summary }
      }
    }
    return result
  }, [links, linksToResolve, summaries])

  const sections = useMemo(() => {
    const result: ChatSection[] = []
    for (let i = 0; i < messages.length; i++) {
      if (messages[i].role !== 'user') continue
      const content = messages[i].content || ''
      if (!content.trim()) continue
      const processed = content.replace(/\n/g, ' ').trim()
      const label = processed.slice(0, 60) + (processed.length > 60 ? '…' : '')
      const displayIdx = messageToDisplayIdx.get(i) ?? -1
      if (displayIdx >= 0) {
        result.push({ label, msgIdx: i, displayIdx })
      }
    }
    return result
  }, [messages, messageToDisplayIdx])

  return { links: resolvedLinks, sections, resolving }
}
