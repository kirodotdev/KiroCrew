import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { pendingSlotSwitchTarget, performSlotSwitch } from '../lib/slotSwitch'
import { useAppDispatch } from '../store'
import { updateSlot } from '../store/dashboardSlice'
import ErrorNotice from './ErrorNotice'
import { ApiError } from '../api/apiError'
import { NativeSelect, NativeSelectOption } from './ui/native-select'

export type AdvisorOverride = 'inherit' | 'on' | 'off'

const ADVISOR_OVERRIDES: AdvisorOverride[] = ['inherit', 'on', 'off']

export default function AdvisorOverrideControl({
  slot,
  currentOverride = 'inherit',
}: {
  slot: string
  currentOverride?: AdvisorOverride
}) {
  const dispatch = useAppDispatch()
  const [selected, setSelected] = useState<AdvisorOverride>(currentOverride)
  const authoritativeRef = useRef(currentOverride)
  const intentRef = useRef(0)
  authoritativeRef.current = currentOverride

  useEffect(() => {
    if (pendingSlotSwitchTarget('advisor_override', slot) === null) setSelected(currentOverride)
  }, [currentOverride, slot])

  const mutation = useMutation({
    mutationFn: ({ value }: { value: AdvisorOverride; intent: number }) => performSlotSwitch(
      'advisor_override',
      slot,
      value,
      async () => {
        const response = await api.chatSlotAdvisorOverride(slot, value)
        return response.advisor_override ?? value
      },
      advisorOverride => dispatch(updateSlot({ key: slot, advisor_override: advisorOverride })),
    ),
    onMutate: ({ value }) => setSelected(value),
    onError: (error, { intent }) => {
      if (intent === intentRef.current) setSelected(authoritativeRef.current)
      // The two refusal codes get their own copy below; anything else shows the
      // plain failure line, and the raw body goes to the console, not the popover.
      // eslint-disable-next-line no-console -- the raw body is for the dev console, not the popover
      console.warn('advisor override update failed', error)
    },
  })

  const persist = (next: string) => {
    mutation.mutate({ value: next as AdvisorOverride, intent: ++intentRef.current })
  }

  // The effective state: "Inherit" alone hides whether anything actually
  // reviews (the shipped default is off), so the option names the value it
  // inherits and the helper only claims active review when review is active.
  const globalQ = useQuery<{ advisor?: { enabled?: boolean }; agent?: { acp_backend?: string } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    staleTime: 30_000,
  })
  // Only DERIVE an effective state once the config query has actually
  // succeeded: a failed/pending load left as `?? false` would render a
  // confident "Inherit (off)" that lies about whether review is on. Until
  // then the option stays the neutral "Inherit" and the helper is silent;
  // the failure is surfaced through ErrorNotice below.
  const globalResolved = globalQ.isSuccess
  const globalOn = globalQ.data?.advisor?.enabled ?? false
  const inheritedLabel = !globalResolved
    ? i18nT('components.advisorOverrideControl.inherit')
    : globalOn
      ? i18nT('components.advisorOverrideControl.inherit_on')
      : i18nT('components.advisorOverrideControl.inherit_off')
  const effectiveOn =
    selected === 'on' || (globalResolved && selected === 'inherit' && globalOn)
  // The untouched default (Inherit under a resolved global-off) gets one
  // state-free sentence saying what the Advisor is; nothing renders until the
  // global setting has resolved, so no explainer sits beside a pending or
  // failed load. An explicit Disabled, or an Inherit that resolves to on,
  // gets its one-line state sentence.
  const helperKey =
    selected === 'inherit' && !effectiveOn
      ? globalResolved
        ? 'components.advisorOverrideControl.helper_inherit_off'
        : null
      : effectiveOn
        ? 'components.advisorOverrideControl.helper'
        : 'components.advisorOverrideControl.helper_off'
  const labels = [
    inheritedLabel,
    i18nT('components.advisorOverrideControl.on'),
    i18nT('components.advisorOverrideControl.off'),
  ]

  // The reviewer runs on the kiro-cli agent backend only. Under another
  // selected backend the control is absent: a disabled row telling every
  // user to change backends is daily chrome about a setting most will never
  // touch. A request that races a backend switch still gets the 409 reason.
  if (globalResolved && (globalQ.data?.agent?.acp_backend ?? '') !== '') return null
  // Nothing until the global setting resolved (or failed): a row that then
  // relabels or vanishes would spring the popover's height once per load.
  if (!globalResolved && !globalQ.isError) return null

  return (
    <div className="shrink-0 border-t border-border px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="inline-flex items-center gap-1.5 text-[13px] text-muted">
          <ShieldCheck className="lucide-inline" />
          {i18nT('components.advisorOverrideControl.advisor')}
        </span>
        {/* Native on every device: the popover closes on any document click
            outside it, so a portaled listbox (SimpleSelect) would dismiss it
            on the very click that picks an option. */}
        <NativeSelect
          value={selected}
          onChange={e => persist(e.target.value as AdvisorOverride)}
          aria-label={i18nT('components.advisorOverrideControl.advisor_mode')}
          className="h-7 min-w-[112px] py-1 text-[12px]"
        >
          {ADVISOR_OVERRIDES.map((opt, i) => (
            <NativeSelectOption key={opt} value={opt}>{labels[i]}</NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
      <p className="mt-1 text-[12px] leading-4 text-muted">
        {helperKey ? i18nT(helperKey) : null}
      </p>
      {globalQ.isError && (
        <div className="mt-1.5">
          {/* No hand-off: the chat composer draft beneath this popover is
              unsaved, and a hand-off would navigate away from it. */}
          <ErrorNotice
            message={i18nT('components.advisorOverrideControl.config_load_failed')}
            variant="inline"
          />
        </div>
      )}
      {mutation.isError && (
        <div className="mt-1.5">
          {/* No hand-off: the chat composer draft beneath this popover is unsaved. */}
          <ErrorNotice
            message={
              // The two refusals a user can act on (non-kiro backend, no
              // credential-masking sandbox) get a localized sentence; any other
              // failure shows the plain line and its raw body goes to the console.
              mutation.error instanceof ApiError && mutation.error.body.includes('advisor_sandbox_unavailable')
                ? i18nT('components.advisorOverrideControl.unavailable_sandbox')
                : mutation.error instanceof ApiError && mutation.error.body.includes('advisor_unavailable')
                  ? i18nT('components.advisorOverrideControl.unavailable_backend')
                  : i18nT('components.advisorOverrideControl.update_failed')
            }
            variant="inline"
            onDismiss={() => mutation.reset()}
          />
        </div>
      )}
    </div>
  )
}
