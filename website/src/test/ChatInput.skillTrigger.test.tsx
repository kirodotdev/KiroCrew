import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/* ── $skill trigger in ChatInput. Mock api so the mounted
 *    SkillPickerMenu's lazy api.skills() fetch is deterministic. ── */
const mockApi = vi.hoisted(() => ({ skills: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import ChatInput from '../components/ChatInput'

const SKILLS = [
  { key: 'WorkforceEmploymentKnowledgeBase/oncall-handover', name: 'oncall-handover', description: 'Handover', source: 'package' },
  { key: 'grill', name: 'grill', description: 'Questioning', source: 'kirocrew' },
]

beforeEach(() => {
  vi.restoreAllMocks()
  // vitest 4's restoreAllMocks no longer clears standalone vi.fn() call history
  // (mockApi.skills), so clear it explicitly or calls leak across tests.
  vi.clearAllMocks()
  localStorage.clear()
  mockApi.skills.mockResolvedValue(SKILLS)
})

function typeInto(value: string) {
  const ta = screen.getByLabelText('Message input')
  fireEvent.change(ta, { target: { value } })
  return ta
}

describe('ChatInput — $skill trigger', () => {
  it('opens the skill picker when typing $ at a word boundary', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    typeInto('hello $hand')
    expect(await screen.findByText('$oncall-handover')).toBeInTheDocument()
  })

  it('does not open the picker for an uppercase env-style token like $PATH', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    typeInto('echo $PATH')
    // no fetch, no listbox
    await waitFor(() => expect(screen.queryByRole('listbox')).not.toBeInTheDocument())
    expect(mockApi.skills).not.toHaveBeenCalled()
  })

  it('inserts the $leaf token on select, leaving it literal', async () => {
    const onChange = vi.fn()
    function Host() {
      const [val, setVal] = useState('')
      return (
        <ChatInput
          value={val}
          onChange={(v) => { onChange(v); setVal(v) }}
          onSend={vi.fn()}
        />
      )
    }
    renderWithProviders(<Host />)
    typeInto('run $hand')
    const opt = await screen.findByText('$oncall-handover')
    fireEvent.mouseDown(opt)
    expect(onChange).toHaveBeenLastCalledWith('run $oncall-handover ')
  })

  it('does not open the skill picker for an @ file mention', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onFileSelect={vi.fn()} />)
    typeInto('see @src')
    await waitFor(() => expect(mockApi.skills).not.toHaveBeenCalled())
  })
})
