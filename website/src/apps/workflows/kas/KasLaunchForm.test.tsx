import { describe, expect, it } from 'vitest'
import { fireEvent, screen, within } from '@testing-library/react'

import { renderWithProviders } from '../../../test/helpers'
import type { KasRecipe } from '../../../api/client'
import { KasLaunchForm } from './KasLaunchForm'

function recipe(over: Partial<KasRecipe> = {}): KasRecipe {
  return {
    name: 'R',
    source: '/p/.kiro/workflows/r.workflow.json',
    description: null,
    builtIn: false,
    readOnly: false,
    tier: null,
    inputs: [{ name: 'task', type: 'prompt' }],
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

const launchBtn = () =>
  within(screen.getByTestId('kas-launch-form'))
    .getAllByRole('button')
    .find(b => (b as HTMLButtonElement).type === 'submit') as HTMLButtonElement

describe('KasLaunchForm — required inputs (gap 17)', () => {
  it('blocks Launch until every declared input is filled', async () => {
    renderWithProviders(<KasLaunchForm recipe={recipe()} project="/p" idPrefix="t" />)
    await screen.findByTestId('kas-launch-form')
    expect(launchBtn()).toBeDisabled()
    expect(screen.getByTestId('kas-input-required-task')).toBeTruthy()
    expect(screen.getByTestId('kas-inputs-required-hint')).toBeTruthy()

    fireEvent.change(screen.getByLabelText(/task/i), { target: { value: 'do the thing' } })
    expect(launchBtn()).not.toBeDisabled()
    expect(screen.queryByTestId('kas-input-required-task')).toBeNull()
  })

  it('blocks Launch for a recipe KAS listed without a source (gap 18)', async () => {
    renderWithProviders(
      <KasLaunchForm recipe={recipe({ source: null, launchable: false, inputs: [] })} project="/p" idPrefix="t" />,
    )
    await screen.findByTestId('kas-launch-form')
    expect(launchBtn()).toBeDisabled()
    expect(screen.getByTestId('kas-not-launchable')).toBeTruthy()
  })
})
