import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Cloud, Laptop, Loader2 } from 'lucide-react'
import { CoderIcon, FargateIcon } from './BrandIcon'
import { api, type SlotEnvironment } from '../api/client'
import { ApiError, apiErrorCode } from '../api/apiError'
import ErrorNotice from './ErrorNotice'
import { START_POLL_MS, slotEnvironmentQueryKey, useEnvironmentStartLabel } from './sessionEnvironmentHooks'
import { useAppDispatch } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'

/** Radix radio values are strings; this one stands for "no profile". */
const LOCAL_VALUE = '__local__'

interface Props {
  slot: string
  /** A running turn keeps its environment: switching applies at the next start. */
  disabled?: boolean
  /** Narrow shelf: icon only, the name stays in the tooltip. */
  compact?: boolean
}

/** Composer chip choosing where a session's files and commands live: this
 *  machine, or one of the Session Environments profiles from Settings.
 *
 *  Renders nothing until profiles exist, so a gateway with no remote
 *  environment configured shows no new control. The endpoint is owner-only, so
 *  an app caller's 403 also renders nothing. Lives in its own separated group
 *  on the composer shelf (`max-two-buttons-per-row`). */
/** The provider's brand mark beside a profile, decorative (the name is the text
 * next to it). An unknown or missing provider, as from an older gateway, keeps
 * the generic cloud. */
function ProviderGlyph({ provider, size }: { provider?: string; size: number }) {
  const mark = provider === 'coder' ? <CoderIcon size={Math.round(size * 0.8)} />
    : provider === 'fargate' ? <FargateIcon size={size} />
      : null
  if (!mark) return <Cloud size={size} className="shrink-0 opacity-80" aria-hidden />
  return <span data-testid={`provider-mark-${provider}`} className="inline-flex shrink-0 items-center" aria-hidden="true">{mark}</span>
}

export default function SessionEnvironmentChip({ slot, disabled, compact }: Props) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const key = slotEnvironmentQueryKey(slot)
  const { data, error, refetch } = useQuery({
    queryKey: key,
    queryFn: () => api.chatSlotEnvironment(slot),
    staleTime: 30_000,
    retry: false,
    // While a turn runs on a remote environment, its first start may be
    // creating compute for minutes: poll the start stage so the chip can show
    // progress instead of an indistinguishable "thinking". Local sessions and
    // idle composers never poll.
    refetchInterval: (query) => (disabled && query.state.data?.profile ? START_POLL_MS : false),
  })
  // The first send is what locks the choice; re-read when that turn ends so
  // the chip settles into its locked form without waiting out staleTime.
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !disabled) void refetch()
    wasRunning.current = !!disabled
  }, [disabled, refetch])
  // The same start the turn indicator shows (one query, one poll), including
  // the setup gap before the first stage is reported.
  const start = useEnvironmentStartLabel(slot, !!disabled)
  const choose = useMutation({
    mutationFn: (profile: string | null) => api.setChatSlotEnvironment(slot, profile),
    onSuccess: (result) => {
      queryClient.setQueryData<SlotEnvironment>(key, (old) =>
        old ? { ...old, profile: result.profile } : old,
      )
    },
    onError: (error: unknown) => {
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
    },
  })

  if (error) {
    if (error instanceof ApiError && error.status === 403 && apiErrorCode(error) === 'owner_only') return null
    return (
      // No hand-off: the chat composer beside this chip may hold an unsent draft.
      <ErrorNotice variant="inline" message={error.message || t('sessionEnvironments.loadError')} />
    )
  }
  if (!data || !Array.isArray(data.profiles) || data.profiles.length === 0) return null

  const pending = choose.isPending ? choose.variables : undefined
  const selected = pending !== undefined ? pending : data.profile
  const profile = data.profiles.find((p) => p.id === selected)
  // A choice naming a profile that was since removed still binds the session
  // (it fails closed at start), so show its id rather than "This machine".
  const name = profile?.name ?? selected ?? t('sessionEnvironments.picker.thisMachine')
  const remote = selected !== null
  // Only a start for the environment the slot is bound to; the stage is
  // cleared by the backend when the start ends (ready, failed or stopped).
  const starting = disabled && remote ? start : null
  const stageLabel = starting ? starting.stage : ''
  const elapsed = starting ? starting.elapsed : ''
  // Picked once for the session's life: the backend reports `locked` once the
  // session has a message, and refuses a change after that.
  const locked = !!data.locked
  const title = starting
    ? t('sessionEnvironments.picker.startingTitle', { name, stage: stageLabel, elapsed })
    : locked
      ? t('sessionEnvironments.picker.lockedTitle', { name })
      : disabled
        ? t('sessionEnvironments.picker.busy')
        : t('sessionEnvironments.picker.runsOn', { name })
  const tone = remote ? 'text-accent' : 'text-muted'
  // Same chip, two forms: while the environment starts it carries the stage
  // and elapsed time in place of the name, then returns to the name.
  // motion-safe: reduced motion keeps a still glyph.
  const content = (
    <>
      {starting
        ? <Loader2 size={13} className="shrink-0 motion-safe:animate-spin" aria-hidden />
        : remote
          ? <ProviderGlyph provider={profile?.provider} size={13} />
          : <Laptop size={13} className="shrink-0 opacity-80" aria-hidden />}
      {!compact && (
        <span className="truncate max-w-[220px] tabular-nums">
          {/* Stage only: the running step in the transcript carries the timer. */}
          {starting ? stageLabel : name}
        </span>
      )}
    </>
  )
  const announce = <span className="sr-only" role="status" aria-live="polite">{stageLabel}</span>

  if (locked) {
    // A status, not a control: same place and form as the picker it was, so it
    // reads as that chip settling rather than a new element appearing.
    return (
      <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
        <span
          data-testid="composer-environment-chip"
          title={title}
          aria-label={title}
          className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 ${tone}`}
        >
          {content}
        </span>
        {announce}
      </div>
    )
  }

  return (
    <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
      <DropdownMenu onOpenChange={(open) => { if (open) void refetch() }}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid="composer-environment-chip"
            disabled={disabled}
            title={title}
            aria-label={title}
            className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent ${remote ? 'text-accent' : 'text-muted hover:text-text disabled:hover:text-muted'}`}
          >
            {content}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          aria-label={t('sessionEnvironments.picker.menuLabel')}
          className="min-w-[240px] max-w-[340px]"
        >
          <DropdownMenuLabel>{t('sessionEnvironments.picker.menuLabel')}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={selected ?? LOCAL_VALUE}
            onValueChange={(value: string) => {
              const next = value === LOCAL_VALUE ? null : value
              if (next !== data.profile) choose.mutate(next)
            }}
          >
            <DropdownMenuRadioItem value={LOCAL_VALUE}>
              <Laptop className="lucide-inline shrink-0" aria-hidden />
              <span className="truncate">{t('sessionEnvironments.picker.thisMachine')}</span>
            </DropdownMenuRadioItem>
            <DropdownMenuSeparator />
            {data.profiles.map((p) => (
              <DropdownMenuRadioItem key={p.id} value={p.id} title={p.workdir}>
                <ProviderGlyph provider={p.provider} size={14} />
                <span className="flex flex-col min-w-0 flex-1">
                  <span className="truncate">{p.name}</span>
                  <span className="truncate text-[12px] text-muted font-mono">{p.workdir}</span>
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
          <p className="px-3 py-1.5 text-[11px] text-muted leading-snug">
            {t('sessionEnvironments.picker.appliesNext')}
          </p>
        </DropdownMenuContent>
      </DropdownMenu>
      {/* Announces each STAGE once. The visible label ticks every second, so
          it must not be the live region itself. */}
      {announce}
    </div>
  )
}
