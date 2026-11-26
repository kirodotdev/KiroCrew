/**
 * Saved topics for the memory page's search by meaning: a short list of search
 * texts the user saved, one click away. Kept in the dashboard's UI preferences
 * (`localStorage`, backed up to the host by `lib/uiPrefs.ts`), written only by
 * the user's Save and remove clicks.
 */
import { useCallback, useEffect, useState } from 'react'
import { safeGetItem } from '../../../utils/safeStorage'
import { savePrefExplicitly } from '../../../lib/uiPrefs'

export const MEMORY_TOPICS_KEY = 'kirocrew:memory-topics'
/** Same-tab writes fire no `storage` event, so every open list hears this one. */
const CHANGED_EVENT = 'kirocrew:memory-topics-changed'
/** The row stays one short line; past this the oldest topic goes. */
export const MEMORY_TOPICS_LIMIT = 12
const MAX_TOPIC_LENGTH = 200

export function readMemoryTopics(): string[] {
  try {
    const value: unknown = JSON.parse(safeGetItem(MEMORY_TOPICS_KEY) || '[]')
    if (!Array.isArray(value)) return []
    const topics = value.filter((item): item is string => typeof item === 'string' && !!item.trim()).map(item => item.trim().slice(0, MAX_TOPIC_LENGTH))
    return [...new Set(topics)].slice(-MEMORY_TOPICS_LIMIT)
  } catch { return [] }
}

function write(topics: string[]) {
  savePrefExplicitly(MEMORY_TOPICS_KEY, JSON.stringify(topics))
  window.dispatchEvent(new Event(CHANGED_EVENT))
}

export function useMemoryTopics() {
  const [topics, setTopics] = useState(readMemoryTopics)
  useEffect(() => {
    const reread = () => setTopics(readMemoryTopics())
    window.addEventListener(CHANGED_EVENT, reread)
    window.addEventListener('storage', reread)
    return () => { window.removeEventListener(CHANGED_EVENT, reread); window.removeEventListener('storage', reread) }
  }, [])
  const save = useCallback((topic: string) => {
    const next = topic.trim().slice(0, MAX_TOPIC_LENGTH)
    if (!next) return
    const current = readMemoryTopics().filter(item => item !== next)
    write([...current, next].slice(-MEMORY_TOPICS_LIMIT))
  }, [])
  const remove = useCallback((topic: string) => write(readMemoryTopics().filter(item => item !== topic)), [])
  return { topics, save, remove }
}
