/**
 * Gap 15: the Kiro Agent workflow slash commands — `/workflow-run`,
 * `/workflow-resume`, `/workflow-status`, `/workflow-cancel` — handled by the
 * client the way the IDE handles them, instead of reaching the model as prose
 * (Crew drops KAS's `available_commands_update`, so a typed `/workflow-run foo`
 * would otherwise be sent verbatim and the model might call `run_workflow`
 * itself, unvalidated).
 *
 * These names deliberately carry a hyphen so they NEVER collide with Crew's own
 * `/workflow` (the saved dynamic-workflow runner, `dashboard/chat_utils.py`) or
 * `/workflows`: `/workflow` is an exact token and `/workflow-run` is a different
 * one. The commands are only offered while the KAS preview is on (see
 * `SlashCommandMenu`), so a non-KAS chat shows none of them.
 *
 * The parser is pure; the executor takes injected effects so both are tested
 * without a store or a network.
 */
import type { KasRecipe, KasRunAction, KasRunRow } from '../../api/client'

/** The four KAS workflow commands, as the menu lists them. */
export const KAS_WORKFLOW_COMMAND_NAMES = [
  '/workflow-run',
  '/workflow-resume',
  '/workflow-status',
  '/workflow-cancel',
] as const

export type KasWorkflowCommand =
  | { verb: 'run'; recipe: string; inputs: Record<string, string> }
  | { verb: 'resume' | 'cancel'; runId: string | null }
  | { verb: 'status' }

// One token: `/workflow-<verb>` then the rest of the line. The verb set is
// closed, so `/workflow-foo` is NOT one of these (returns null, falls through).
const KAS_CMD_RE = /^\/workflow-(run|resume|status|cancel)(?:\s+([\s\S]+))?$/

/** `key=value` / `key="quoted value"` pairs, the shape the IDE's recipe form
 *  serialises inputs as. Bare tokens (no `=`) are ignored. */
function parseInputs(rest: string): Record<string, string> {
  const out: Record<string, string> = {}
  const re = /([A-Za-z_][A-Za-z0-9_-]*)=(?:"([^"]*)"|(\S+))/g
  let m: RegExpExecArray | null
  while ((m = re.exec(rest)) !== null) {
    out[m[1]] = m[2] ?? m[3] ?? ''
  }
  return out
}

/** Parse composer text into a KAS workflow command, or null when it is not one. */
export function parseKasWorkflowCommand(raw: string): KasWorkflowCommand | null {
  const m = raw.trim().match(KAS_CMD_RE)
  if (!m) return null
  const verb = m[1]
  const rest = (m[2] ?? '').trim()
  if (verb === 'status') return { verb: 'status' }
  if (verb === 'resume' || verb === 'cancel') return { verb, runId: rest || null }
  // run: the first whitespace-delimited token is the recipe source/name; the
  // remainder is its `key=value` inputs.
  const firstSpace = rest.search(/\s/)
  const recipe = firstSpace === -1 ? rest : rest.slice(0, firstSpace)
  const inputs = firstSpace === -1 ? {} : parseInputs(rest.slice(firstSpace + 1))
  return { verb: 'run', recipe, inputs }
}

/** Whether composer text is any KAS workflow command (cheap sync gate). */
export function isKasWorkflowCommand(raw: string): boolean {
  return KAS_CMD_RE.test(raw.trim())
}

/** The effects the executor needs, injected so it is testable. */
export interface KasWorkflowCommandCtx {
  slot: string | null
  /** This chat's runs (already narrowed to its slot). */
  listRuns: () => Promise<readonly KasRunRow[]>
  control: (workflowId: string, action: KasRunAction) => Promise<unknown>
  /** Resolve the recipe the user named (by display name or opaque source) for
   *  this chat's project. Null when there is no project, no match, or several
   *  matches — the caller then opens the launch form. */
  resolveRecipe: (nameOrSource: string) => Promise<KasRecipe | null>
  /** Launch a resolved recipe with its inputs (the `launchKasWorkflow` flow). */
  launch: (recipe: KasRecipe, inputs: Record<string, string>) => Promise<unknown>
  /** Bring the Workflows view on screen — `/workflow-status`, and the launch
   *  form fallback for a `/workflow-run` that cannot launch as typed. */
  openWorkflows: () => void
}

export interface KasWorkflowCommandResult {
  failed?: boolean
  /** A short, already-localized reason when `failed` — shown on the composer. */
  error?: string
}

const TERMINAL = new Set(['completed', 'failed', 'aborted'])

/**
 * Resolve which run a bare `/workflow-resume` / `/workflow-cancel` (no id)
 * targets: a run id wins if given; otherwise the chat's single live run. Zero
 * or several live runs with no id is ambiguous — the caller reports it rather
 * than guessing.
 */
export function resolveRunTarget(
  runId: string | null,
  runs: readonly KasRunRow[],
): { ok: true; runId: string } | { ok: false; reason: 'none' | 'ambiguous' } {
  if (runId) return { ok: true, runId }
  const live = runs.filter(r => !TERMINAL.has(r.status))
  if (live.length === 0) return { ok: false, reason: 'none' }
  if (live.length > 1) return { ok: false, reason: 'ambiguous' }
  return { ok: true, runId: live[0].workflowId }
}

/** Run a parsed KAS workflow command. Returns a plain result the caller maps
 *  onto its own intercept type. Localized reasons are passed in so this module
 *  carries no i18n dependency (and the tests assert on the keys the caller
 *  hands it). */
export async function runKasWorkflowCommand(
  cmd: KasWorkflowCommand,
  ctx: KasWorkflowCommandCtx,
  reasons: { noSlot: string; noRun: string; ambiguous: string },
): Promise<KasWorkflowCommandResult> {
  if (cmd.verb === 'status') {
    ctx.openWorkflows()
    return {}
  }
  if (cmd.verb === 'run') {
    // Launch the named recipe with the typed inputs. Fall back to the launch
    // form only when we cannot launch AS TYPED: no such recipe (or ambiguous /
    // no project), an invalid recipe, or a declared input the command did not
    // supply — the IDE treats every declared input as required, so a blank one
    // sends the user to the form rather than starting an under-specified run.
    const recipe = await ctx.resolveRecipe(cmd.recipe)
    if (!recipe || recipe.validationError) {
      ctx.openWorkflows()
      return {}
    }
    const missing = recipe.inputs.some(input => {
      const value = cmd.inputs[input.name]
      return value === undefined || value.trim() === ''
    })
    if (missing) {
      ctx.openWorkflows()
      return {}
    }
    await ctx.launch(recipe, cmd.inputs)
    return {}
  }
  if (!ctx.slot) return { failed: true, error: reasons.noSlot }
  const runs = await ctx.listRuns()
  const target = resolveRunTarget(cmd.runId, runs)
  if (!target.ok) {
    return { failed: true, error: target.reason === 'none' ? reasons.noRun : reasons.ambiguous }
  }
  await ctx.control(target.runId, cmd.verb)
  return {}
}
