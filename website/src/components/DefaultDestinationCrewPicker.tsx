import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Send } from 'lucide-react'
import { api, type InstanceView } from '../api/client'
import SearchableSelect, { type SearchableSelectOption } from './SearchableSelect'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

/** The crew *Send a copy to* lists first, marked as the default.
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
  // The raw failure text, kept verbatim: ErrorNotice matches it against the
  // error journal to recover the endpoint and status. The catalog sentence is
  // the notice's title, so the reader always gets it first.
  const [error, setError] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: (id: string) => api.patchConfig('instances.default_destination', id),
    onMutate: () => setError(null),
    // Returning the refetch keeps the save pending until `value` is the saved
    // id, so the picker below never offers a choice against a stale value.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['instances'] }),
    onError: (e) => { setError(e instanceof Error ? e.message : '') },
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
    // A Settings row: name and hint on the left, the select on the right.
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1.5 py-2" data-testid="default-destination-crew">
      <div className="flex-1 min-w-[200px]">
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text">
          <Send size={12} className="shrink-0" aria-hidden /> {i18nT('pages.settings.instancesPanel.default_destination')}
        </span>
        {error !== null ? (
          // No hand-off: this sits in Settings → Remote Crew, whose Add Crew
          // forms hold unsaved fields an agent chat would navigate away from.
          // Picking again retries in place.
          <ErrorNotice
            variant="inline"
            title={error ? i18nT('pages.settings.instancesPanel.default_destination_save_failed') : undefined}
            message={error || i18nT('pages.settings.instancesPanel.default_destination_save_failed')}
            onDismiss={() => setError(null)}
            testId="default-destination-crew-error"
            className="flex-wrap"
          />
        ) : <span className="block text-[12px] text-muted">{i18nT('pages.settings.instancesPanel.default_destination_hint')}</span>}
      </div>
      <div className="w-full sm:w-56">
        <SearchableSelect
          aria-label={i18nT('pages.settings.instancesPanel.default_destination')}
          options={options}
          value={shown}
          // Disabled while a save is in flight: a second pick then could match the
          // cached value and be dropped, leaving the first pick saved.
          disabled={save.isPending}
          onChange={v => { if (v !== shown) save.mutate(v) }}
        />
      </div>
    </div>
  )
}
