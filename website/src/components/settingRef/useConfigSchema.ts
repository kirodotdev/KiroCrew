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
  requiresRestart?: boolean
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
      // Carried through as-is: the backend omits the key for every hot field, so
      // `undefined` and `false` both mean "applies live".
      requiresRestart: entry.requiresRestart === true ? true : undefined,
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
  return useConfigSchemaQuery().schema
}

/**
 * The same query with its failure exposed. A caller that renders a schema-driven
 * affordance (the restart badge) must be able to tell "still loading" from
 * "failed": the first is honestly nothing-yet, the second is an error the user
 * is owed through `ErrorNotice` rather than a silently missing hint.
 */
export function useConfigSchemaQuery(): { schema: Map<string, SchemaEntry> | undefined; error: Error | null } {
  const { data, error } = useQuery<Map<string, SchemaEntry>>({
    queryKey: ['config-schema'],
    queryFn: fetchConfigSchema,
    staleTime: Infinity,
  })
  return { schema: data, error: error ?? null }
}
