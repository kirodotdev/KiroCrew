import { useSidePanelDock } from '../hooks/useSidePanelDock'
import { useIsMobile } from '../hooks/useIsMobile'
import {
  PanelBottomLight, PanelBottomSolid, PanelRightLight, PanelRightSolid,
  type PanelIconProps,
} from './icons/panels'

/** The side panel's glyph, drawn for where the panel actually docks: the
 *  bottom-dock pane when the user chose "Dock panel below chat", else the
 *  right-dock pane. Every control that opens or closes the side panel uses
 *  this so none of them keeps pointing right after the dock flips. Mobile
 *  always renders the panel on the right, so it keeps the right glyph there.
 *
 *  `light` picks the thick-pane (open) variant, for a control shown while the
 *  panel is open. `bottom` overrides the resolved dock for a host that knows
 *  better (SidePanel, whose `canDockBottom` can pin it right). */
export function SidePanelGlyph({ light, bottom, ...props }: PanelIconProps & { light?: boolean; bottom?: boolean }) {
  const [dock] = useSidePanelDock()
  const isMobile = useIsMobile()
  const isBottom = bottom ?? (dock === 'bottom' && !isMobile)
  const Icon = isBottom
    ? (light ? PanelBottomLight : PanelBottomSolid)
    : (light ? PanelRightLight : PanelRightSolid)
  return <Icon {...props} />
}
