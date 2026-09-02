/**
 * Prompt text for the Papyrus co-author agent.
 *
 * Kept in its own module because it is model-facing, not user-facing: the skill
 * name, the file paths and the role framing are English identifiers the agent
 * matches on, so this text is deliberately NOT routed through the i18n catalog.
 * Translating it would degrade the agent's instruction-following without changing
 * anything the user ever sees. The user-visible copy for this panel lives in the
 * catalog like every other string.
 */

/** Fallback document name when a project has no main file selected yet. */
export const DEFAULT_MAIN_FILE = 'main.tex'

const LOAD_SKILL_INSTRUCTION =
  'Load the `papyrus-writing` skill for the project path, the compile workflow,'
  + ' and the LaTeX style rules before editing.'

const READ_BEFORE_WRITE_INSTRUCTION =
  'Read a file before you change it — the author is editing it live in the'
  + ' other pane.'

/**
 * The context lines handed to the co-author on session start.
 *
 * The agent needs the project name and the main document; the bundled
 * `papyrus-writing` skill supplies everything else (where projects live, how to
 * compile, the style rules).
 */
export function companionContextLines(project: string, mainFile: string): string[] {
  return [
    `You are the co-author for the Papyrus paper "${project}".`,
    `The main document is ${mainFile || DEFAULT_MAIN_FILE}.`,
    LOAD_SKILL_INSTRUCTION,
    READ_BEFORE_WRITE_INSTRUCTION,
  ]
}
