import { safeSetItem } from '../utils/safeStorage'
/**
 * Tracks which agent IDs have been seen in each scene.
 * If an agent was previously seen, scenes skip the entrance animation.
 * Uses module-level Map (survives unmount) + localStorage (survives reload).
 */

const STORAGE_KEY = 'mc-scene-known-agents'
const memoryCache = new Map<string, Set<string>>()

function loadFromStorage(): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch { return {} }
}

function saveToStorage() {
  const obj: Record<string, string[]> = {}
  memoryCache.forEach((set, key) => { obj[key] = [...set] })
  try { safeSetItem(STORAGE_KEY, JSON.stringify(obj)) } catch { /* ignore */ }
}

function getOrInit(sceneKey: string): Set<string> {
  if (memoryCache.has(sceneKey)) return memoryCache.get(sceneKey)!
  const stored = loadFromStorage()
  const set = new Set<string>(stored[sceneKey] || [])
  memoryCache.set(sceneKey, set)
  return set
}

/** Check if this agent was previously seen in this scene */
export function isKnownAgent(sceneKey: string, agentId: string): boolean {
  return getOrInit(sceneKey).has(agentId)
}

/** Mark agent IDs as known for this scene and persist */
export function markAgentsKnown(sceneKey: string, agentIds: string[]) {
  const set = getOrInit(sceneKey)
  let changed = false
  for (const id of agentIds) {
    if (!set.has(id)) { set.add(id); changed = true }
  }
  if (changed) saveToStorage()
}

/** Remove agent IDs that are no longer present (skip if currentIds is empty to avoid wiping cache on mount) */
export function pruneAgents(sceneKey: string, currentIds: string[]) {
  if (currentIds.length === 0) return
  const set = getOrInit(sceneKey)
  const currentSet = new Set(currentIds)
  let changed = false
  for (const id of set) {
    if (!currentSet.has(id)) { set.delete(id); changed = true }
  }
  if (changed) saveToStorage()
}
