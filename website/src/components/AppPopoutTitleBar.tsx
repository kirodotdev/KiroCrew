import { Undo2 } from 'lucide-react'
import Clickable from './Clickable'
import { useAppPopouts } from '../hooks/useAppPopouts'
import { i18nT } from '../i18n/t'

/**
 * The title bar of a popped-out app window: the app's name, and Return, which
 * hands the app back to the main dashboard.
 *
 * A real 32px row above the app's `<main>`, not an overlay: the app below it
 * lays out in the space that is left, so nothing the app draws in its own
 * top-trailing corner sits under Return.
 *
 * `focus-caption-reserve` on the trailing group: in focus mode on Windows and
 * frameless Linux the window's caption controls paint in this corner, and the
 * class indents Return past them, as the chat title bar's trailing group does.
 */
export default function AppPopoutTitleBar({ label }: {
  /** The app's display name, already resolved (the frame falls back to a generic one). */
  label: string
}) {
  const { returnSelfToMain } = useAppPopouts()
  return (
    <div
      className="h-8 shrink-0 flex items-center gap-2 pl-3 pr-1.5 bg-bg border-b border-border"
      data-testid="app-popout-title-bar"
    >
      <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-text-strong" title={label}>
        {label}
      </span>
      <div className="ml-auto flex shrink-0 items-center focus-caption-reserve">
        <Clickable
          className="flex items-center gap-1 text-muted hover:text-text transition-colors cursor-pointer text-[11px] font-medium px-1.5 py-0.5 rounded hover:bg-bg-hover"
          onClick={returnSelfToMain}
          title={i18nT('components.appPopoutControl.return_this_app_to_the_main_window')}
          aria-label={i18nT('components.appPopoutControl.return_to_main_window')}
        >
          <Undo2 size={13} aria-hidden="true" /> {i18nT('components.appPopoutControl.return')}
        </Clickable>
      </div>
    </div>
  )
}
