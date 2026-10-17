/** Extract the target filesystem path from a tool call's JSON-encoded args.
 *
 * `input` is the raw JSON string of a tool's arguments (as stored on the
 * toolLog / message meta). We pull the first plausible fs path so the tool
 * pill can offer an "open in side panel" affordance for file-op tools
 * (read / edit / write) while staying silent for non-file tools (bash,
 * search, url fetch, ...).
 *
 * Recognized shapes, in order:
 *   1. top-level `path` / `file_path` / `filePath`
 *   2. `operations[].path` (multi-edit tools)
 *   3. `files[].path` (multi-file tools)
 *
 * Returns null when: input isn't valid JSON, no recognized field is present,
 * or the candidate is empty or an http(s):// URL (a fetch target, not a file).
 */
export function extractToolFilePath(input: string): string | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(input)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const obj = parsed as Record<string, unknown>

  const candidate = (v: unknown): string | null => {
    if (typeof v !== 'string') return null
    const s = v.trim()
    if (!s) return null
    if (/^https?:\/\//i.test(s)) return null
    return s
  }

  // 1. top-level path fields
  for (const key of ['path', 'file_path', 'filePath'] as const) {
    const c = candidate(obj[key])
    if (c) return c
  }

  // 2. operations[].path / 3. files[].path
  for (const key of ['operations', 'files'] as const) {
    const arr = obj[key]
    if (!Array.isArray(arr)) continue
    for (const item of arr) {
      if (item && typeof item === 'object') {
        const c = candidate((item as Record<string, unknown>).path)
        if (c) return c
      }
    }
  }

  return null
}
