/**
 * The language-neutral description of what one tool call does — the
 * structure `toolCallTitle.ts` renders into a localized title. Produced by
 * `shellCommandParse.ts` for shell commands and by `toolCallTitle.ts` for
 * native and MCP tools; mirrored field-for-field by
 * `src/kiro_crew/tool_call_title.py`, and pinned by
 * `test/fixtures/tool_call_titles.json`.
 *
 * Optional fields are OMITTED when absent (never `undefined`-valued), so a
 * JSON comparison against the Python side is exact.
 */
export type ToolAction =
  | { type: 'read'; path: string; from?: number; to?: number }
  | { type: 'list_files'; path?: string }
  | { type: 'search'; query: string; path?: string }
  | { type: 'find_files'; pattern: string; path?: string }
  | { type: 'git'; sub: string; path?: string }
  | { type: 'install' }
  | { type: 'test' }
  | { type: 'script'; name: string }
  | { type: 'build' }
  | { type: 'lint' }
  | { type: 'print' }
  | { type: 'github'; noun: string; verb: string; target?: string }
  | { type: 'github_api'; path: string }
  | { type: 'fetch'; host: string; method?: string }
  | { type: 'search_web'; query: string }
  | { type: 'edit'; path: string }
  | { type: 'create'; path: string }
  | { type: 'view_image'; path: string }
  | { type: 'mcp'; tool: string; arg?: string }
  | { type: 'unknown'; cmd: string }
