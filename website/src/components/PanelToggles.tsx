import { SquareTerminal } from 'lucide-react'
import { Btn } from './ui'
import { SidePanelGlyph } from './SidePanelGlyph'
import { useAppSelector } from '../store'
import { selectActiveSlotProject } from '../store/chatSlice'
import { toggleBottomTerminal, useBottomTerminalOpen } from '../hooks/useBottomTerminal'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import { focusPopout, useTerminalPoppedOut } from '../utils/terminalPopout'
import { activateTerminalEntry, isTerminalShown } from '../lib/terminalEntry'
import { i18nT } from '../i18n/t'

/** One 28px title-row action cell. Shared so the three panel headers cannot drift. */
export const PANEL_HEADER_ACTION_CLS = 'pi-morph flex items-center justify-center w-7 h-7 p-0 rounded-md border-none shrink-0 transition-colors bg-transparent pointer-events-auto'

/** The spacing shared by title-row action groups. */
export const PANEL_HEADER_ACTIONS_CLS = 'flex items-center gap-1.5 shrink-0'

/** Bottom-panel then side-panel toggles, in the order used at the workspace edge. */
export default function PanelToggles({
  workspaceOpen,
  exitFullscreen,
}: {
  workspaceOpen: boolean
  /** Passed only while workspace fullscreen is on, which covers the docked terminal. */
  exitFullscreen?: () => void
}) {
  const terminalEnabled = useTerminalEnabled()
  const terminalOpen = useBottomTerminalOpen()
  const terminalPoppedOut = useTerminalPoppedOut()
  const workspaceActive = workspaceOpen
  const cwd = useAppSelector(selectActiveSlotProject)
  const workspaceFullscreen = exitFullscreen !== undefined
  const terminalShown = isTerminalShown(terminalOpen, workspaceFullscreen)
  const terminalActive = terminalShown || terminalPoppedOut
  // The side toggle draws SidePanelGlyph, so its pane follows where the panel
  // docks, as every other side-panel control does. The terminal toggle draws the
  // nav rail Terminal row's glyph: a bottom-pane glyph would match the side toggle
  // whenever the side panel docks bottom.
  const stateClass = (active: boolean) => active
    ? 'text-accent bg-accent/10'
    : 'text-muted hover:text-text hover:bg-bg-hover'
  // Each control is named by what its click does, for the tooltip and the
  // accessible name alike, and the name follows the panel's state ("Show / Hide
  // side panel"), as the Sessions toggle in the same row does. A name that
  // already states the action carries no aria-pressed, which would announce the
  // state a second time. A popped-out terminal's click focuses that window and
  // toggles nothing here, so its name says so, matching the "Popped out" chip.
  // An open terminal covered by workspace fullscreen reads as hidden: its click
  // leaves fullscreen and brings that terminal back.
  const terminalLabel = terminalPoppedOut
    ? i18nT('pages.chatPage.focus_popped_out_window')
    : terminalShown
      ? i18nT('components.panelToggles.hide_terminal')
      : i18nT('components.panelToggles.show_terminal')
  const workspaceLabel = workspaceActive
    ? i18nT('components.panelToggles.hide_side_panel')
    : i18nT('components.panelToggles.show_side_panel')

  const toggleTerminal = () => activateTerminalEntry({
    open: terminalOpen,
    workspaceFullscreen,
    poppedOut: terminalPoppedOut,
    exitFullscreen,
    focusPopout,
    toggle: () => toggleBottomTerminal(cwd),
  })

  return (
    <div className={PANEL_HEADER_ACTIONS_CLS} data-panel-toggles>
      {terminalEnabled && (
        <Btn
          className={`${PANEL_HEADER_ACTION_CLS} ${stateClass(terminalActive)}`}
          title={terminalLabel}
          aria-label={terminalLabel}
          onClick={toggleTerminal}
        >
          <SquareTerminal size={14} />
        </Btn>
      )}
      {/* The workspace/side-panel toggle is unconditional: every title row that
          hosts these controls owns that panel's edge, so a row without it would
          leave the panel with no toggle. The terminal toggle stays conditional
          because the docked terminal can be disabled by config. */}
      <Btn
        className={`${PANEL_HEADER_ACTION_CLS} ${stateClass(workspaceActive)}`}
        title={workspaceLabel}
        aria-label={workspaceLabel}
        onClick={() => window.dispatchEvent(new Event('toggle-activity-panel'))}
      >
        <SidePanelGlyph light={workspaceActive} size={14} />
      </Btn>
    </div>
  )
}
