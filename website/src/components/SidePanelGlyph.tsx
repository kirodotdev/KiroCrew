import { createContext, useContext } from 'react'
import { useSidePanelDock } from '../hooks/useSidePanelDock'
import { useIsMobile } from '../hooks/useIsMobile'
import {
  PanelBottomLight, PanelBottomSolid, PanelRightLight, PanelRightSolid,
  type PanelIconProps,
} from './icons/panels'

/** Whether the host that owns the side panel can dock it below the chat. The
 *  chat page can only while the App shell's activity bar is present; embed
 *  frames, popouts and the Members page always open the panel on the right.
 *  The host provides this once, so every glyph under it resolves the dock the
 *  way the panel itself does. Defaults to true, matching SidePanel's own
 *  `canDockBottom` default. */
const SidePanelCanDockBottomCtx = createContext(true)

export const SidePanelDockHost = SidePanelCanDockBottomCtx.Provider

/** Resolve where the side panel actually docks for the current host: the
 *  stored preference, unless the host cannot dock bottom or the window is
 *  mobile-width (the panel always opens on the right there). */
function useEffectiveSidePanelDock(): 'right' | 'bottom' {
  const [dock] = useSidePanelDock()
  const isMobile = useIsMobile()
  const canDockBottom = useContext(SidePanelCanDockBottomCtx)
  return canDockBottom && dock === 'bottom' && !isMobile ? 'bottom' : 'right'
}

/** The side panel's glyph, drawn for where the panel actually docks. Every
 *  control that opens or closes the side panel uses this so none of them keeps
 *  pointing right after the dock flips. `light` picks the thick-pane (open)
 *  variant, for a control shown while the panel is open. */
export function SidePanelGlyph({ light, ...props }: PanelIconProps & { light?: boolean }) {
  const isBottom = useEffectiveSidePanelDock() === 'bottom'
  const Icon = isBottom
    ? (light ? PanelBottomLight : PanelBottomSolid)
    : (light ? PanelRightLight : PanelRightSolid)
  return <Icon {...props} />
}
