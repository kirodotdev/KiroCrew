import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Send } from 'lucide-react'
import { api, type InstanceView } from '../api/client'
import SearchableSelect, { type SearchableSelectOption } from './SearchableSelect'
import { i18nT } from '../i18n/t'

/** The crew *Send a copy to* lists first, marked as the default (#21547).
 *
 *  Stored as `instances.default_destination`; a folder's own default
 *  destination crew overrides it for sessions in that folder. Choosing one only
 *  orders and marks the menu: nothing is sent without a click. Hidden with no
 *  registered crew, since the menu then has nothing to list. */
export default function DefaultDestinationCrewPicker({ instances, value }: {
  readonly instances: readonly InstanceView[]
  /** The stored id as served by GET /api/instances ('' = none). */
  readonly value: string
}) {
  const queryClient = useQueryClient()
  const [error, setError] = useState('')
  const save = useMutation({
    mutationFn: (id: string) => api.patchConfig('instances.default_destination', id),
    onMutate: () => setError(''),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['instances'] }) },
    onError: (e) => {
      setError(e instanceof Error && e.message
        ? e.message
        : i18nT('pages.settings.instancesPanel.default_destination_save_failed'))
    },
  })
  if (instances.length === 0) return null

  // A stored id naming no registered crew stays visible, so the user sees why
  // nothing is marked and can clear it.
  const options: SearchableSelectOption[] = [
    { value: '', label: i18nT('pages.settings.instancesPanel.default_destination_none') },
    ...(value && !instances.some(i => i.id === value)
      ? [{ value, label: i18nT('pages.settings.instancesPanel.default_destination_not_registered', { id: value }) }]
      : []),
    ...instances.map(i => ({ value: i.id, label: i.name })),
  ]
  const shown = save.isPending && typeof save.variables === 'string' ? save.variables : value

  return (
    <div className="flex flex-col gap-1.5 py-2" data-testid="default-destination-crew">
      <span className="flex items-center gap-1.5 text-[11.5px] font-semibold text-muted">
        <Send size={12} className="shrink-0" /> {i18nT('pages.settings.instancesPanel.default_destination')}
      </span>
      <SearchableSelect
        aria-label={i18nT('pages.settings.instancesPanel.default_destination')}
        options={options}
        value={shown}
        onChange={v => { if (v !== value) save.mutate(v) }}
      />
      {error
        ? <span className="text-[11px] text-danger" role="alert">{error}</span>
        : <span className="text-[11px] text-muted-strong">{i18nT('pages.settings.instancesPanel.default_destination_hint')}</span>}
    </div>
  )
}
