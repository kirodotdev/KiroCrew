/**
 * Ghost dress-up props.
 *
 * Lives in shared/ because two windows need it: the pet overlay renders the prop,
 * and the avatar editor (gallery window) offers the picker. Importing it from
 * PetWidget would pull that component's whole SVG asset graph into the gallery
 * bundle, so the list and its type live here instead.
 *
 * Only the default ghost wears props — custom appearance packs supply their own art.
 */
export type GhostAccessory =
  | 'none' | 'sunglasses' | 'flower' | 'antenna' | 'partyhat' | 'sleepmask' | 'coffee' | 'popper'

export const GHOST_ACCESSORIES: { id: GhostAccessory; labelKey: string }[] = [
  { id: 'none', labelKey: 'apps.crewCompanion.accessory.noprop' },
  { id: 'sunglasses', labelKey: 'apps.crewCompanion.accessory.shades' },
  { id: 'partyhat', labelKey: 'apps.crewCompanion.accessory.partyhat' },
  { id: 'popper', labelKey: 'apps.crewCompanion.accessory.popper' },
  { id: 'sleepmask', labelKey: 'apps.crewCompanion.accessory.sleepmask' },
  { id: 'coffee', labelKey: 'apps.crewCompanion.accessory.coffee' },
]

/**
 * Props that sit OVER the eyes, so the live eye layer must not draw underneath.
 * Includes 'antenna', which is no longer in the pickable list above but is still a
 * valid id an older config could hold — and it does still render.
 */
export const HIDES_EYES = new Set<GhostAccessory>(['sunglasses', 'antenna', 'sleepmask'])

/**
 * Props used for the celebrate reaction on a "job done" notification, picked at
 * random so the celebration doesn't feel canned. 'none' is included on purpose —
 * a plain hop should stay a common outcome, otherwise every completion turns
 * into confetti and the flourish stops meaning anything.
 */
export const CELEBRATE_PROPS: GhostAccessory[] = ['none', 'partyhat', 'popper', 'sunglasses']

/** Pick a celebrate prop at random. */
export function randomCelebrateProp(rand: () => number = Math.random): GhostAccessory {
  return CELEBRATE_PROPS[Math.floor(rand() * CELEBRATE_PROPS.length)] ?? 'none'
}
