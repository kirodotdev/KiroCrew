/**
 * The Task Runner's "Workflows" section, shown when Kiro Agent owns workflows:
 * pick a project and a KAS recipe, fill its inputs, launch it into a chat, and
 * follow recent runs. The Task Runner's own compose / spec / YAML modes are
 * untouched; this sits below them.
 */
import { useMemo, useState } from 'react'
import { Workflow } from 'lucide-react'

import ErrorNotice from '../../../components/ErrorNotice'
import SimpleSelect from '../../../components/SimpleSelect'
import { PanelSectionHeader } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { KasLaunchForm, KasProjectDirInput } from './KasLaunchForm'
import { KasRunList } from './KasRunView'
import { KasRecipeBadges, useRememberedProject } from './KasWorkflowsPanel'
import { KasEngineGate } from './KasEngineGate'
import { useKasRecipes, useKasState } from './useKasWorkflows'

/** How many recent runs the section lists. */
const RECENT_RUNS = 10

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof Error ? error.message : String(error)
}

/** Renders nothing unless KAS owns workflows and the preview is on. */
export default function KasTaskRunnerSection() {
  return (
    <KasEngineGate>
      <KasTaskRunnerSectionBody />
    </KasEngineGate>
  )
}

function KasTaskRunnerSectionBody() {
  const [draft, project, setDraft, commit] = useRememberedProject()
  const recipes = useKasRecipes(project, true)
  // Only launchable recipes (those KAS gave a `source`) can be picked and run
  // here; a sourceless recipe is viewable in Customize › Workflows but not
  // launchable, so it has no place in the launcher's picker.
  const list = useMemo(
    () => (recipes.data?.recipes ?? []).filter((r): r is typeof r & { source: string } => !!r.source),
    [recipes.data],
  )
  const [source, setSource] = useState('')
  const recipe = list.find(r => r.source === source) ?? null
  const state = useKasState(true)

  return (
    <section className="mt-6 border-t border-border pt-4 flex flex-col gap-3" data-testid="kas-task-runner-section">
      <PanelSectionHeader label={i18nT('apps.workflows.kas.task_runner_title')} />
      <p className="m-0 text-[13px] text-muted">
        <Workflow className="lucide-inline" /> {i18nT('apps.workflows.kas.task_runner_description')}
      </p>
      <KasProjectDirInput id="kas-task-runner-project" value={draft} onChange={setDraft} onCommit={commit} />
      {project ? (
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor="kas-task-runner-recipe" className="text-[13px] text-muted font-medium">
            {i18nT('apps.workflows.kas.recipe')}
          </label>
          <SimpleSelect
            id="kas-task-runner-recipe"
            aria-label={i18nT('apps.workflows.kas.recipe')}
            style={{ flex: '0 1 280px', minWidth: 200 }}
            options={list.map(r => r.source)}
            optionLabels={list.map(r => r.name)}
            value={recipe?.source ?? ''}
            onChange={setSource}
            disabled={recipes.isLoading || list.length === 0}
            triggerFallback={
              recipes.isLoading
                ? i18nT('apps.workflows.kas.loading_recipes')
                : list.length === 0
                  ? i18nT('apps.workflows.kas.no_recipes')
                  : i18nT('apps.workflows.kas.choose_recipe')
            }
          />
          {recipe ? <KasRecipeBadges recipe={recipe} /> : null}
        </div>
      ) : (
        <p className="m-0 text-[12px] text-muted">{i18nT('apps.workflows.kas.pick_project')}</p>
      )}
      {/* No hand-off: the Task Runner's compose drafts sit above this section. */}
      <ErrorNotice message={errorText(recipes.error)} variant="inline" />
      {recipe ? (
        <>
          {recipe.description ? <p className="m-0 text-[13px] text-text">{recipe.description}</p> : null}
          {recipe.validationError ? (
            <ErrorNotice title={i18nT('apps.workflows.kas.invalid')} message={recipe.validationError} variant="inline" />
          ) : null}
          <KasLaunchForm key={recipe.source} recipe={recipe} project={project} idPrefix="kas-task-runner" />
        </>
      ) : null}
      <h4 className="m-0 mt-2 text-sm font-semibold text-text-strong">{i18nT('apps.workflows.kas.recent_runs')}</h4>
      {/* No hand-off: the Task Runner's compose drafts sit above this section. */}
      <ErrorNotice message={errorText(state.error)} variant="inline" />
      <KasRunList
        runs={state.data?.runs ?? []}
        approvals={state.data?.approvals ?? []}
        emptyText={i18nT('apps.workflows.kas.no_runs')}
        limit={RECENT_RUNS}
      />
    </section>
  )
}
