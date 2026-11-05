import { createContext, useContext } from 'react'
import type { MemberRosterRow } from '../api/client'

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

/**
 * The crewmate that proposed a card, read from the card itself rather than the
 * page showing it: a card proposed in a crewmate's own chat carries that chat's
 * slot key, which the roster binds to the crewmate. Its display name wins over
 * its name. `null` when no crewmate owns the slot (the main assistant proposed
 * it) or the roster is not loaded yet; the caller then keeps the current author.
 */
export function cardProposer(slotKey: string | undefined, rows: readonly MemberRosterRow[] | undefined): string | null {
  if (!slotKey || !rows) return null
  const row = rows.find(r => r.slot_key && r.slot_key === slotKey)
  if (!row) return null
  return row.display_name?.trim() || row.name?.trim() || null
}
