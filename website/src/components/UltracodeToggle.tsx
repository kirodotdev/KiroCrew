import { useId } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import ErrorNotice from './ErrorNotice'
import { Toggle } from './ui'

/** The Claude session's ultracode switch, under the effort slider in the model
 *  picker. The caller renders it only while the slot's selection-capabilities
 *  read carries `ultracode`, which it does only while the session's model
 *  advertises the option. A write answers the value the session reads back, and
 *  that value replaces the cached one. */
export default function UltracodeToggle({ slot, enabled }: { slot: string; enabled: boolean }) {
  const queryClient = useQueryClient()
  const hintId = useId()
  const write = useMutation({
    mutationFn: (next: boolean) => api.chatSlotUltracode(slot, next),
    onSuccess: async ({ ultracode }) => {
      // A poll that read the old value must not land on top of the read-back.
      await queryClient.cancelQueries({ queryKey: ['slot-selection-capabilities', slot] })
      queryClient.setQueryData<{ ultracode?: boolean }>(
        ['slot-selection-capabilities', slot],
        prev => (prev ? { ...prev, ultracode } : prev),
      )
    },
  })
  const label = i18nT('components.ultracodeToggle.label')
  return (
    <div className="shrink-0 border-t border-border px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-text">{label}</span>
        <Toggle
          checked={write.isPending ? write.variables : enabled}
          onChange={next => write.mutate(next)}
          disabled={write.isPending}
          label={label}
          describedBy={hintId}
        />
      </div>
      <p id={hintId} className="mt-1 text-[11px] leading-snug text-muted">
        {i18nT('components.ultracodeToggle.hint')}
      </p>
      {/* No hand-off: the picker sits over the composer, which may hold an unsent draft. */}
      <ErrorNotice className="mt-1.5" variant="inline" message={write.isError ? agentSwitchFailureMessage(write.error) : null} />
    </div>
  )
}
