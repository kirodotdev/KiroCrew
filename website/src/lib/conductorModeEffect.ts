/** Fires the short conductor mode turn-on effect (components/crew/ConductorModeEffect)
 *  from the composer chip, with the chip's own rect. */
export const CONDUCTOR_MODE_EFFECT_EVENT = 'mc-conductor-mode-effect'
export function fireConductorModeEffect(origin: DOMRect | null): void {
  window.dispatchEvent(new CustomEvent(CONDUCTOR_MODE_EFFECT_EVENT, { detail: origin ? { x: origin.left + origin.width / 2, y: origin.top + origin.height / 2 } : null }))
}
