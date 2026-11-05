import { Smartphone, SquareTerminal } from 'lucide-react'
import type { ReactNode } from 'react'

import { toggleBottomTerminal } from '../../hooks/useBottomTerminal'
import { focusPopout as focusTerminalPopout } from '../../utils/terminalPopout'
import { i18nT } from '../../i18n/t'
import type { AppShell } from '../appShell'

/** Grid row height of the status bar, in px. App.tsx sizes the shell's last track with it. */
export const STATUS_BAR_HEIGHT = 26

function StatusButton({ label, icon, pressed, onClick }: { label: string; icon: ReactNode; pressed?: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={pressed}
      className={`h-[22px] inline-flex items-center gap-1.5 px-1.5 rounded-md text-[12px] bg-transparent border-none cursor-pointer ${pressed ? 'text-text bg-bg-hover' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
    >
      <span className="w-3.5 h-3.5 flex items-center justify-center">{icon}</span>
      <span>{label}</span>
    </button>
  )
}

/**
 * Desktop status bar along the bottom of the shell: toggles that act on the
 * window rather than navigate (the docked terminal, phone connect). They left
 * the rail so the rail holds only pinnable destinations. Not rendered on the
 * phone or in focus mode.
 */
export function ShellStatusBar({ shell }: { shell: AppShell }) {
  const {
    terminalEnabled, terminalPoppedOut, bottomTerminalOpen, activeSlotProject,
    hasRenderableMobileConnect, mobileConnectOpen, setMobileConnectOpen,
  } = shell
  if (!terminalEnabled && !hasRenderableMobileConnect) return null
  return (
    <div
      role="toolbar"
      aria-label={i18nT('nav.status_bar')}
      className="flex items-center gap-1 px-2 text-muted border-t border-border"
      style={{ gridArea: 'status', height: STATUS_BAR_HEIGHT }}
    >
      {terminalEnabled && (
        <StatusButton
          label={i18nT('app.terminal')}
          icon={<SquareTerminal size={14} />}
          pressed={bottomTerminalOpen || terminalPoppedOut}
          // Popped out: focus it (re-dock lives in the detached bar), same as the old rail row.
          onClick={() => { if (terminalPoppedOut) focusTerminalPopout(); else toggleBottomTerminal(activeSlotProject) }}
        />
      )}
      {hasRenderableMobileConnect && (
        <StatusButton
          label={i18nT('app.connect_your_phone')}
          icon={<Smartphone size={14} />}
          pressed={mobileConnectOpen}
          onClick={() => setMobileConnectOpen(true)}
        />
      )}
    </div>
  )
}
