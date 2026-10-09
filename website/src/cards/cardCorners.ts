import { createContext, useContext } from 'react'

/**
 * The corner classes of a card's outer surface. A standalone card is evenly
 * rounded; inside a crewmate's run the transcript provides the run's grouped
 * corners, so a card reads as one more bubble of that reply rather than a
 * differently shaped box beside it.
 */
export const CARD_CORNERS_DEFAULT = 'rounded-xl'

export const CardCornersContext = createContext<string>(CARD_CORNERS_DEFAULT)

export function useCardCorners(): string {
  return useContext(CardCornersContext)
}
