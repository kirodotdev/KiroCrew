/** The words on an interrupted session's sidebar line: "Turn interrupted",
 *  then what to do about it. */
import { i18nT } from '../../i18n/t'
import { useResumeAllWaiting } from '../../lib/resumeAllWaiting'
import { slotIsRemoteBound } from '../../store/dashboardSlice'
import type { Slot } from './types'

/** The interrupted line's label for *s*.
 *
 *  A crew-bound row must not name Resume: the composer offers no such control
 *  there (`selectContinuable` mirrors the server's `relay_archive_read_only`
 *  refusal), so the instruction would point at a button that is not on screen.
 *  The interruption is still real and still needs the marker; only the
 *  instruction is dropped. This shares `slotIsRemoteBound` with the composer
 *  deliberately: the row and that gate answer the SAME question, so one
 *  spelling keeps the label from drifting if the server's refusal is ever keyed
 *  elsewhere.
 *
 *  While a Resume-all run this tab waits on has not reached the session yet,
 *  the line says it is waiting rather than inviting a press the run is about
 *  to make. */
export function useInterruptedRowLabel(s: Slot): string {
  const waiting = useResumeAllWaiting(s.key)
  const interrupted = i18nT('pages.chat.recoveryCard.turn_interrupted')
  const next = waiting ? i18nT('pages.chatSidebar.resume_all_waiting') : i18nT('components.chatInput.resume')
  const label = slotIsRemoteBound(s) ? interrupted : `${interrupted} · ${next}`
  return label
}
