/**
 * Hook: fetches GET /api/config/schema and returns a Map<path, SchemaEntry>
 * usable by resolveSettingRef's `schemaIndex` parameter.
 *
 * The schema is essentially static (changes only on version upgrade), so
 * staleTime is Infinity — fetched once per page load.
 */
import { useQuery } from '@tanstack/react-query'
import type { SchemaEntry } from './resolveSettingRef'

interface RawSchemaEntry {
  path: string
  type: string
  label?: string
  help?: string
  tags?: string[]
  enumValues?: string[]
  defaultValue?: unknown
}

async function fetchConfigSchema(): Promise<Map<string, SchemaEntry>> {
  const res = await fetch('/api/config/schema')
  if (!res.ok) throw new Error(`Config schema fetch failed: ${res.status}`)
  const json = (await res.json()) as { entries: RawSchemaEntry[] }
  const map = new Map<string, SchemaEntry>()
  for (const entry of json.entries ?? []) {
    map.set(entry.path, {
      path: entry.path,
      type: entry.type,
      label: entry.label,
      help: entry.help,
      tags: entry.tags,
      enum: entry.enumValues ?? undefined,
      default: entry.defaultValue,
    })
  }
  return map
}

/**
 * Fetches the backend config schema and returns a stable Map<path, SchemaEntry>
 * or `undefined` while the data is not yet available (loading or erroring).
 *
 * - `undefined`: schema not yet known (loading / retrying after failure).
 *   Callers should render optimistic file-mode for valid-shaped keys.
 * - `Map`: schema loaded — callers can positively determine presence/absence.
 *
 * On fetch failure, react-query retries with default exponential backoff.
 */
export function useConfigSchema(): Map<string, SchemaEntry> | undefined {
  const { data } = useQuery<Map<string, SchemaEntry>>({
    queryKey: ['config-schema'],
    queryFn: fetchConfigSchema,
    staleTime: Infinity,
  })
  return data
}
