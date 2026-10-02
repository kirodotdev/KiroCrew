import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import TileList from '../pages/chat/command-center/TileList'
import { buildCommandCenter } from '../pages/chat/command-center/model'

describe('dock Needs you list', () => {
  it('names each waiting [OPTIONS:] session so two asks read as two rows', () => {
    const model = buildCommandCenter({ root: 'root', subagents: {}, workflows: [], questions: [], approvals: [], slots: [
      { key: 'root', title: 'Fix the dock', messages: 2, running: false, has_options: true, options_ts: 't1', options: ['Keep it', 'Change it'] },
      { key: 'child', title: 'Settings copy', created_by: 'root', messages: 2, running: false, has_options: true, options_ts: 't2', options: ['Ship it'] },
    ] })
    renderWithProviders(<TileList tile="attention" data={model} />)
    expect(screen.getByText('Fix the dock')).toBeVisible()
    expect(screen.getByText('Settings copy')).toBeVisible()
    expect(screen.getAllByText('The session is waiting for your choice.')).toHaveLength(2)
  })
})
