/**
 * Glass — the one place the Liquid Glass recipe lives.
 *
 * Every surface that floats over the transcript in the composer dock (the
 * composer itself, an approval bar, the follow-up chips, a tip or suggestion
 * card, the queue cards, the memory chip, the jump-to-bottom button, and the
 * status stack above the box: the task, sub-agent and workflow progress bars,
 * the Command Center card, the held-delivery line, the quote bubble in flight),
 * the mobile Settings search capsule, the notification panes (the in-app banner, the
 * bell popover's rows and controls card), the list panels' search field
 * (Sessions sidebar, Crew Members roster; components/SearchFilterBar.tsx) and
 * the crewmate DM header's centred identity pill (face + name, itself the
 * "Edit crewmate" button; pages/members/MembersPage.tsx) and the top bar's
 * three pills (the search trigger, the readout capsule and the Request a
 * Feature pill; App.tsx, components/FeedbackPill.tsx) wear the SAME
 * material, from the SAME primitive:
 * `--glass-tint` over a blurred backdrop, an even top/bottom light band in
 * `--glass-band`, a 1px `--glass-edge` line down each side and a half-pixel
 * `--glass-hairline` just outside the top and bottom edges. No ring: the
 * reference material sets two lit edges against thin dark sides and draws
 * nothing around the corners, so the host has no border. Call sites say what
 * they are (`variant`), how round they are (`radius`) and which element they
 * ARE (`as`); they never restate the optics.
 *
 * Two variants, one recipe: `panel` and `chip` share the same optics (light
 * 25) -- the maintainer wants the light band reaching full white on every edge,
 * chips included -- and differ only in name, kept so a call site still says
 * which kind of box it is; the chip used to run lighter (light 18) for the small
 * pills and cards, where the panel numbers read heavy at 30px tall.
 *
 * One scale, five thicknesses (`thickness`, issue #16299): how much of what
 * lies under the pane shows through. A thickness is a MATERIAL step -- blur
 * radius and tint move TOGETHER along one ladder, the way Apple's
 * `.ultraThin … .ultraThick` materials do -- so a call site picks a step and
 * never a blur or an alpha of its own. The blur lives here (`GLASS_THICKNESS`,
 * handed to the primitive as `frost`); the tint lives in index.css as one
 * `--glass-tint-<step>` token per polarity, selected by the `glass-<step>`
 * class this component puts on the host, so the step follows light / dark like
 * every other glass token. The ladder is tuned so a pane over a plain page
 * keeps the SAME overall colour at every step (light: #f8f8f8 over white): as
 * the alpha rises the tint colour moves toward the page colour,
 * `c_step = page + (c_thin - page) * (a_thin / a_step)` per channel. `thin` is
 * the recipe every pane wore before the ladder existed (blur 4, alpha .40 dark /
 * .45 light) and stays the default, so nothing changes until a call site opts
 * into a step. Measured over a transcript bubble, the steps read: ultrathin
 * fully legible, thin legible, regular an outline, thick a wash, ultrathick
 * near-opaque (the maintainer accepted that the top two sit close).
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
 * (picked chip, tip card), `glass-warn` (incognito chip) or `glass-danger` (the
 * offline readout capsule), and `glass-hover`
 * brightens an interactive pane a step on hover — all four swap `--glass-tint`
 * on the host (index.css), so the pane stays the same material; the three hue
 * modifiers swap `--glass-band` as well, so the lit edges carry the hue. Focus changes
 * NOTHING on the pane — no theme colour, no brighter tint, no darker side
 * lines, no deeper shadow (maintainer decision): a focused pane is the same
 * glass as a resting one, and the focus indicator is the caret, or the app's
 * own `:focus-visible` ring on a pane that is itself the control. The optics
 * are not open for override here — change the recipe, not the call site.
 */
import { forwardRef, type ReactElement, type Ref } from 'react'
import { LiquidGlass, type GlassHostTag, type LiquidGlassOwnProps, type LiquidGlassProps } from './ui/liquid-glass'

export type GlassVariant = 'panel' | 'chip'

/** The five steps of the one thickness ladder, thinnest first. */
export const GLASS_THICKNESSES = ['ultrathin', 'thin', 'regular', 'thick', 'ultrathick'] as const
export type GlassThickness = (typeof GLASS_THICKNESSES)[number]

/**
 * Blur radius per step. The matching tint is `--glass-tint-<step>` in
 * index.css (dark / light), selected by the `glass-<step>` host class; the two
 * halves are one table split across the file that owns each value, and
 * `Glass.thickness.test.tsx` pins them together.
 */
export const GLASS_THICKNESS: Record<GlassThickness, Pick<LiquidGlassOwnProps, 'frost'>> = {
  ultrathin: { frost: 2 },
  thin: { frost: 4 },
  regular: { frost: 8 },
  thick: { frost: 14 },
  ultrathick: { frost: 28 },
}

/** The step a pane wears when its call site names none: the pre-ladder recipe. */
export const DEFAULT_GLASS_THICKNESS: GlassThickness = 'thin'

const RECIPE: Record<GlassVariant, Pick<LiquidGlassOwnProps, 'lightIntensity'>> = {
  panel: { lightIntensity: 25 },
  chip: { lightIntensity: 25 },
}

export type GlassProps<T extends GlassHostTag = 'div'> = Omit<LiquidGlassProps<T>, 'cornerRadius' | 'frost' | 'lightIntensity'> & {
  variant?: GlassVariant
  /** How much of the content under the pane shows through. Default `thin`. */
  thickness?: GlassThickness
  /** Corner radius in px of the pane. */
  radius: number
}

function GlassImpl(
  { variant = 'panel', thickness = DEFAULT_GLASS_THICKNESS, radius, className, ...rest }: GlassProps<GlassHostTag>,
  ref: Ref<HTMLElement>,
) {
  // The step's class goes FIRST so a caller's tint modifier (`glass-accent`,
  // `glass-warn`, …) that follows mixes into this step's tint, never into the
  // root's: index.css declares the modifiers after the step classes and has
  // them read `--glass-tint-step`, which the step class sets on this host.
  // `glass-<step>` is one of the five index.css step classes (pinned by
  // Glass.thickness.test.tsx); the caller's own classes follow it and the lint
  // reads those at each call site.
  // eslint-disable-next-line shadcn/require-static-classes
  const stepClass = `glass-${thickness}`
  return (
    <LiquidGlass
      {...(rest as Omit<LiquidGlassProps<GlassHostTag>, 'cornerRadius' | 'frost' | 'lightIntensity' | 'className'>)}
      ref={ref}
      className={className ? `${stepClass} ${className}` : stepClass}
      {...RECIPE[variant]}
      {...GLASS_THICKNESS[thickness]}
      cornerRadius={radius}
    />
  )
}

/** Polymorphic on `as`, like the primitive: `<Glass as="button" onClick …>` type-checks. */
export const Glass = forwardRef(GlassImpl) as <T extends GlassHostTag = 'div'>(
  props: GlassProps<T> & { ref?: Ref<HTMLElement> },
) => ReactElement

export default Glass
