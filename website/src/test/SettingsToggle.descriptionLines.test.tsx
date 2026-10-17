import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SettingsToggle } from '../components/settings'
import en from '../i18n/locales/en.json'

// A toggle's description carries the consequences a reader must weigh before
// flipping it. When there is more than one, each goes on its own line: a
// newline in the catalog string, preserved by the row, so the consequence that
// matters most is not buried at the end of a grey paragraph.
describe('SettingsToggle description lines', () => {
  it('keeps a line break in the description so each consequence reads on its own line', () => {
    render(
      <SettingsToggle
        label="Example setting"
        description={'Costs something.\nRuns tools without asking after an error.'}
        checked={false}
        onChange={() => {}}
      />
    )

    const description = screen.getByText(/Costs something/)
    expect(description).toHaveClass('whitespace-pre-line')
    expect(description.textContent).toBe('Costs something.\nRuns tools without asking after an error.')
  })

  it('the automatic-resume switch states its two consequences as two lines, cost first', () => {
    const chatPanel = (en as { pages: { settings: { chatPanel: Record<string, string> } } }).pages
      .settings.chatPanel
    const lines = chatPanel.resume_interrupted_turns_description.split('\n')

    expect(lines).toHaveLength(2)
    expect(lines[0]).toMatch(/^Uses model turns and tool calls/)
    expect(lines[1]).toMatch(/^After a restart,.*after an error, the chat keeps its Trust/)
    // The hint no longer repeats the restart consequence the description states.
    expect(chatPanel.resume_interrupted_turns_hint).not.toMatch(/ends at a restart/)
  })
})
