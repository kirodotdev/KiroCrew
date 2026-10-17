import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createRef } from 'react'
import { ChatPaneAgentMenu, ChatPaneModelMenu } from './ChatPanePickerMenus'
import type { useChatPanePickerActions, useChatPanePickerModels } from './useChatPanePickers'
import { settingsPath } from './settingsPath'
import { i18nT } from '../i18n/t'

/* ── ChatPane's two portaled picker menus, driven through their own props: what
 *    each control in the agent and the model menu does with the picker state
 *    and actions `useChatPanePickers` hands them. The effort control is a stub
 *    here; its own spec covers it. ── */

vi.mock('./ReasoningEffortDropdown', () => ({
  default: ({ onClose }: { onClose: () => void }) => <button type="button" onClick={onClose}>effort-stub</button>,
}))

type Models = ReturnType<typeof useChatPanePickerModels>
type Actions = ReturnType<typeof useChatPanePickerActions>

function dropdown(items: { name: string }[]) {
  return {
    open: true,
    setOpen: vi.fn(),
    filter: '',
    setFilter: vi.fn(),
    filtered: items,
    dropdownRef: createRef<HTMLDivElement>(),
    inputRef: createRef<HTMLInputElement>(),
  }
}

function setup(overrides: Partial<Record<string, unknown>> = {}) {
  const agentDD = dropdown([{ name: 'alpha', source: 'builtin' }, { name: 'beta', source: 'user' }] as never)
  const modelDD = dropdown([{ name: 'model-one' }, { name: 'model-two' }])
  const models = {
    agentDD,
    agentBtnRect: { left: 12, top: 400 } as DOMRect,
    paneAgentName: 'alpha',
    defaultAgent: 'beta',
    installedAgents: [],
    agentChoices: [],
    toggleDefaultAgent: vi.fn(),
    defaultAgentFailed: false,
    modelDD,
    modelBtnRect: { left: 12, top: 400 } as DOMRect,
    hiddenModelsQ: { isError: true, refetch: vi.fn() },
    attachModelList: vi.fn(),
    modelListEdges: { bottom: false },
    shownModel: 'model-one',
    modelPickerConfigured: false,
    effortSupported: true,
    codexPairModels: false,
    defaultEffort: '',
    effortLevelsOverride: undefined,
    ...overrides,
  } as unknown as Models
  const actions = {
    switchAgent: vi.fn(),
    switchModel: vi.fn(),
    onAgentListKeyDown: vi.fn(),
    onModelListKeyDown: vi.fn(),
    modelDialogIme: { claimKey: vi.fn(() => true), bindComposition: () => ({}) },
  } as unknown as Actions
  const navigate = vi.fn()
  render(
    <>
      <ChatPaneAgentMenu paneSlot={undefined} models={models} actions={actions} navigate={navigate} />
      <ChatPaneModelMenu slotKey="slot-1" paneSlot={undefined} models={models} actions={actions} navigate={navigate} />
    </>,
  )
  const [agentMenu, modelMenu] = screen.getAllByRole('dialog')
  return { models, actions, navigate, agentDD, modelDD, agentMenu, modelMenu }
}

afterEach(cleanup)

describe('ChatPaneAgentMenu', () => {
  it('filters, picks an agent and closes', () => {
    const { actions, agentDD, agentMenu } = setup()
    fireEvent.change(within(agentMenu).getByLabelText(i18nT('components.chatPane.type_to_filter')), { target: { value: 'be' } })
    expect(agentDD.setFilter).toHaveBeenCalledWith('be')
    fireEvent.click(within(agentMenu).getAllByRole('option')[1])
    expect(actions.switchAgent).toHaveBeenCalledWith('beta', undefined)
    expect(agentDD.setOpen).toHaveBeenCalledWith(false)
  })

  it('writes the default agent from its row and opens the manage page', () => {
    const { models, navigate, agentDD, agentMenu } = setup()
    const row = agentMenu.querySelector<HTMLButtonElement>('button[data-option]:not([role="option"])')
    expect(row).not.toBeNull()
    fireEvent.click(row!)
    expect(vi.mocked(models.toggleDefaultAgent).mock.calls[0][0]).toBe('alpha')
    fireEvent.click(within(agentMenu).getByText(i18nT('components.agentDropdownList.manage_agents')))
    expect(agentDD.setOpen).toHaveBeenCalledWith(false)
    expect(navigate).toHaveBeenCalledWith('/capabilities?tab=crews')
  })

  it('routes keys to the listbox handler', () => {
    const { actions, agentMenu } = setup()
    fireEvent.keyDown(agentMenu, { key: 'ArrowDown' })
    expect(actions.onAgentListKeyDown).toHaveBeenCalledTimes(1)
  })
})

describe('ChatPaneModelMenu', () => {
  it('filters, picks a model and closes', () => {
    const { actions, modelDD, modelMenu } = setup()
    fireEvent.change(within(modelMenu).getByLabelText(i18nT('components.chatPane.type_to_filter')), { target: { value: 'two' } })
    expect(modelDD.setFilter).toHaveBeenCalledWith('two')
    fireEvent.click(within(modelMenu).getAllByRole('option')[1])
    expect(actions.switchModel).toHaveBeenCalledWith('model-two')
    expect(modelDD.setOpen).toHaveBeenCalledWith(false)
  })

  it('retries a failed hidden-models read', () => {
    const { models, modelMenu } = setup()
    fireEvent.click(within(modelMenu).getByText(i18nT('pages.settings.chatPanel.retry')))
    expect(models.hiddenModelsQ.refetch).toHaveBeenCalledTimes(1)
  })

  it('opens the visible-models setting and closes', () => {
    const { navigate, modelDD, modelMenu } = setup()
    fireEvent.click(within(modelMenu).getByLabelText(i18nT('components.modelEffortDropdown.manage_visible_models')))
    expect(modelDD.setOpen).toHaveBeenCalledWith(false)
    expect(navigate)
      .toHaveBeenCalledWith(settingsPath({ tab: 'chat', sub: 'models', highlight: 'key:dashboard.model_picker_hidden_models' }))
  })

  it('closes from the embedded effort control and hands Escape to the listbox', () => {
    const { actions, modelDD, modelMenu } = setup()
    fireEvent.click(within(modelMenu).getByText('effort-stub'))
    expect(modelDD.setOpen).toHaveBeenCalledWith(false)
    fireEvent.keyDown(within(modelMenu).getByLabelText(i18nT('components.chatPane.type_to_filter')), { key: 'Escape' })
    expect(actions.onModelListKeyDown).toHaveBeenCalledTimes(1)
  })

  it('draws nothing while closed', () => {
    const closed = { ...dropdown([]), open: false }
    render(
      <ChatPaneModelMenu slotKey="s" paneSlot={undefined} actions={{} as Actions} navigate={vi.fn()}
        models={{ modelDD: closed, modelBtnRect: null } as unknown as Models} />,
    )
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
