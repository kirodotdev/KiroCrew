import { i18nT } from '../../i18n/t'
import { toolStatusLabel, type ToolStatusDetail } from '../../utils/toolStatusLabel'

/** A slot's running-status line. Every phase resolves through toolStatusLabel:
 *  the fixed phases (`thinking`/`streaming`) carry no copy in the store and map
 *  to catalog keys at render time, a server-supplied status (also
 *  `kind: 'thinking'`, with its own `label`) is passed through, and a `tool`
 *  phase honors the user's `simplifiedToolNames` preference (purpose vs raw tool
 *  title), so the row agrees with the inline tool pill in the transcript rather
 *  than always showing the purpose. The generic copy covers whatever resolves to
 *  nothing (an `idle` phase caught before the slot list refreshes). Split out of
 *  `SessionRow.tsx` to keep that file under the line cap. */
export function slotStatusText(detail: ToolStatusDetail | undefined, simplifiedToolNames: boolean, uiLang: string): string {
  return toolStatusLabel(detail, simplifiedToolNames, uiLang) || i18nT('pages.chatSidebar.thinking')
}
