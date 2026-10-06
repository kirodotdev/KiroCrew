import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'

/**
 * A shadowed collision row explains the override on its detail line. That line
 * used to open with `{{name}}` — but the row title directly above already
 * shows the name, so repeating it pushed the actual meaning ("…instead of your
 * global agent") past the line's own `truncate` and rendered "…of this n…" on
 * the 400px popover (UX Review span=7ddb24260c79). The detail now leads with
 * the meaning and the name lives in the title alone.
 */
describe('AgentSelector override-detail line does not repeat the agent name', () => {
  const collision: KiroCrewAgent[] = [
    {
      name: 'reviewer', kiro_agent: '', workspace: 'default', memory_store: 'default',
      description: '', source: 'kirocrew', scope: 'project',
    },
  ]

  it('states the override starting from the meaning, not the name', () => {
    render(
      <AgentSelector
        agents={collision}
        defaultAgent="default"
        value=""
        onChange={() => {}}
        shadowedGlobals={new Set(['reviewer'])}
      />,
    )
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const row = screen.getByRole('option', { name: /reviewer/ })
    // A shadowed row shows the full explanation as body text, not as a native
    // `title` on the chip: a tooltip has no keyboard path and no hover on
    // touch, and this sentence is the only thing telling a user why their
    // configured agent is not the one running. It is also the string every
    // catalog already translates, so non-English users get the same reassurance.
    expect(row).toHaveTextContent(
      "This job's project directory defines its own agent with this name, so it runs "
      + 'instead of your global one. Your global agent is unchanged elsewhere.',
    )
    // The detail does not begin with the agent name (which would read as
    // "reviewer runs instead of …"): the leading name is redundant with the
    // row title directly above it.
    expect(row).not.toHaveTextContent('reviewer runs instead of')
  })
})
