/**
 * Front-end-only metadata for the built-in tools, used by the Built From → Tools
 * read-only inspector to turn bare tool IDs into something a person can read:
 * a friendly name, a one-line description, a category, and whether the tool is
 * read-only or can modify/run things.
 *
 * This is a DISPLAY aid, not a source of truth. The read-only set mirrors the
 * backend's `_HOST_READ_ONLY_BUILTIN_TOOLS` (hooks.py) at the time of writing;
 * it is only used to tint a badge, never to make a security decision. An unknown
 * tool id (e.g. an MCP tool) falls back to its id, the "Other" group, and an
 * unknown mutation class (no badge).
 *
 * Human-readable copy lives in the i18n catalog under
 * `components.crew.toolMeta.*` and is resolved lazily (at call time, so the
 * active locale wins) by `toolMeta()` — the table
 * itself holds only ids and classification, never translatable literals.
 */
import { i18nT } from '../../i18n/t'

export type ToolCategory = 'filesystem' | 'execution' | 'web' | 'kirocrew' | 'other'

export interface ToolMeta {
  label: string
  description: string
  category: ToolCategory
  /** true = read-only, false = can modify/run, undefined = unknown. */
  readOnly?: boolean
}

// Static classification + full catalog key literals per tool id. The label and
// description keys are written out in full (not assembled) so the static tooling
// can see them; `toolMeta()` resolves them against the active locale at call time.
const TOOL_CLASS: Record<
  string,
  { category: ToolCategory; readOnly?: boolean; labelKey: string; descriptionKey: string }
> = {
  fs_read: {
    category: 'filesystem',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.fs_read_label',
    descriptionKey: 'components.crew.toolMeta.fs_read_description',
  },
  fs_write: {
    category: 'filesystem',
    readOnly: false,
    labelKey: 'components.crew.toolMeta.fs_write_label',
    descriptionKey: 'components.crew.toolMeta.fs_write_description',
  },
  glob: {
    category: 'filesystem',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.glob_label',
    descriptionKey: 'components.crew.toolMeta.glob_description',
  },
  grep: {
    category: 'filesystem',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.grep_label',
    descriptionKey: 'components.crew.toolMeta.grep_description',
  },
  execute_bash: {
    category: 'execution',
    readOnly: false,
    labelKey: 'components.crew.toolMeta.execute_bash_label',
    descriptionKey: 'components.crew.toolMeta.execute_bash_description',
  },
  code: {
    category: 'execution',
    readOnly: false,
    labelKey: 'components.crew.toolMeta.code_label',
    descriptionKey: 'components.crew.toolMeta.code_description',
  },
  web_fetch: {
    category: 'web',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.web_fetch_label',
    descriptionKey: 'components.crew.toolMeta.web_fetch_description',
  },
  web_search: {
    category: 'web',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.web_search_label',
    descriptionKey: 'components.crew.toolMeta.web_search_description',
  },
  introspect: {
    category: 'kirocrew',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.introspect_label',
    descriptionKey: 'components.crew.toolMeta.introspect_description',
  },
  session: {
    category: 'kirocrew',
    labelKey: 'components.crew.toolMeta.session_label',
    descriptionKey: 'components.crew.toolMeta.session_description',
  },
  report: {
    category: 'kirocrew',
    labelKey: 'components.crew.toolMeta.report_label',
    descriptionKey: 'components.crew.toolMeta.report_description',
  },
  tool_search: {
    category: 'kirocrew',
    readOnly: true,
    labelKey: 'components.crew.toolMeta.tool_search_label',
    descriptionKey: 'components.crew.toolMeta.tool_search_description',
  },
}

export function toolMeta(id: string): ToolMeta {
  const cls = TOOL_CLASS[id]
  if (!cls) return { label: id, description: '', category: 'other' }
  return {
    label: i18nT(cls.labelKey),
    description: i18nT(cls.descriptionKey),
    category: cls.category,
    readOnly: cls.readOnly,
  }
}
