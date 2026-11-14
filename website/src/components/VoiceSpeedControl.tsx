import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { VOICE_SPEED_PRESETS, formatVoiceSpeed, voiceSpeedMultiplier } from '../lib/voiceSpeed'
import ErrorNotice from './ErrorNotice'
import { RadioChipGroup, radioChip } from './RadioChipGroup'
import { Btn } from './ui'

type VoiceSpeedConfig = { rate?: string }

const VOICE_CONFIG_KEY = ['voiceConfig']

function announceRate(rate: string | undefined) {
  window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: { rate } }))
}

/** The speaking-rate presets as one single-choice control. The rate is the
 *  gateway's `voice_reply.rate`, the value Settings → Voice → Speed edits, so
 *  the two stay in step and the choice survives reloads. A saved choice applies
 *  from the next synthesized audio, not to audio already queued, so a note under
 *  the group says so and points at Read again. `children` share the group's
 *  line, after it, and wrap under it where the line is too narrow. */
export default function VoiceSpeedControl({ children }: { children?: ReactNode }) {
  const queryClient = useQueryClient()
  const labelId = useId()
  const [appliesNextShown, setAppliesNextShown] = useState(false)
  const voiceConfig = useQuery<VoiceSpeedConfig>({ queryKey: VOICE_CONFIG_KEY, queryFn: () => api.voiceConfig() })
  const reconcileStoredRate = async () => {
    await queryClient.invalidateQueries({ queryKey: VOICE_CONFIG_KEY, refetchType: 'none' })
    try {
      const stored = await queryClient.fetchQuery<VoiceSpeedConfig>({
        queryKey: VOICE_CONFIG_KEY,
        queryFn: () => api.voiceConfig(),
      })
      announceRate(stored.rate)
    } catch {
      // The query's ErrorNotice owns this failure and offers Retry.
    }
  }
  const setRate = useMutation({
    mutationFn: (rate: string) => api.updateVoiceConfig({ rate }),
    onMutate: async rate => {
      setAppliesNextShown(false)
      await queryClient.cancelQueries({ queryKey: VOICE_CONFIG_KEY })
      const previous = queryClient.getQueryData<VoiceSpeedConfig>(VOICE_CONFIG_KEY)
      queryClient.setQueryData<VoiceSpeedConfig>(VOICE_CONFIG_KEY, config => ({ ...config, rate }))
      announceRate(rate)
      return { previous }
    },
    onSuccess: () => setAppliesNextShown(true),
    onError: (_error, _rate, context) => {
      queryClient.setQueryData(VOICE_CONFIG_KEY, context?.previous)
      announceRate(context?.previous?.rate)
    },
    onSettled: reconcileStoredRate,
  })
  const storedRate = voiceConfig.data?.rate
  const loaded = !!voiceConfig.data
  // A pending save keeps every option focusable (aria-disabled, not native
  // disabled): native disabled on the option just chosen would drop a keyboard
  // or screen-reader user's focus to the page.
  const saving = setRate.isPending
  return (
    <div className="flex flex-col items-end" data-testid="voice-playback-speed-row">
      <div className="flex max-w-full flex-wrap items-center justify-end gap-x-2 gap-y-1">
        <span id={labelId} className="text-muted">{i18nT('pages.settings.voicePanel.speed')}</span>
        <RadioChipGroup
          ariaLabelledBy={labelId}
          className="inline-flex rounded-md border border-border bg-bg p-0.5"
          data-testid="voice-playback-speed"
        >
          {VOICE_SPEED_PRESETS.map(preset => {
            const checked = storedRate !== undefined && voiceSpeedMultiplier(storedRate) === voiceSpeedMultiplier(preset)
            return (
              <button
                key={preset}
                type="button"
                {...radioChip(checked)}
                disabled={!loaded}
                aria-disabled={saving || undefined}
                aria-busy={saving || undefined}
                onClick={() => { if (!saving && !checked) setRate.mutate(preset) }}
                className={`rounded px-2 py-0.5 tabular-nums transition-colors focus-ring disabled:cursor-not-allowed aria-disabled:cursor-not-allowed [@media(hover:none)]:min-h-10 [@media(hover:none)]:min-w-10 ${
                  checked ? 'bg-accent/15 font-medium text-accent' : 'text-muted hover:text-text disabled:hover:text-muted aria-disabled:hover:text-muted'
                }`}
              >
                {formatVoiceSpeed(preset)}
              </button>
            )
          })}
        </RadioChipGroup>
        {children}
      </div>
      {/* Always mounted so the polite announcement fires when the text arrives;
          empty, it takes no space and the group above never moves. */}
      <p
        role="status"
        className={appliesNextShown ? 'mt-1 max-w-full text-right text-[12px] text-muted' : 'm-0'}
        data-testid="voice-speed-applies-next"
      >
        {appliesNextShown ? i18nT('components.voicePlaybackBar.speedAppliesNext') : null}
      </p>
      {voiceConfig.isError && (
        <div className="mt-1 flex max-w-full flex-wrap items-center justify-end gap-2" data-testid="voice-speed-load-error">
          <ErrorNotice
            variant="inline"
            message={i18nT('components.voicePlaybackBar.speedLoadFailed')}
            askAgent
            actionPlacement="below-on-phone"
          />
          <Btn danger busy={voiceConfig.isFetching} onClick={() => { void reconcileStoredRate() }}>
            {i18nT('pages.settings.voicePanel.retry')}
          </Btn>
        </div>
      )}
      <ErrorNotice
        variant="inline"
        message={setRate.isError ? i18nT('components.voicePlaybackBar.speedSaveFailed') : null}
        askAgent
        actionPlacement="below-on-phone"
        onDismiss={() => setRate.reset()}
        className="mt-1 justify-end"
        testId="voice-speed-save-error"
      />
    </div>
  )
}
