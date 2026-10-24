import { ExternalLink, Undo2 } from 'lucide-react'
import Clickable from './Clickable'
import { useLabelTip } from './InstantTip'
import { useAppPopouts } from '../hooks/useAppPopouts'
import { i18nT } from '../i18n/t'

/**
 * The pop-out control an app page carries in its top-trailing corner — the
 * same three states, in the same spot, as the chat title bar's pop-out control
 * (`ChatTitleOverlay`): a dimmed icon to pop the app out, an accent chip while
 * it IS out (click focuses that window), and inside the pop-out window itself
 * a Return control in the same place.
 *
 * Owned by the HOST, not by the apps. An app page renders whatever header it
 * likes, so there is no app-side slot to put this in and no way to add one to
 * a third-party bundle; the host overlays it instead. The overlay is a
 * zero-height sticky row at the top of the page's scroll container: it stays
 * at the corner while the page scrolls, it pushes no content down, and its
 * only hit target is the 28px control itself (`pointer-events-none` on the row,
 * `auto` on the control), so an app header that does put something in that
 * corner loses at most the pixels under the button.
 *
 * `focus-caption-reserve`: in focus mode on Windows and frameless Linux the
 * window's caption controls paint in this same corner; the class indents the
 * control past them, as the chat title bar's trailing group does.
 */
export default function AppPopoutControl({ route, popout = false, initialPath }: {
  /** The app's dashboard route (`/dev-fleet`, `/apps/foo`) — the pop-out's identity. */
  route: string
  /** True inside the pop-out window: the spot carries Return instead. */
  popout?: boolean
  /** App-space path and query the new window opens at (the current page); the app's root when absent. */
  initialPath?: string
}) {
  const { isPoppedOut, open, focus, returnSelfToMain } = useAppPopouts()
  const popOutTip = useLabelTip(i18nT('components.appPopoutControl.pop_out_app_to_its_own_window'))
  // A pop-out never holds its OWN route in the coordination map (the channel
  // does not self-deliver), so the popped-out chip can only ever show in main.
  const poppedOut = !popout && isPoppedOut(route)

  return (
    <div
      className="sticky top-0 z-30 h-0 shrink-0 flex items-start justify-end pointer-events-none focus-caption-reserve"
      data-testid="app-popout-control"
    >
      <div className="mt-1.5 mr-3 pointer-events-auto">
        {popout ? (
          <Clickable
            className="flex items-center gap-1 text-muted hover:text-text transition-colors cursor-pointer text-[11px] font-medium px-1.5 py-0.5 rounded hover:bg-bg-hover"
            onClick={returnSelfToMain}
            title={i18nT('components.appPopoutControl.return_this_app_to_the_main_window')}
            aria-label={i18nT('components.appPopoutControl.return_to_main_window')}
          >
            <Undo2 size={13} /> {i18nT('components.appPopoutControl.return')}
          </Clickable>
        ) : poppedOut ? (
          <Clickable
            className="flex items-center gap-1 text-accent bg-accent/10 hover:bg-accent/20 transition-colors cursor-pointer text-[11px] font-medium px-1.5 py-0.5 rounded"
            onClick={() => focus(route)}
            title={i18nT('components.appPopoutControl.this_app_is_open_in_its_own_window_focus_it')}
            aria-label={i18nT('components.appPopoutControl.focus_popped_out_window')}
          >
            <ExternalLink size={13} /> {i18nT('components.appPopoutControl.popped_out')}
          </Clickable>
        ) : (
          <Clickable
            className="flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 text-muted hover:text-text"
            onClick={() => open(route, initialPath)}
            aria-label={i18nT('components.appPopoutControl.pop_out_app_to_its_own_window')}
            {...popOutTip.handlers}
          >
            <ExternalLink size={15} />
            {popOutTip.bubble}
          </Clickable>
        )}
      </div>
    </div>
  )
}
