import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { TeamConfigInvalid } from './PeopleModes'

const invalidMode = (reason: string): CrewModeResponse => ({
  schema_version: 1, mode: 'invalid', crew_id: 'crew-a',
  team_config: { state: 'invalid', reason, capability: '', cause: '' },
  console: { state: 'not_offered', url: '' },
  adapters: {
    tailscale: { state: 'unavailable', reason: 'not_configured' },
    kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
    systems_manager: { state: 'unavailable', reason: 'not_in_this_build' },
  },
  systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable',
})

describe('TeamConfigInvalid', () => {
  it.each(['console_capability_missing', ''])('announces the configuration failure and reason %j without a draft-losing hand-off', reason => {
    render(<TeamConfigInvalid mode={invalidMode(reason)} />)
    const notice = screen.getByRole('alert')
    expect(notice).toBe(screen.getByTestId('people-team-config-invalid'))
    expect(within(notice).getByTestId('team-config-reason')).toHaveTextContent(reason || 'invalid')
    expect(screen.queryByRole('button', { name: 'Ask the agent' })).toBeNull()
  })
})
