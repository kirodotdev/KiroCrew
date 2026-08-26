import { PanelRightLight, PanelBottomLight } from './icons/panels'

/** Split-direction icon: a right panel = split right, a bottom panel = split down.
 *  Uses the thick-pane (open) variants: the glyph depicts the pane the split
 *  will occupy, not a panel's open/closed state — and it is a pure indicator,
 *  so its enclosing controls deliberately carry no `pi-morph`. */
export function SplitGlyph({ down }: { down?: boolean }) {
  return down ? <PanelBottomLight size={12} /> : <PanelRightLight size={12} />
}
