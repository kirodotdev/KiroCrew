/**
 * The words a roster card and row show for a crewmate's bindings: display
 * names and plain labels, never the internal ids the binding stores.
 *
 * - Built from: the same names the editor's Built From shows
 *   (`components/crew/builtFromName`). A crewmate bound to its own file
 *   (`kiro_agent` equal to its key) is named by its worker kit's template.
 * - Memory store: whether the memory is the crewmate's own or shared, from the
 *   same `memberMemoryState` the editor's memory row reads; the store id is a
 *   generated directory name nobody chose.
 */
import { useQuery } from '@tanstack/react-query'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import { parseErrorCode, reportForError, type ErrorReport } from '../utils/errorReport'
import { crewWorkerKitApi, crewWorkerKitKey } from '../api/crewCapabilities'
import { DEFAULT_TEMPLATE, kitTemplateName, templateDisplayName } from '../components/crew/builtFromName'
import { workerKitErrorText } from '../components/crew/workerKitErrors'
import { memberMemoryState } from './KiroCrewAgentsPageFields'

export type MemoryStores = Record<string, { memory_version?: number; owner_member?: string }>

/** What a roster cell shows for a crewmate's memory store. */
export function memoryStoreLabel(member: string, store: string, stores: MemoryStores | undefined): string {
  if (!stores && member !== 'default') return ''
  const state = memberMemoryState(member, store, stores)
  if (state === 'private') return i18nT('pages.kiroCrewAgentsPage.memory_label_private')
  if (state === 'legacy') return i18nT('pages.kiroCrewAgentsPage.memory_label_shared')
  return i18nT('pages.kiroCrewAgentsPage.memory_label_check')
}

/**
 * The template name a roster cell shows for "Built from".
 *
 * A binding whose `kiro_agent` is the crewmate's own key reads its worker kit;
 * the kit endpoint answers `not_a_mate_file` when that name is in fact a shared
 * template, which is then shown by its own name. Empty while the kit loads.
 * Any other failure to read the kit is returned as `error`, with no label, so
 * the cell shows it rather than a provenance it does not know, and `report`
 * carries the request behind it for the agent hand-off.
 */
export function useRosterBuiltFrom(agent: { name: string; kiro_agent: string }): { label: string; error: string | null; report?: ErrorReport } {
  // A crewmate keyed `kirocrew` is bound to the default template, not to a file.
  const ownsFile = !!agent.kiro_agent && agent.kiro_agent === agent.name && agent.kiro_agent !== DEFAULT_TEMPLATE
  const kit = useQuery({
    queryKey: crewWorkerKitKey(agent.name),
    queryFn: () => crewWorkerKitApi.get(agent.name),
    enabled: ownsFile,
    retry: false,
  })
  if (!ownsFile) return { label: templateDisplayName(agent.kiro_agent), error: null }
  if (kit.isError) {
    const err = kit.error
    if (err instanceof ApiError && parseErrorCode(err.body) === 'not_a_mate_file') {
      return { label: templateDisplayName(agent.kiro_agent), error: null }
    }
    // The sentence is localized, so the hand-off is given the journalled
    // report itself rather than looking it up by message.
    return { label: '', error: workerKitErrorText(err), report: reportForError(err) }
  }
  if (!kit.data) return { label: '', error: null }
  return { label: kitTemplateName(kit.data) ?? templateDisplayName(DEFAULT_TEMPLATE), error: null }
}
