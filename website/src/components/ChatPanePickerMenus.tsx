import { createPortal } from 'react-dom'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import AgentDropdownList, { DefaultAgentRow, ManageAgentsFooter, defaultWriteKind, isCrewmateChat } from './AgentDropdownList'
import ModelDropdownList from './ModelDropdownList'
import ReasoningEffortDropdown from './ReasoningEffortDropdown'
import { ManageModelsFooter, MORE_BELOW_MASK } from './ModelEffortDropdown'
import { routeModelPickerKeys } from './modelPickerKeyRouting'
import { settingsPath } from './settingsPath'
import { legacyCodexEffort } from '../hooks/useInteractiveModels'
import { jevRouteShownModel } from '../lib/jevRoute'
import { i18nT } from '../i18n/t'
import { sessionMemoryTag } from '../lib/agentMemorySource'
import type { NavigateFunction } from 'react-router-dom'
import type { RootState } from '../store'
import type { useChatPanePickerActions, useChatPanePickerModels } from './useChatPanePickers'

/**
 * `ChatPane`'s two portaled picker menus, each anchored to the composer chip
 * that opened it: the agent list (with the default-agent row and the manage
 * footer) and the model list with the reasoning-effort control inside it. Both
 * read `useChatPanePickerModels` / `useChatPanePickerActions`; the pane renders
 * them after its composer dock.
 */

// One instance covers both dropdown filter inputs (never open at once).
const ddInputCls = 'w-full px-2 py-1 text-[13px] font-body bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent'

type MenuProps = {
  slotKey: string
  paneSlot: RootState['dashboard']['slots'][number] | undefined
  models: ReturnType<typeof useChatPanePickerModels>
  actions: ReturnType<typeof useChatPanePickerActions>
  navigate: NavigateFunction
}

export function ChatPaneAgentMenu({ paneSlot, models, actions, navigate }: Omit<MenuProps, 'slotKey'>) {
  const { agentDD, agentBtnRect, paneAgentName, defaultAgent, installedAgents, agentChoices, toggleDefaultAgent, defaultAgentFailed } = models
  const { onAgentListKeyDown, switchAgent } = actions
  return (
    <>
      {/* Agent picker portal — anchored to the input-bar agent button. */}
      {agentDD.open && agentBtnRect && createPortal(
        /* The labeled dialog owns roving-focus key handling for its descendants. */
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          ref={agentDD.dropdownRef}
          role="dialog"
          aria-label={i18nT('components.chatPane.agent_list')}
          tabIndex={-1}
          onKeyDown={onAgentListKeyDown}
          className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[260px] max-w-[340px] flex flex-col p-1 gap-0.5 animate-slide-up"
          style={(() => { const left = Math.max(8, Math.min(agentBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - agentBtnRect.top + 4, left } })()}
        >
          <div className="px-1.5 pt-1.5 pb-1">
            <input
              ref={agentDD.inputRef}
              type="text"
              aria-label={i18nT('components.chatPane.type_to_filter')}
              placeholder={i18nT('components.chatPane.type_to_filter')}
              value={agentDD.filter}
              onChange={(e) => agentDD.setFilter(e.target.value)}
              /* Enter/Escape live on the portal container's onListKeyDown
                 (useListboxKeyboard), which claims Enter against IME
                 composition internally — a second handler here would give
                 the same keys two dispatch paths. */
              className={ddInputCls}
            />
          </div>
          <div role="listbox" aria-label={i18nT('components.chatPane.agent_list')} className="overflow-y-auto max-h-[280px]">
            <AgentDropdownList agents={agentDD.filtered} activeAgent={paneAgentName} activeKind={paneSlot?.agent_kind} defaultAgent={defaultAgent} onSelect={(name, kind) => { switchAgent(name, kind); agentDD.setOpen(false) }} />
          </div>
          {/* The default for new sessions is a template; a crewmate's chat offers no such write. */}
          {!isCrewmateChat(paneSlot?.agent_kind, paneAgentName, installedAgents) && <DefaultAgentRow agentName={paneAgentName} isDefault={paneAgentName === defaultAgent} memoryTag={sessionMemoryTag(paneSlot?.agent, paneSlot?.agent_kind, defaultAgent, agentChoices)} onSetDefault={() => toggleDefaultAgent(paneAgentName, defaultWriteKind(paneSlot?.agent_kind))} />}
          <ManageAgentsFooter error={defaultAgentFailed} onManage={() => { agentDD.setOpen(false); navigate('/capabilities?tab=crews') }} />
        </div>,
        document.body,
      )}
    </>
  )
}

export function ChatPaneModelMenu({ slotKey, paneSlot, models, actions, navigate }: MenuProps) {
  const { modelDD, modelBtnRect, hiddenModelsQ, attachModelList, modelListEdges, shownModel, modelPickerConfigured, effortSupported, codexPairModels, defaultEffort, effortLevelsOverride } = models
  const { modelDialogIme, onModelListKeyDown, switchModel } = actions
  return (
    <>
      {/* Model picker portal — anchored to the input-bar model button. */}
      {modelDD.open && modelBtnRect && createPortal(
        /* The labeled dialog owns roving-focus key handling for its descendants. */
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          ref={modelDD.dropdownRef}
          role="dialog"
          aria-label={i18nT('components.chatPane.model_list')}
          tabIndex={-1}
          onKeyDown={event => routeModelPickerKeys(event, modelDialogIme.claimKey, onModelListKeyDown)}
          className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl min-w-[252px] max-w-[348px] flex flex-col p-1 overflow-hidden animate-slide-up"
          /* Capped to the space above the chip, like ModelEffortDropdown: the
             effort block below the list adds height, and a short split pane
             would otherwise let the dialog grow past the viewport top. The
             model list shrinks first, floored at about two rows so a chip
             that sits high in a short pane never opens a picker with no
             model in it; once the cap drops below the fixed rows' own height
             the body column scrolls instead of clipping the effort block. */
          style={(() => { const left = Math.max(8, Math.min(modelBtnRect.left, window.innerWidth - 348)); return { bottom: window.innerHeight - modelBtnRect.top + 4, left, maxHeight: Math.max(0, modelBtnRect.top - 12) } })()}
        >
         <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
          <div className="shrink-0 px-1.5 pt-1.5 pb-1">
            <input
              ref={modelDD.inputRef}
              type="text"
              aria-label={i18nT('components.chatPane.type_to_filter')}
              placeholder={i18nT('components.chatPane.type_to_filter')}
              value={modelDD.filter}
              /* Tracks composition for the dialog's Tab claim (see
                 routeModelPickerKeys): the latch has to see the composition
                 to know a Tab inside the post-composition window is the
                 IME's, not the user's. */
              {...modelDialogIme.bindComposition<HTMLInputElement>()}
              onChange={(e) => modelDD.setFilter(e.target.value)}
              /* Enter/Escape live on the portal container's onListKeyDown
                 (useListboxKeyboard), which claims Enter against IME
                 composition internally — a second handler here would give
                 the same keys two dispatch paths. */
              className={ddInputCls}
            />
          </div>
          {hiddenModelsQ.isError && (
            <div className="flex shrink-0 items-center gap-2 px-1.5 py-1">
              {/* No hand-off: this pane's composer may hold an unsent draft.
                  Retry keeps the user in the owning chat. */}
              <ErrorNotice
                className="min-w-0 flex-1"
                variant="inline"
                message={i18nT('pages.settings.chatPanel.failed_to_load_dashboard_config')}
              />
              <Btn type="button" className="shrink-0" onClick={() => hiddenModelsQ.refetch()}>
                {i18nT('pages.settings.chatPanel.retry')}
              </Btn>
            </div>
          )}
          {/* Same bottom fade as ModelEffortDropdown's list (see MORE_BELOW_MASK). */}
          <div ref={attachModelList} role="listbox" aria-label={i18nT('components.chatPane.model_list')} className={`min-h-[96px] flex-1 overflow-y-auto max-h-[280px] ${modelListEdges.bottom ? MORE_BELOW_MASK : ''}`}>
            <ModelDropdownList models={modelDD.filtered} activeModel={jevRouteShownModel(shownModel, paneSlot)} onSelect={(name) => { switchModel(name); modelDD.setOpen(false) }} />
          </div>
          {!modelPickerConfigured && <ManageModelsFooter onManage={() => {
            modelDD.setOpen(false)
            navigate(settingsPath({ tab: 'chat', sub: 'models', highlight: 'key:dashboard.model_picker_hidden_models' }))
          }} />}
          {/* Reasoning effort lives INSIDE the model picker (one control),
              fed by the slot's ACP capability read: the agent's own answer
              decides whether the row exists and which levels it offers. */}
          {effortSupported && (
            <div className="mt-2 shrink-0 border-t border-border">
              <ReasoningEffortDropdown
                slot={slotKey}
                currentEffort={paneSlot?.reasoning_effort || legacyCodexEffort(paneSlot?.model || '', '', codexPairModels)}
                defaultEffort={defaultEffort}
                levelsOverride={effortLevelsOverride}
                onClose={() => modelDD.setOpen(false)}
                embedded
              />
            </div>
          )}
         </div>
        </div>,
        document.body,
      )}
    </>
  )
}
