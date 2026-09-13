import { Btn } from './ui'
import { PanelBottomSolid, PanelRightSolid } from './icons/panels'
import { useAppSelector } from '../store'
import { selectActiveSlotProject } from '../store/chatSlice'
import { toggleBottomTerminal, useBottomTerminalOpen } from '../hooks/useBottomTerminal'
import { useTerminalEnabled } from '../utils/terminalRegistry'
import { focusPopout, useTerminalPoppedOut } from '../utils/terminalPopout'
import { i18nT } from '../i18n/t'

/** Shell-owned controls keep their identity and position as either panel opens.
 *
 *  The group is the workspace toggle and the terminal toggle: two actions, one
 *  row. Fullscreen is the workspace panel's own action and renders in that
 *  panel's action group (see SidePanel), which also keeps this row under the
 *  two-button cap. `exitFullscreen` is set while the workspace is fullscreen so
 *  the terminal action leaves fullscreen before it opens or focuses a terminal.
 *
 *  The glyphs are the house panel icons (`icons/panels`), the same family the
 *  split-pane controls draw through SplitGlyph, so one title row never mixes
 *  two renderings of "a panel". `open` follows the pressed state (thin solid
 *  pane = closed, thick dimmed pane = open) and `pi-morph` on the button
 *  previews the click's outcome on hover. */
export default function PanelToggles({ showWorkspace, workspaceOpen, exitFullscreen }: { showWorkspace: boolean; workspaceOpen?: boolean; exitFullscreen?: () => void }) {
  const terminalEnabled = useTerminalEnabled()
  // Terminal state is scoped to the active chat session (see
  // useBottomTerminal); the toggle must read and drive the same scope the
  // bottom panel renders, not the global one.
  const terminalSessionScope = useAppSelector(s => s.chat.activeSlot)
  const terminalOpen = useBottomTerminalOpen(terminalSessionScope)
  const terminalPoppedOut = useTerminalPoppedOut(terminalSessionScope)
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  const workspaceActive = workspaceOpen ?? activityOpen
  const cwd = useAppSelector(selectActiveSlotProject)
  const terminalActive = terminalOpen || terminalPoppedOut
  // Width and height both follow the toolbar token: the shared CSS only pins
  // height and min-width, and a fixed `w-7` stays 28px where the session grid
  // shrinks the token to 24px, overrunning the space its hosts reserve.
  const buttonClass = (active: boolean) => `pi-morph flex items-center justify-center w-[var(--panel-toolbar-button-size)] h-[var(--panel-toolbar-button-size)] p-0 border-none shrink-0 ${active ? 'text-accent bg-accent/10' : 'text-muted hover:text-text'}`
  const terminalLabel = i18nT('hooks.useKeyboardShortcuts.toggle_terminal')
  const workspaceLabel = workspaceActive ? i18nT('pages.chat.sidePanel.close_panel') : i18nT('pages.chatPage.open_activity_panel')
  const toggleTerminal = () => {
    exitFullscreen?.()
    if (terminalPoppedOut) focusPopout(terminalSessionScope); else toggleBottomTerminal(cwd, terminalSessionScope)
  }
  const toggleWorkspace = () => window.dispatchEvent(new Event('toggle-activity-panel'))
  return (
    <div className="panel-toolbar-actions flex items-center shrink-0" data-panel-toggles>
      {showWorkspace && (
        <Btn className={buttonClass(workspaceActive)}
          title={workspaceLabel}
          aria-label={i18nT('hooks.useKeyboardShortcuts.toggle_side_panel')}
          aria-pressed={workspaceActive}
          onClick={toggleWorkspace}>
          <PanelRightSolid size={16} open={workspaceActive} />
        </Btn>
      )}
      {terminalEnabled && (
        <Btn className={buttonClass(terminalActive)}
          title={terminalLabel}
          aria-label={terminalLabel}
          aria-pressed={terminalActive}
          onClick={toggleTerminal}>
          <PanelBottomSolid size={16} open={terminalActive} />
        </Btn>
      )}
    </div>
  )
}
