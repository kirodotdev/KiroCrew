import { Btn } from './ui'
import { PanelBottomLight, PanelBottomSolid, PanelRightLight, PanelRightSolid } from './icons/panels'
import { useAppSelector } from '../store'
import { selectActiveSlotProject } from '../store/chatSlice'
import { toggleBottomTerminal, useBottomTerminalOpen } from '../hooks/useBottomTerminal'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import { focusPopout, useTerminalPoppedOut } from '../utils/terminalPopout'
import { i18nT } from '../i18n/t'

/** One 28px title-row action cell. Shared so the three panel headers cannot drift. */
export const PANEL_HEADER_ACTION_CLS = 'pi-morph flex items-center justify-center w-7 h-7 p-0 rounded-md border-none shrink-0 transition-colors bg-transparent pointer-events-auto'

/** The spacing shared by title-row action groups. */
export const PANEL_HEADER_ACTIONS_CLS = 'flex items-center gap-1.5 shrink-0'

/** Bottom-panel then side-panel toggles, in the order used at the workspace edge. */
export default function PanelToggles({
  showWorkspace,
  workspaceOpen,
  exitFullscreen,
}: {
  showWorkspace: boolean
  workspaceOpen?: boolean
  exitFullscreen?: () => void
}) {
  const terminalEnabled = useTerminalEnabled()
  const terminalOpen = useBottomTerminalOpen()
  const terminalPoppedOut = useTerminalPoppedOut()
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  const workspaceActive = workspaceOpen ?? activityOpen
  const cwd = useAppSelector(selectActiveSlotProject)
  const terminalActive = terminalOpen || terminalPoppedOut
  const TerminalIcon = terminalActive ? PanelBottomLight : PanelBottomSolid
  const WorkspaceIcon = workspaceActive ? PanelRightLight : PanelRightSolid
  const stateClass = (active: boolean) => active
    ? 'text-accent bg-accent/10'
    : 'text-muted hover:text-text hover:bg-bg-hover'
  // One name per control, for the tooltip and the accessible name alike: a
  // toggle that carries aria-pressed keeps a stable name and reports its state
  // through that attribute, instead of renaming itself per state. The one
  // exception is a popped-out terminal: the click then focuses the other
  // window and toggles nothing here, so the name says what the click does,
  // matching the "Popped out" chip beside it.
  const terminalLabel = terminalPoppedOut
    ? i18nT('pages.chatPage.focus_popped_out_window')
    : i18nT('hooks.useKeyboardShortcuts.toggle_terminal')
  const workspaceLabel = i18nT('hooks.useKeyboardShortcuts.toggle_side_panel')

  const toggleTerminal = () => {
    exitFullscreen?.()
    if (terminalPoppedOut) focusPopout()
    else toggleBottomTerminal(cwd)
  }

  return (
    <div className={PANEL_HEADER_ACTIONS_CLS} data-panel-toggles>
      {terminalEnabled && (
        <Btn
          className={`${PANEL_HEADER_ACTION_CLS} ${stateClass(terminalActive)}`}
          title={terminalLabel}
          aria-label={terminalLabel}
          aria-pressed={terminalActive}
          onClick={toggleTerminal}
        >
          <TerminalIcon size={14} />
        </Btn>
      )}
      {showWorkspace && (
        <Btn
          className={`${PANEL_HEADER_ACTION_CLS} ${stateClass(workspaceActive)}`}
          title={workspaceLabel}
          aria-label={workspaceLabel}
          aria-pressed={workspaceActive}
          onClick={() => window.dispatchEvent(new Event('toggle-activity-panel'))}
        >
          <WorkspaceIcon size={14} />
        </Btn>
      )}
    </div>
  )
}
