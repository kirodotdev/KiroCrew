import { useLocation } from 'react-router-dom'
import { AppWindow, ExternalLink } from 'lucide-react'
import { useLabelTip } from '../../components/InstantTip'
import { useAppPopouts } from '../../hooks/useAppPopouts'
import { hasBuiltinComponent } from '../../apps/builtinRegistry'
import { appRouteOf } from '../../utils/appPopoutRoute'
import { i18nT } from '../../i18n/t'

/** The press events dnd-kit's mouse, touch and keyboard sensors start a drag from. */
const stop = (e: React.SyntheticEvent) => e.stopPropagation()

/**
 * The pop-out entry point on an Apps-group rail row, in two states:
 *
 *  - the app is in this window: a muted icon that shows while the row is
 *    hovered or the button has keyboard focus, and opens the app in its own
 *    window. On the active row the window opens at the page and query on
 *    screen; on any other row it opens at the app's route. A touch screen has
 *    no hover (`hover:` is gated on `(hover: hover)`), so there the icon is
 *    always shown, at a 32px target.
 *  - the app is popped out: an accent mark that is always shown, and focuses
 *    that window. The row itself still navigates this window to the app.
 *    It uses its own glyph (a window, not the pop-out arrow), so the state is
 *    told apart by shape as well as by colour.
 *
 * The rail renders this as a SIBLING of the row's `role="button"` element,
 * absolutely placed at the trailing end of the `relative group/approw`
 * container that holds both: a button inside a button is one control to
 * assistive tech and activates the outer one.
 *
 * The sortable wrapper around the row carries dnd-kit's listeners, so the
 * press events here stop at the button: pressing it is never a pick-up, and
 * Enter or Space on it never starts a keyboard drag.
 *
 * Renders nothing for a route the pop-out window would refuse as no app, so
 * the rail never offers a window that opens on a not-found notice.
 */
export function AppRowPopoutAction({ route, active }: {
  /** The row's app route (`/dev-fleet`, `/apps/foo`): the pop-out's identity. */
  route: string
  /** Whether the row is the app on screen, so the window opens where the user is. */
  active: boolean
}) {
  const { pathname, search } = useLocation()
  const { isPoppedOut, open, focus } = useAppPopouts()
  const poppedOut = isPoppedOut(route)
  const popOutLabel = i18nT('components.appPopoutControl.pop_out_app_to_its_own_window')
  const focusTipLabel = i18nT('components.appPopoutControl.this_app_is_open_in_its_own_window_focus_it')
  const tip = useLabelTip(poppedOut ? focusTipLabel : popOutLabel)
  if (appRouteOf(route, hasBuiltinComponent) !== route) return null

  return (
    <button
      type="button"
      data-testid="app-row-popout-action"
      data-popped-out={poppedOut ? '' : undefined}
      className={`absolute right-1.5 top-1/2 -translate-y-1/2 z-10 flex items-center justify-center w-6 h-6 rounded-md border-none cursor-pointer transition-[opacity,color,background-color] duration-150 ${poppedOut
        ? 'opacity-100 text-accent bg-accent/10 hover:bg-accent/20'
        : 'opacity-0 group-hover/approw:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100 [@media(hover:none)]:w-8 [@media(hover:none)]:h-8 [@media(hover:none)]:right-1 bg-transparent text-muted hover:text-text hover:bg-bg-hover'}`}
      aria-label={poppedOut
        ? i18nT('components.appPopoutControl.focus_popped_out_window')
        : popOutLabel}
      {...tip.handlers}
      onPointerDown={e => { e.stopPropagation(); tip.handlers.onPointerDown?.(e) }}
      onMouseDown={stop}
      onTouchStart={stop}
      onKeyDown={stop}
      onClick={e => {
        e.stopPropagation()
        if (poppedOut) focus(route)
        else open(route, active ? `${pathname}${search}` : route)
      }}
    >
      {poppedOut
        ? <AppWindow size={13} aria-hidden="true" data-icon="popped-out" />
        : <ExternalLink size={13} aria-hidden="true" data-icon="pop-out" />}
      {tip.bubble}
    </button>
  )
}
