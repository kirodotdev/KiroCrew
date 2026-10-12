/**
 * Front-end-only metadata for the built-in tools, used by the Built From → Tools
 * read-only inspector to turn bare tool IDs into something a person can read:
 * a friendly name, a one-line description, a category for grouping, and whether
 * the tool is read-only or can modify/run things.
 *
 * This is a DISPLAY aid, not a source of truth. The read-only set mirrors the
 * backend's `_HOST_READ_ONLY_BUILTIN_TOOLS` (hooks.py) at the time of writing;
 * it is only used to tint a badge, never to make a security decision. An unknown
 * tool id (e.g. an MCP tool) falls back to its id, the "Other" group, and an
 * unknown mutation class (no badge).
 *
 * Human-readable copy lives in the i18n catalog under
 * `components.crew.toolMeta.*` and is resolved lazily (at call time, so the
 * active locale wins) by `toolMeta()` and `toolCategoryLabel()` — the table
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

// Order the category groups render in.
export const TOOL_CATEGORY_ORDER: ToolCategory[] = ['filesystem', 'execution', 'web', 'kirocrew', 'other']

/**
 * Full catalog keys, written as literals (never assembled) so every static
 * tool — the extractor, the dead-key scan, the dynamic-key gate — can see them.
 * An `as const` map indexed by the enum is the codebase's sanctioned shape for
 * "one of N known keys"; `` i18nT(`...${id}`) `` would hide every key from the
 * scan and read as dead. See `i18n/dynamicKeys.test.ts`.
 */
const CATEGORY_LABEL_KEY: Record<ToolCategory, string> = {
  filesystem: 'components.crew.toolMeta.category_filesystem',
  execution: 'components.crew.toolMeta.category_execution',
  web: 'components.crew.toolMeta.category_web',
  kirocrew: 'components.crew.toolMeta.category_kirocrew',
  other: 'components.crew.toolMeta.category_other',
} as const

/** The display label for a category, resolved against the active locale. */
export function toolCategoryLabel(category: ToolCategory): string {
  return i18nT(CATEGORY_LABEL_KEY[category])
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
