import { useId } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import ErrorNotice from './ErrorNotice'
import { Toggle } from './ui'

/** The on/off switches a Claude session's model may advertise, each keyed by its
 *  selection-capabilities field: `ultracode`, and `fast` (Fast mode). */
export type SessionSwitch = 'ultracode' | 'fast'

const SWITCHES: Record<SessionSwitch, {
  write: (slot: string, enabled: boolean) => Promise<boolean>
  label: () => string
  hint: () => string
  /** The hint of a switch its harness says cannot be turned on, given the
   *  harness's reason (`""` when it gives none). */
  blockedHint?: (reason: string) => string
}> = {
  ultracode: {
    write: (slot, enabled) => api.chatSlotUltracode(slot, enabled).then(r => r.ultracode),
    label: () => i18nT('components.ultracodeToggle.label'),
    hint: () => i18nT('components.ultracodeToggle.hint'),
  },
  fast: {
    write: (slot, enabled) => api.chatSlotFastMode(slot, enabled).then(r => r.fast),
    label: () => i18nT('components.fastModeToggle.label'),
    hint: () => i18nT('components.fastModeToggle.hint'),
    blockedHint: reason => (reason
      ? i18nT('components.fastModeToggle.unavailable', { reason })
      : i18nT('components.fastModeToggle.unavailable_no_reason')),
  },
}

/** One of the Claude session's switches, under the effort slider in the model
 *  picker. The caller renders it only while the slot's selection-capabilities
 *  read carries the switch's field, which it does only while the session's model
 *  advertises the option. A write answers the value the session reads back, and
 *  that value replaces the cached one. A switch that reads off while its harness
 *  says it cannot be turned on (`unavailable`, the harness's reason) is drawn
 *  disabled, its hint row naming the reason; one that reads on stays writable,
 *  so it can always be turned off. */
export default function SessionSwitchToggle({ slot, option, enabled, unavailable }: { slot: string; option: SessionSwitch; enabled: boolean; unavailable?: string }) {
  const queryClient = useQueryClient()
  const hintId = useId()
  const spec = SWITCHES[option]
  const write = useMutation({
    mutationFn: (next: boolean) => spec.write(slot, next),
    onSuccess: async value => {
      // A poll that read the old value must not land on top of the read-back.
      await queryClient.cancelQueries({ queryKey: ['slot-selection-capabilities', slot] })
      queryClient.setQueryData<Partial<Record<SessionSwitch, boolean>>>(
        ['slot-selection-capabilities', slot],
        prev => (prev ? { ...prev, [option]: value } : prev),
      )
    },
  })
  const label = spec.label()
  const blockedHint = typeof unavailable === 'string' && !enabled && spec.blockedHint ? spec.blockedHint(unavailable) : null
  return (
    <div className="shrink-0 border-t border-border px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-text">{label}</span>
        <Toggle
          checked={write.isPending ? write.variables : enabled}
          onChange={next => write.mutate(next)}
          disabled={write.isPending || blockedHint !== null}
          label={label}
          describedBy={hintId}
        />
      </div>
      <p id={hintId} className="mt-1 text-[11px] leading-snug text-muted">
        {blockedHint ?? spec.hint()}
      </p>
      {/* No hand-off: the picker sits over the composer, which may hold an unsent draft. */}
      <ErrorNotice className="mt-1.5" variant="inline" message={write.isError ? agentSwitchFailureMessage(write.error) : null} />
    </div>
  )
}
