import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import WorkflowRunGraph from './WorkflowRunGraph'
import type { RunPlan } from './planModel'
import { i18next, initI18n } from '../../i18n/all'
import { renderWithProviders } from '../../test/helpers'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

function plan(title: string, label: string): RunPlan {
  return {
    phases: [{ title, certain: true, nodes: [{ kind: 'agent', label, certain: true }] }],
    truncated: false,
  }
}

async function render(title: string, label: string) {
  await initI18n()
  renderWithProviders(<WorkflowRunGraph events={[]} plan={plan(title, label)} status="running" />)
  const phase = screen.getByTestId('workflow-graph-phase')
  return {
    title: phase.querySelector('span.flex-1')?.textContent,
    label: screen.getByTestId('workflow-graph-node').querySelector('span.font-mono')?.textContent,
  }
}

describe('WorkflowRunGraph labels with astral characters', () => {
  it('drops an emoji the 100-unit label cap would cut in half instead of keeping its high surrogate', async () => {
    // 99 ASCII units, then an emoji occupying UTF-16 units 99-100.
    const { label } = await render('Research', `${'a'.repeat(99)}😀 and more`)
    expect(label).toBe('a'.repeat(99))
  })

  it('drops an emoji the 80-unit phase title cap would cut in half', async () => {
    // 79 ASCII units, then an emoji occupying UTF-16 units 79-80.
    const { title } = await render(`${'t'.repeat(79)}🚀 and more`, 'spec')
    expect(title).toBe('t'.repeat(79))
  })

  it('keeps an emoji that ends exactly at either cap', async () => {
    const { title, label } = await render(`${'t'.repeat(78)}🚀 and more`, `${'a'.repeat(98)}😀 and more`)
    expect(title).toBe(`${'t'.repeat(78)}🚀`)
    expect(label).toBe(`${'a'.repeat(98)}😀`)
  })

  it('leaves long ASCII at the caps', async () => {
    const { title, label } = await render('t'.repeat(120), 'a'.repeat(140))
    expect(title).toBe('t'.repeat(80))
    expect(label).toBe('a'.repeat(100))
  })
})
