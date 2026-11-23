import { createContext } from 'react'

/** Whether a composer exists to stage a session reference into (not split
 *  view, an active slot present). The session row menu's "Reference in chat"
 *  item reads it. A context rather than a session-row action because it flips
 *  when the first chat opens: as a row action, that flip re-rendered every
 *  session row in the sidebar inside the task that opens the chat. A row menu
 *  mounts its items only while it is open, so here the flip reaches nothing
 *  else. Defaults to true: a menu outside the sidebar gets no
 *  `onReferenceInChat` handler, which already hides the item. */
export const SessionRefStageContext = createContext(true)
