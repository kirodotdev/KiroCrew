/**
 * Glass — the one place the Liquid Glass recipe lives.
 *
 * Every surface that floats over the transcript in the composer dock (the
 * composer itself, an approval bar, the follow-up chips, a tip or suggestion
 * card, the queue, the memory chip, the jump-to-bottom button), the mobile
 * Settings search capsule, the notification panes (the in-app banner, the
 * bell popover's rows and controls card), the list panels' search field
 * (Sessions sidebar, Crew Members roster; components/SearchFilterBar.tsx) and
 * the crewmate DM header's centred identity pill (face + name, itself the
 * "Edit crewmate" button; pages/members/MembersPage.tsx) wear the SAME
 * material, from the SAME primitive:
 * `--glass-tint` over a blurred backdrop, an even top/bottom light band in
 * `--glass-band`, a 1px `--glass-edge` line down each side and a half-pixel
 * `--glass-hairline` just outside the top and bottom edges. No ring: the
 * reference material sets two lit edges against thin dark sides and draws
 * nothing around the corners, so the host has no border. Call sites say what
 * they are (`variant`), how round they are (`radius`) and which element they
 * ARE (`as`); they never restate the optics.
 *
 * Two variants, one recipe: `panel` and `chip` now share the same optics
 * (frost 4, light 25) -- the maintainer wants one blur across every pane and the
 * light band reaching full white on every edge, chips included -- and differ
 * only in name, kept so a call site still says which kind of box it is; the
 * chip used to run lighter (frost 4, light 18) for the small pills and cards, where
 * the panel numbers read heavy at 30px tall.
 *
 * The pane IS the host element — there is no wrapper box. A follow-up chip is
 * `<Glass as="button" …>`: the button is the flex item, carries the width cap,
 * the entrance animation and its own `onClick`, and the effect layers sit
 * inside it under the label. `className` and `style` go on that host and carry
 * LAYOUT (margin, width, flex, padding) plus the box-shadow state the caller
 * owns — `glass-shadow` for the neutral rest shadow every pane wears (the
 * session composer included), plus `approval-glow` stacked on it while a
 * decision is pending — because which shadow a pane wears at this instant is
 * the caller's state, not the material's. A hue is mixed INTO the tint with `glass-accent`
 * (picked chip, tip card) or `glass-warn` (incognito chip), and `glass-hover`
 * brightens an interactive pane a step on hover — all three swap `--glass-tint`
 * on the host (index.css), so the pane stays the same material. Focus changes
 * NOTHING on the pane — no theme colour, no brighter tint, no darker side
 * lines, no deeper shadow (maintainer decision): a focused pane is the same
 * glass as a resting one, and the focus indicator is the caret, or the app's
 * own `:focus-visible` ring on a pane that is itself the control. The optics
 * are not open for override here — change the recipe, not the call site.
 */
import { forwardRef, type ReactElement, type Ref } from 'react'
import { LiquidGlass, type GlassHostTag, type LiquidGlassOwnProps, type LiquidGlassProps } from './ui/liquid-glass'

export type GlassVariant = 'panel' | 'chip'

const RECIPE: Record<GlassVariant, Pick<LiquidGlassOwnProps, 'frost' | 'lightIntensity'>> = {
  panel: { frost: 4, lightIntensity: 25 },
  chip: { frost: 4, lightIntensity: 25 },
}

export type GlassProps<T extends GlassHostTag = 'div'> = Omit<LiquidGlassProps<T>, 'cornerRadius' | 'frost' | 'lightIntensity'> & {
  variant?: GlassVariant
  /** Corner radius in px of the pane. */
  radius: number
}

function GlassImpl(
  { variant = 'panel', radius, ...rest }: GlassProps<GlassHostTag>,
  ref: Ref<HTMLElement>,
) {
  return <LiquidGlass {...(rest as Omit<LiquidGlassProps<GlassHostTag>, 'cornerRadius' | 'frost' | 'lightIntensity'>)} ref={ref} {...RECIPE[variant]} cornerRadius={radius} />
}

/** Polymorphic on `as`, like the primitive: `<Glass as="button" onClick …>` type-checks. */
export const Glass = forwardRef(GlassImpl) as <T extends GlassHostTag = 'div'>(
  props: GlassProps<T> & { ref?: Ref<HTMLElement> },
) => ReactElement

export default Glass
