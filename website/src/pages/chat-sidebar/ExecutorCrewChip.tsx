import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { RemoteCrewChip } from '../../components/RemoteCrewChip'
import { i18nT } from '../../i18n/t'

/** The runs-elsewhere chip of a remote-EXECUTED local row, with the peer's
 *  display name read from the SHARED ['instances'] cache. Its own component so
 *  the query observer exists only on rows that are actually bound: an observer
 *  in every row, even a disabled one, re-registers its options on every row
 *  render, and each registration is a query-cache event every cache listener
 *  answers, a per-row cost a full folder pays on each sidebar commit. Falls
 *  back to the instance id: it is less friendly but it is true, and a blank chip
 *  would claim the session runs somewhere unnamed. */
export function ExecutorCrewChip({ instanceId }: { instanceId: string }) {
  const { data } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: !!instanceId,
  })
  const name = data?.instances?.find(i => i.id === instanceId)?.name || instanceId
  return (
    <RemoteCrewChip
      name={name}
      label={i18nT('pages.chatSidebar.on_instance', { name })}
      title={i18nT('pages.chatSidebar.runs_on_crew', { name })}
    />
  )
}
