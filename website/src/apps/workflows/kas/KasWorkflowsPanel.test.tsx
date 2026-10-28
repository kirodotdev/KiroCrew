import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { api } from '../../../api/client'
import type { KasRecipe } from '../../../api/client'
import { renderWithProviders } from '../../../test/helpers'
import KasWorkflowsPanel, { KAS_PROJECT_STORAGE_KEY } from './KasWorkflowsPanel'

function recipe(over: Partial<KasRecipe> = {}): KasRecipe {
  return {
    name: 'R',
    source: '/p/.kiro/workflows/r.workflow.json',
    description: null,
    builtIn: false,
    readOnly: false,
    tier: null,
    inputs: [],
    plan: [],
    stepCount: 1,
    loop: null,
    validationError: null,
    scope: 'workspace',
    group: 'workspace',
    launchable: true,
    ...over,
  }
}

const ws = recipe({ name: 'Local', source: '/p/.kiro/workflows/local.workflow.json', group: 'workspace', scope: 'workspace' })
const global = recipe({ name: 'Shared', source: '/home/u/.kiro/workflows/shared.workflow.json', group: 'global', scope: 'global' })

beforeEach(() => {
  localStorage.setItem(KAS_PROJECT_STORAGE_KEY, '/p')
  vi.spyOn(api, 'workflowEngine').mockResolvedValue({ engine: 'kas', backend: 'kas', kas_backend: true, kas_workflows: true })
  vi.spyOn(api, 'kasState').mockResolvedValue({ runs: [], approvals: [], revision: 1, recipesRevision: 1 })
  vi.spyOn(api, 'kasRuns').mockResolvedValue({ runs: [], approvals: [], revision: 1 })
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { kas_creator_effort: '' } } as never)
  vi.spyOn(api, 'kasRecipes').mockResolvedValue({ recipes: [ws, global] })
  vi.spyOn(api, 'kasRecipeDetail').mockResolvedValue({
    recipe: ws,
    steps: [{ nodeId: 'main', agent: 'wf-coder', prompt: 'Do the task.' }],
    definitionAvailable: true,
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('KasWorkflowsPanel recipes (gaps 16F, 18F, 27F)', () => {
  it('groups recipes, shows step prompts, and offers Refresh and New', async () => {
    renderWithProviders(<KasWorkflowsPanel />)
    // Both recipes list under their group headings, in their English text.
    expect(await screen.findByText('This project')).toBeTruthy()
    expect(screen.getByText('Global')).toBeTruthy()
    expect(screen.getAllByText('Local').length).toBeGreaterThan(0)
    expect(screen.getByText('Shared')).toBeTruthy()
    // The selected recipe's detail shows its per-step prompt text (gap 27F).
    expect(await screen.findByTestId('kas-recipe-steps')).toBeTruthy()
    expect(screen.getByText('Do the task.')).toBeTruthy()
    // Refresh (gap 16F) and New (gap 18F) controls are present.
    expect(screen.getByTestId('kas-recipe-refresh')).toBeTruthy()
    expect(screen.getByTestId('kas-recipe-new')).toBeTruthy()
  })

  it('creates a recipe through the New form (gap 18F)', async () => {
    const create = vi.spyOn(api, 'kasCreateRecipe').mockResolvedValue({ name: 'my-wf', source: '/p/.kiro/workflows/my-wf.workflow.json' })
    renderWithProviders(<KasWorkflowsPanel />)
    fireEvent.click(await screen.findByTestId('kas-recipe-new'))
    const name = document.getElementById('kas-new-recipe-name') as HTMLInputElement
    fireEvent.change(name, { target: { value: 'my-wf' } })
    fireEvent.click(screen.getByTestId('kas-recipe-create'))
    await waitFor(() => expect(create).toHaveBeenCalledWith('/p', 'my-wf', 'workspace'))
  })
})
