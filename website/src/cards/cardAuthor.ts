import { createContext, useContext } from 'react'

/**
 * Who proposed the card, as the person reads them: the crewmate's display name
 * inside its chat, the product name everywhere else. The card copy says this
 * name ("Pebble's reason"), never "the agent".
 */
export const CARD_AUTHOR_DEFAULT = 'Kiro Crew'

export const CardAuthorContext = createContext<string>(CARD_AUTHOR_DEFAULT)

export function useCardAuthor(): string {
  return useContext(CardAuthorContext) || CARD_AUTHOR_DEFAULT
}
