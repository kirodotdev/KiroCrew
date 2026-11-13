/** Fires the short Ultra Mode turn-on effect (components/crew/UltraModeEffect)
 *  from the composer chip, with the chip's own rect. */
export const ULTRA_EFFECT_EVENT = 'mc-ultra-mode-effect'
export function fireUltraEffect(origin: DOMRect | null): void {
  window.dispatchEvent(new CustomEvent(ULTRA_EFFECT_EVENT, { detail: origin ? { x: origin.left + origin.width / 2, y: origin.top + origin.height / 2 } : null }))
}
