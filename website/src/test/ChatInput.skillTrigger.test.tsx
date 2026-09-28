import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'

/* ── $skill trigger in ChatInput. Mock api so the mounted
 *    SkillPickerMenu's lazy api.skills() fetch is deterministic. ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skillTrust: vi.fn(),
  grantSkillTrust: vi.fn(),
}))
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
  mockApi.skillTrust.mockResolvedValue({
    project: '/work/project-a',
    project_key: '/work/project-a',
  })
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
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

  it('does not insert a trusted token after the active chat changes', async () => {
    let resolveGrant!: (value: { trusted: boolean }) => void
    mockApi.skills.mockResolvedValue([
      {
        key: 'kiro-workspace/oncall-handover',
        name: 'oncall-handover',
        description: 'Handover',
        source: 'kiro-workspace',
        trusted: false,
      },
    ])
    mockApi.grantSkillTrust.mockReturnValue(
      new Promise(resolve => { resolveGrant = resolve }),
    )
    const onChange = vi.fn()
    const store = createTestStore()
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-a' })

    function Host() {
      const [val, setVal] = useState('')
      return (
        <ChatInput
          value={val}
          onChange={(next) => { onChange(next); setVal(next) }}
          onSend={vi.fn()}
        />
      )
    }

    renderWithProviders(<Host />, { store })
    typeInto('run $hand')
    fireEvent.mouseDown(await screen.findByText('$oncall-handover'))
    const trust = await screen.findByRole('button', { name: /Trust this folder/ })
    await waitFor(() => expect(trust).not.toBeDisabled())
    fireEvent.click(trust)
    await waitFor(() =>
      expect(mockApi.grantSkillTrust).toHaveBeenCalledWith(
        'dashboard:chat-a',
        '/work/project-a',
      ),
    )

    act(() => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-b' }) })
    onChange.mockClear()
    await act(async () => { resolveGrant({ trusted: true }) })

    await waitFor(() => expect(onChange).not.toHaveBeenCalled())
  })

  it('does not insert a trusted token after the same chat changes projects', async () => {
    let resolveGrant!: (value: { trusted: boolean }) => void
    mockApi.skills.mockResolvedValue([
      {
        key: 'kiro-workspace/oncall-handover',
        name: 'oncall-handover',
        description: 'Handover',
        source: 'kiro-workspace',
        trusted: false,
      },
    ])
    mockApi.grantSkillTrust.mockReturnValue(
      new Promise(resolve => { resolveGrant = resolve }),
    )
    const onChange = vi.fn()
    const store = createTestStore()
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-a' })

    function Host() {
      const [val, setVal] = useState('')
      const [project, setProject] = useState('/work/project-a')
      return (
        <>
          <button onClick={() => setProject('/work/project-b')}>Switch project</button>
          <ChatInput
            value={val}
            project={project}
            onChange={(next) => { onChange(next); setVal(next) }}
            onSend={vi.fn()}
          />
        </>
      )
    }

    renderWithProviders(<Host />, { store })
    typeInto('run $hand')
    fireEvent.mouseDown(await screen.findByText('$oncall-handover'))
    const trust = await screen.findByRole('button', { name: /Trust this folder/ })
    await waitFor(() => expect(trust).not.toBeDisabled())
    fireEvent.click(trust)
    await waitFor(() => expect(mockApi.grantSkillTrust).toHaveBeenCalled())

    fireEvent.click(screen.getByRole('button', { name: 'Switch project' }))
    onChange.mockClear()
    await act(async () => { resolveGrant({ trusted: true }) })

    await waitFor(() => expect(onChange).not.toHaveBeenCalled())
  })

  it('does not let an older trust request clear or insert into a newer one', async () => {
    let resolveFirstGrant!: (value: { trusted: boolean }) => void
    mockApi.skills.mockResolvedValue([
      {
        key: 'kiro-workspace/oncall-handover',
        name: 'oncall-handover',
        description: 'Handover',
        source: 'kiro-workspace',
        trusted: false,
      },
      {
        key: 'kiro-workspace/incident-summary',
        name: 'incident-summary',
        description: 'Incident',
        source: 'kiro-workspace',
        trusted: false,
      },
    ])
    mockApi.grantSkillTrust
      .mockReturnValueOnce(new Promise(resolve => { resolveFirstGrant = resolve }))
      .mockResolvedValueOnce({ trusted: true })
    const onChange = vi.fn()
    const store = createTestStore()
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'chat-a' })

    function Host() {
      const [val, setVal] = useState('')
      return (
        <ChatInput
          value={val}
          project="/work/project-a"
          onChange={(next) => { onChange(next); setVal(next) }}
          onSend={vi.fn()}
        />
      )
    }

    renderWithProviders(<Host />, { store })
    typeInto('run $hand')
    fireEvent.mouseDown(await screen.findByText('$oncall-handover'))
    let trust = await screen.findByRole('button', { name: /Trust this folder/ })
    await waitFor(() => expect(trust).not.toBeDisabled())
    fireEvent.click(trust)
    await waitFor(() => expect(mockApi.grantSkillTrust).toHaveBeenCalledTimes(1))

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    typeInto('run $incident')
    fireEvent.mouseDown(await screen.findByText('$incident-summary'))
    expect(await screen.findByRole('dialog')).toHaveTextContent('incident-summary')

    onChange.mockClear()
    await act(async () => { resolveFirstGrant({ trusted: true }) })
    await waitFor(() => expect(onChange).not.toHaveBeenCalled())

    trust = screen.getByRole('button', { name: /Trust this folder/ })
    await waitFor(() => expect(trust).not.toBeDisabled())
    fireEvent.click(trust)

    await waitFor(() => expect(mockApi.grantSkillTrust).toHaveBeenCalledTimes(2))
    expect(onChange).toHaveBeenLastCalledWith('run $incident-summary ')
  })
})
