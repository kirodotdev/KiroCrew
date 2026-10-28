/**
 * Customize > Workflows in Kiro Agent mode: pick a project, see the recipes KAS
 * can run there, launch one into a chat, and follow the project's runs.
 *
 * Rendered by `WorkflowLibraryTab` only when KAS owns workflows (and the
 * preview is on); the Crew library stays one segment away as "Crew scripts".
 */
import { useMemo, useState } from 'react'
import { FilePlus, Loader2, Lock, PenLine, RefreshCw, Trash2, Workflow } from 'lucide-react'

import type { KasNodeDescriptor, KasRecipe, KasRunRow } from '../../../api/client'
import { Badge, Btn, Input } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import SimpleSelect from '../../../components/SimpleSelect'
import { useConfirm } from '../../../components/ConfirmDialog'
import { revealOrOpen, useCanOpenFile, useRevealFailure } from '../../../components/FilePathMenu'
import { i18nT } from '../../../i18n/t'
import { safeGetItem, safeSetItem } from '../../../utils/safeStorage'
import { KasLaunchForm, KasProjectDirInput } from './KasLaunchForm'
import { KasRunList } from './KasRunView'
import { KasCreatorEffortSetting, KasDockModeSetting } from './KasWorkflowSettings'
import { useKasProjectRuns, useKasState } from './useKasWorkflows'
import { useKasRecipeDetail, useKasRecipeMutations, useKasRecipesList } from './useKasRecipes'

/** Remembers the last project per device, so returning to the tab lands where you were. */
export const KAS_PROJECT_STORAGE_KEY = 'mc-kas-workflows-project'

export function useRememberedProject(): [string, string, (v: string) => void, (v: string) => void] {
  const [draft, setDraft] = useState(() => safeGetItem(KAS_PROJECT_STORAGE_KEY) ?? '')
  const [project, setProject] = useState(draft)
  const commit = (value: string) => {
    setProject(value)
    setDraft(value)
    safeSetItem(KAS_PROJECT_STORAGE_KEY, value)
  }
  return [draft, project, setDraft, commit]
}

/** The project's runs: its disk history, each row replaced by its live copy
 *  from the state poll when the router holds one. */
export function useProjectRunRows(project: string) {
  const state = useKasState(true)
  const knownIds = useMemo(
    () => (state.data?.runs ?? []).map(r => r.workflowId).sort().join(','),
    [state.data],
  )
  const history = useKasProjectRuns(project, true, knownIds)
  const rows = useMemo<KasRunRow[]>(() => {
    const live = new Map((state.data?.runs ?? []).map(r => [r.workflowId, r]))
    return (history.data?.runs ?? []).map(r => live.get(r.workflowId) ?? r)
  }, [history.data, state.data])
  const approvals = state.data?.approvals ?? history.data?.approvals ?? []
  return { rows, approvals, error: history.error ?? state.error, loading: history.isLoading }
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof Error ? error.message : String(error)
}

/** A stable id for a recipe in the list. `source` is the natural key, but a
 *  non-launchable recipe has none, so fall back to a name-derived key. */
function recipeKey(recipe: KasRecipe): string {
  return recipe.source ?? `name:${recipe.name}`
}

export default function KasWorkflowsPanel() {
  const [draft, project, setDraft, commit] = useRememberedProject()
  const recipes = useKasRecipesList(project, true)
  const list = recipes.recipes
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const selected = list.find(r => recipeKey(r) === selectedKey) ?? list[0] ?? null
  const runs = useProjectRunRows(project)

  return (
    <div className="flex flex-col gap-4" data-testid="kas-workflows-panel">
      <p className="text-[13px] text-muted m-0">{i18nT('apps.workflows.kas.panel_description')}</p>
      <KasProjectDirInput id="kas-workflows-project" value={draft} onChange={setDraft} onCommit={commit} />

      {!project ? (
        <p className="text-[12px] text-muted m-0">{i18nT('apps.workflows.kas.pick_project')}</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-[260px_minmax(0,1fr)] gap-4">
          <aside className="min-w-0 border border-border rounded-lg p-2 bg-bg-elevated" aria-label={i18nT('apps.workflows.kas.recipes')}>
            <KasRecipeList
              recipes={list}
              loading={recipes.isLoading}
              error={recipes.error}
              project={project}
              selectedKey={selected ? recipeKey(selected) : null}
              onSelect={setSelectedKey}
              onRefresh={recipes.refresh}
              onCreated={source => setSelectedKey(source)}
            />
          </aside>
          <section className="min-w-0">
            {selected ? (
              <KasRecipeDetail
                recipe={selected}
                project={project}
                onDeleted={() => setSelectedKey(null)}
              />
            ) : null}
          </section>
        </div>
      )}

      {project ? (
        <section className="flex flex-col gap-2" aria-label={i18nT('apps.workflows.kas.project_runs')}>
          <h4 className="m-0 text-sm font-semibold text-text-strong">{i18nT('apps.workflows.kas.project_runs')}</h4>
          {/* No hand-off: the launch form above holds unsubmitted inputs. */}
          <ErrorNotice message={errorText(runs.error)} variant="inline" />
          <KasRunList runs={runs.rows} approvals={runs.approvals} emptyText={i18nT('apps.workflows.kas.no_runs')} />
        </section>
      ) : null}

      {/* Kiro Agent workflow settings. They moved here from the Feature Previews
          card when that collapsed to a single opt-in: the creator-step effort
          (gateway-wide, applied per chat session) and how several live workflows
          in one chat collapse (device-local). This panel only renders behind
          `KasEngineGate`, so these show only while the switch is on and the
          backend is Kiro Agent — exactly their old visibility. */}
      <div className="flex flex-col gap-2 border-t border-border pt-3">
        <KasCreatorEffortSetting />
        <KasDockModeSetting />
      </div>
    </div>
  )
}

/**
 * The recipe list: a Refresh and a New-recipe control at the top, then the
 * recipes split into "This project" and "Global" groups (gap 27). The list
 * re-reads itself when KAS reports the set on disk changed, and Refresh forces
 * a re-read (gap 16).
 */
function KasRecipeList({
  recipes,
  loading,
  error,
  project,
  selectedKey,
  onSelect,
  onRefresh,
  onCreated,
}: {
  recipes: KasRecipe[]
  loading: boolean
  error: unknown
  project: string
  selectedKey: string | null
  onSelect: (key: string) => void
  onRefresh: () => void
  onCreated: (source: string) => void
}) {
  const workspace = recipes.filter(r => r.group === 'workspace')
  const global = recipes.filter(r => r.group !== 'workspace')
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-1 mb-1">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">
          {i18nT('apps.workflows.kas.recipes')}
        </span>
        <Btn
          onClick={onRefresh}
          title={i18nT('apps.workflows.kas.refresh_recipes')}
          aria-label={i18nT('apps.workflows.kas.refresh_recipes')}
          className="px-1.5 py-1"
          data-testid="kas-recipe-refresh"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} aria-hidden="true" />
        </Btn>
      </div>

      <KasNewRecipe project={project} onCreated={onCreated} />

      {loading && recipes.length === 0 ? (
        <p className="text-[12px] text-muted p-2 m-0">{i18nT('apps.workflows.kas.loading_recipes')}</p>
      ) : recipes.length === 0 && !error ? (
        <p className="text-[12px] text-muted p-2 m-0">{i18nT('apps.workflows.kas.no_recipes')}</p>
      ) : (
        <>
          {workspace.length > 0 ? (
            <KasRecipeGroup
              heading={i18nT('apps.workflows.kas.group_workspace')}
              recipes={workspace}
              selectedKey={selectedKey}
              onSelect={onSelect}
            />
          ) : null}
          {global.length > 0 ? (
            <KasRecipeGroup
              heading={i18nT('apps.workflows.kas.group_global')}
              recipes={global}
              selectedKey={selectedKey}
              onSelect={onSelect}
            />
          ) : null}
        </>
      )}
      {/* No hand-off: the project field above may hold an uncommitted draft. */}
      <ErrorNotice message={errorText(error)} variant="inline" />
    </div>
  )
}

function KasRecipeGroup({
  heading,
  recipes,
  selectedKey,
  onSelect,
}: {
  heading: string
  recipes: KasRecipe[]
  selectedKey: string | null
  onSelect: (key: string) => void
}) {
  return (
    <div className="mb-1" role="group" aria-label={heading}>
      <p className="m-0 mt-1 mb-0.5 px-1 text-[10.5px] font-semibold uppercase tracking-wide text-muted">{heading}</p>
      {recipes.map(recipe => {
        const key = recipeKey(recipe)
        return (
          <Btn
            key={key}
            onClick={() => onSelect(key)}
            aria-pressed={selectedKey === key}
            className={`w-full mb-1 px-2.5 py-2 justify-start text-left block ${
              selectedKey === key ? 'bg-accent-subtle border-accent/40' : ''
            }`}
          >
            <span className="flex items-center gap-1.5 font-semibold min-w-0">
              <span className="truncate">{recipe.name}</span>
              {recipe.readOnly ? <Lock size={12} className="shrink-0 text-muted" aria-label={i18nT('apps.workflows.kas.read_only')} /> : null}
            </span>
            <span className="block text-[11px] text-muted truncate">
              {recipe.validationError
                ? i18nT('apps.workflows.kas.invalid')
                : i18nT('apps.workflows.kas.steps_label', { n: recipe.stepCount })}
            </span>
          </Btn>
        )
      })}
    </div>
  )
}

/** "New recipe": a name and a scope (this project / global), which writes a
 *  scaffold `<name>.workflow.json` the user can then edit (gap 18). */
function KasNewRecipe({ project, onCreated }: { project: string; onCreated: (source: string) => void }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [scope, setScope] = useState<'workspace' | 'global'>('workspace')
  const { create } = useKasRecipeMutations(project)
  // The same safe-basename rule the backend enforces, mirrored for an inline hint.
  const valid = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(name.trim())
  const error = create.error ? (create.error instanceof Error ? create.error.message : String(create.error)) : null

  const submit = () => {
    if (!valid || create.isPending) return
    create.mutate(
      { name: name.trim(), scope },
      {
        onSuccess: result => {
          setName('')
          setOpen(false)
          onCreated(result.source)
        },
      },
    )
  }

  if (!open) {
    return (
      <Btn
        onClick={() => setOpen(true)}
        className="w-full mb-1 px-2.5 py-1.5 justify-start text-left"
        data-testid="kas-recipe-new"
      >
        <FilePlus size={13} className="lucide-inline" /> {i18nT('apps.workflows.kas.new_recipe')}
      </Btn>
    )
  }

  return (
    <form
      className="flex flex-col gap-2 mb-2 p-2 border border-border rounded-md bg-bg"
      data-testid="kas-recipe-new-form"
      onSubmit={e => {
        e.preventDefault()
        submit()
      }}
    >
      <label htmlFor="kas-new-recipe-name" className="block text-[12px] text-muted">
        <span className="block mb-1">{i18nT('apps.workflows.kas.new_recipe_name')}</span>
        <Input
          id="kas-new-recipe-name"
          className="w-full font-mono"
          value={name}
          autoFocus
          placeholder={i18nT('apps.workflows.kas.new_recipe_name_placeholder')}
          onChange={e => setName(e.target.value)}
        />
      </label>
      <div className="text-[12px] text-muted">
        <label htmlFor="kas-new-recipe-scope" className="block mb-1">
          {i18nT('apps.workflows.kas.new_recipe_scope')}
        </label>
        <SimpleSelect
          id="kas-new-recipe-scope"
          options={['workspace', 'global']}
          optionLabels={[i18nT('apps.workflows.kas.scope_workspace'), i18nT('apps.workflows.kas.scope_global')]}
          value={scope}
          onChange={v => setScope(v === 'global' ? 'global' : 'workspace')}
          aria-label={i18nT('apps.workflows.kas.new_recipe_scope')}
          triggerFallback={
            scope === 'global' ? i18nT('apps.workflows.kas.scope_global') : i18nT('apps.workflows.kas.scope_workspace')
          }
        />
      </div>
      {name.trim() && !valid ? (
        <p className="m-0 text-[11px] text-warn" role="alert">{i18nT('apps.workflows.kas.new_recipe_name_invalid')}</p>
      ) : null}
      <div className="flex items-center gap-2">
        <Btn primary type="submit" disabled={!valid || create.isPending} data-testid="kas-recipe-create">
          {create.isPending ? <Loader2 className="lucide-inline animate-spin" /> : <FilePlus className="lucide-inline" />}
          {i18nT('apps.workflows.kas.create_recipe')}
        </Btn>
        <Btn type="button" onClick={() => { setOpen(false); setName('') }} disabled={create.isPending}>
          {i18nT('apps.workflows.kas.new_recipe_cancel')}
        </Btn>
      </div>
      {/* No hand-off: the recipe-name field above is an unsaved draft. */}
      <ErrorNotice message={error} variant="inline" />
    </form>
  )
}

export function KasRecipeBadges({ recipe }: { recipe: KasRecipe }) {
  return (
    <>
      {recipe.builtIn ? <Badge variant="aim">{i18nT('apps.workflows.kas.built_in')}</Badge> : null}
      {recipe.readOnly ? <Badge variant="muted">{i18nT('apps.workflows.kas.read_only')}</Badge> : null}
      {recipe.tier ? <Badge variant="muted">{recipe.tier}</Badge> : null}
    </>
  )
}

function KasRecipeDetail({
  recipe,
  project,
  onDeleted,
}: {
  recipe: KasRecipe
  project: string
  onDeleted: () => void
}) {
  // Only a launchable (file-backed) recipe has per-step prompt text to read.
  const detail = useKasRecipeDetail(project, recipe.launchable ? recipe.source : null, true)
  const steps = detail.data?.steps ?? []
  const definitionAvailable = detail.data?.definitionAvailable ?? false

  return (
    <div className="flex flex-col gap-3" data-testid="kas-recipe-detail">
      <div className="flex flex-wrap items-center gap-2">
        <Workflow className="lucide-inline text-accent" />
        <h4 className="m-0 text-base font-semibold text-text-strong">{recipe.name}</h4>
        <KasRecipeBadges recipe={recipe} />
        <span className="ml-auto flex items-center gap-1.5">
          {!recipe.readOnly && recipe.source ? (
            <KasRecipeFileActions recipe={recipe} source={recipe.source} project={project} onDeleted={onDeleted} />
          ) : null}
        </span>
      </div>
      <p className="m-0 text-[12px] text-muted">
        {i18nT('apps.workflows.kas.steps_label', { n: recipe.stepCount })}
        {recipe.loop ? (
          <span className="ml-1.5">· {i18nT('apps.workflows.kas.review_loop_max', { max: recipe.loop.maxIterations })}</span>
        ) : null}
      </p>
      {recipe.description ? <p className="m-0 text-[13px] text-text">{recipe.description}</p> : null}
      {recipe.validationError ? (
        // A recipe KAS cannot parse: its own error, verbatim, and no Launch.
        <ErrorNotice title={i18nT('apps.workflows.kas.invalid')} message={recipe.validationError} variant="inline" />
      ) : null}

      {/* Per-step prompt text when the file is readable (gap 27); otherwise the
          plan's structure alone, with a note that step descriptions are
          unavailable (bundled / YAML recipes KAS does not expand for us). */}
      {definitionAvailable && steps.length > 0 ? (
        <KasRecipeSteps steps={steps} />
      ) : (
        <>
          {recipe.plan.length > 0 ? <KasPlanOutline nodes={recipe.plan} /> : null}
          {detail.data && !definitionAvailable ? (
            <p className="m-0 text-[12px] text-muted" data-testid="kas-recipe-steps-unavailable">
              {i18nT('apps.workflows.kas.recipe_steps_unavailable')}
            </p>
          ) : null}
        </>
      )}

      <KasLaunchForm key={recipe.source ?? recipe.name} recipe={recipe} project={project} idPrefix={`kas-recipe-${recipe.name}`} />
    </div>
  )
}

/** Edit (open the recipe file) and Delete, for a file-backed recipe (gap 18). */
function KasRecipeFileActions({
  recipe,
  source,
  project,
  onDeleted,
}: {
  recipe: KasRecipe
  /** The recipe's file path; guaranteed non-null by the call site. */
  source: string
  project: string
  onDeleted: () => void
}) {
  const { remove } = useKasRecipeMutations(project)
  const { confirm, confirmDialog } = useConfirm()
  const canOpen = useCanOpenFile('file')
  const { error: revealError, onError, clear } = useRevealFailure(source)

  const edit = () => {
    clear()
    // Open the file in the OS default app when the browser shares the gateway's
    // machine; otherwise reveal it in the file manager. Both act on the real
    // recipe file at `source`.
    void revealOrOpen(source, canOpen ? 'open' : 'reveal', { onError })
  }

  const del = async () => {
    const ok = await confirm({
      title: i18nT('apps.workflows.kas.delete_recipe_title'),
      body: i18nT('apps.workflows.kas.delete_recipe_body', { name: recipe.name }),
      confirmLabel: i18nT('apps.workflows.kas.delete_recipe_confirm'),
      danger: true,
    })
    if (!ok) return
    remove.mutate({ source }, { onSuccess: onDeleted })
  }

  const removeError = remove.error ? (remove.error instanceof Error ? remove.error.message : String(remove.error)) : null

  return (
    <>
      <Btn onClick={edit} className="px-2 py-1" data-testid="kas-recipe-edit">
        <PenLine size={13} className="lucide-inline" /> {i18nT('apps.workflows.kas.edit_recipe')}
      </Btn>
      <Btn onClick={del} disabled={remove.isPending} className="px-2 py-1" data-testid="kas-recipe-delete">
        {remove.isPending ? <Loader2 size={13} className="lucide-inline animate-spin" /> : <Trash2 size={13} className="lucide-inline" />}
        {i18nT('apps.workflows.kas.delete_recipe')}
      </Btn>
      {confirmDialog}
      {/* No hand-off: a reveal/delete failure is an action result, not an unsaved draft. */}
      <ErrorNotice message={revealError ?? removeError} variant="inline" onDismiss={clear} />
    </>
  )
}

/** A file-backed recipe's steps, numbered, each with its agent and prompt. */
function KasRecipeSteps({ steps }: { steps: { nodeId: string; agent: string | null; prompt: string | null }[] }) {
  return (
    <ol className="m-0 p-0 list-none flex flex-col gap-2" aria-label={i18nT('apps.workflows.kas.steps')} data-testid="kas-recipe-steps">
      {steps.map((step, i) => (
        <li key={step.nodeId} className="flex gap-2 min-w-0">
          <span className="shrink-0 text-[11px] font-semibold text-muted tabular-nums mt-[2px]">{i + 1}.</span>
          <div className="min-w-0 flex-1">
            <span className="flex items-center gap-1.5 text-[12px] font-semibold text-text min-w-0">
              <span className="truncate">{step.nodeId}</span>
              {step.agent ? <span className="text-[11px] font-normal text-muted truncate">{step.agent}</span> : null}
            </span>
            {step.prompt ? (
              <p className="m-0 mt-0.5 text-[12px] text-muted whitespace-pre-wrap break-words">{step.prompt}</p>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  )
}

/** A recipe's plan before it runs: the step tree with no state. */
export function KasPlanOutline({ nodes, depth = 0 }: { nodes: readonly KasNodeDescriptor[]; depth?: number }) {
  return (
    <ol
      className="m-0 p-0 list-none flex flex-col gap-0.5"
      aria-label={depth === 0 ? i18nT('apps.workflows.kas.steps') : undefined}
    >
      {nodes.map(node => (
        <li key={node.id}>
          <span
            className={`block text-[12px] ${node.type === 'step' ? 'text-text' : 'text-muted'}`}
            style={{ marginLeft: depth * 14 }}
          >
            {node.label || node.agent || node.id}
            {node.type === 'repeat' && node.maxIterations ? (
              <span className="ml-1.5 text-[11px] text-muted tabular-nums">
                {i18nT('apps.workflows.kas.max_iterations', { max: node.maxIterations })}
              </span>
            ) : null}
          </span>
          {node.children.length > 0 ? <KasPlanOutline nodes={node.children} depth={depth + 1} /> : null}
        </li>
      ))}
    </ol>
  )
}
