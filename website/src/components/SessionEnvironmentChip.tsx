import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import EnvironmentHealthCard from './EnvironmentHealthCard'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import { useTranslation } from 'react-i18next'
import { Check, Cloud, Laptop, Loader2, Lock } from 'lucide-react'
import { CoderIcon } from './BrandIcon'
import { api, type SlotEnvironment } from '../api/client'
import { ApiError, apiErrorCode } from '../api/apiError'
import ErrorNotice from './ErrorNotice'
import { SLOT_ENVIRONMENT_RETRY, START_POLL_MS, slotEnvironmentQueryKey, useEnvironmentStartLabel } from './sessionEnvironmentHooks'
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

/** Hover-to-open delays: long enough that crossing the shelf does not flash
 *  the card, short enough to feel like a tooltip. */
const HOVER_OPEN_MS = 250
const HOVER_CLOSE_MS = 200

/** The `reason` a `choice_refused` body names (e.g. `private_chat`), or `''`. */
const refusalReason = (error: unknown): string => {
  if (!(error instanceof ApiError)) return ''
  try {
    const reason = (JSON.parse(error.body) as { reason?: unknown } | null)?.reason
    return typeof reason === 'string' ? reason : ''
  } catch {
    return ''
  }
}

function LockedRemoteChip({ slot, name, title, children }: {
  slot: string; name: string; title: string; children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // A hover preview must not move focus (the owner may be typing in the
  // composer); a click or keyboard open moves it into the card so Tab reaches
  // the card's own controls.
  const viaHover = useRef(false)
  const later = (next: boolean, ms: number) => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(next), ms)
  }
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="composer-environment-chip"
          title={title}
          aria-label={title}
          aria-haspopup="dialog"
          onMouseEnter={() => {
            if (!open) viaHover.current = true
            later(true, HOVER_OPEN_MS)
          }}
          onMouseLeave={() => later(false, HOVER_CLOSE_MS)}
          onClick={(e) => {
            // A click shows the card; with hover having opened it already, the
            // trigger's toggle would close it under the pointer, so keep it open.
            if (timer.current) clearTimeout(timer.current)
            viaHover.current = false
            if (open) e.preventDefault()
          }}
          className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer text-muted"
        >
          {children}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        className="w-auto"
        onMouseEnter={() => later(true, 0)}
        onMouseLeave={() => later(false, HOVER_CLOSE_MS)}
        onOpenAutoFocus={(e: Event) => { if (viaHover.current) e.preventDefault() }}
        onCloseAutoFocus={(e: Event) => {
          if (viaHover.current) e.preventDefault()
          viaHover.current = false
        }}
      >
        <EnvironmentHealthCard slot={slot} name={name} />
      </PopoverContent>
    </Popover>
  )
}


interface Props {
  slot: string
  /** A running turn keeps its environment: switching applies at the next start. */
  disabled?: boolean
  /** Narrow shelf: icon only, the name stays in the tooltip. */
  compact?: boolean
}

/** Composer chip choosing where a session's files and commands live: this
 *  machine or a Session Environments profile. Renders nothing until profiles
 *  exist (or on an app caller's 403). */
/** The provider's decorative brand mark; an unknown provider keeps the generic cloud. */
function ProviderGlyph({ provider, size }: { provider?: string; size: number }) {
  if (provider !== 'coder') return <Cloud size={size} className="shrink-0 opacity-80" aria-hidden />
  return (
    <span data-testid={`provider-mark-${provider}`} className="inline-flex shrink-0 items-center" aria-hidden="true">
      <CoderIcon size={Math.round(size * 0.8)} />
    </span>
  )
}

export default function SessionEnvironmentChip(props: Props) {
  // One instance per chat: a switch pending in one chat must never label, or
  // land its result in, the next chat the composer shows.
  return <SlotEnvironmentChip key={props.slot} {...props} />
}

function SlotEnvironmentChip({ slot, disabled, compact }: Props) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const key = slotEnvironmentQueryKey(slot)
  const { data, error, refetch } = useQuery({
    queryKey: key,
    queryFn: () => api.chatSlotEnvironment(slot),
    staleTime: 30_000,
    ...SLOT_ENVIRONMENT_RETRY,
    // Poll the start stage only while a turn runs on a remote environment.
    refetchInterval: (query) => (disabled && query.state.data?.profile ? START_POLL_MS : false),
  })
  // The first send locks the choice: re-read when that turn ends.
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !disabled) void refetch()
    wasRunning.current = !!disabled
  }, [disabled, refetch])
  // The turn indicator's start (one query), including the pre-stage gap.
  const start = useEnvironmentStartLabel(slot, !!disabled)
  const choose = useMutation({
    mutationFn: (profile: string | null) => api.setChatSlotEnvironment(slot, profile),
    onSuccess: async (result) => {
      // A placement read started before this switch (opening the menu refetches)
      // would land the old choice over this one: cancel it, publish, re-read.
      await queryClient.cancelQueries({ queryKey: key })
      queryClient.setQueryData<SlotEnvironment>(key, (old) =>
        old ? { ...old, profile: result.profile, provider: result.profile ? old.profiles.find(p => p.id === result.profile)?.provider : null } : old,
      )
      void queryClient.invalidateQueries({ queryKey: key })
    },
  })
  // A failed switch stays beside the chip until the next pick or a dismiss, and
  // names both ends: where it was going and where the chat still runs.
  // No hand-off: the chat composer beside this chip may hold an unsent draft.
  const nameOf = (id: string | null | undefined) => (id === null || id === undefined
    ? t('sessionEnvironments.picker.thisMachine')
    : data?.profiles?.find(p => p.id === id)?.name ?? id)
  // A known refusal in words that say what to do; anything else as the gateway said it.
  const switchReason = (error: unknown): string => {
    const code = error instanceof ApiError ? apiErrorCode(error) : ''
    if (code === 'choices_unreadable') return t('sessionEnvironments.picker.choicesUnreadable')
    if (code === 'unknown_profile') return t('sessionEnvironments.picker.unknownProfile')
    if (code === 'environment_locked') return t('sessionEnvironments.picker.environmentLocked')
    if (code === 'choice_refused' && refusalReason(error) === 'private_chat') return t('sessionEnvironments.picker.privateChat')
    return agentSwitchFailureMessage(error)
  }
  const chooseError = choose.isError
    ? <ErrorNotice variant="inline" className="ml-2 min-w-0 max-w-[280px]" message={t('sessionEnvironments.picker.switchFailed', {
      target: nameOf(choose.variables), current: nameOf(data?.profile), reason: switchReason(choose.error),
    })} onDismiss={() => choose.reset()} />
    : null

  if (error) {
    if (error instanceof ApiError && error.status === 403 && apiErrorCode(error) === 'owner_only') return null
    return (
      // No hand-off: the chat composer beside this chip may hold an unsent draft.
      <ErrorNotice variant="inline" message={error.message || t('sessionEnvironments.loadError')} />
    )
  }
  // Hidden only with nothing to pick and nothing bound. A chat still bound to a
  // removed profile keeps the chip, so "This machine" stays reachable.
  if (!data || !Array.isArray(data.profiles) || (data.profiles.length === 0 && data.profile == null)) return null

  const pending = choose.isPending ? choose.variables : undefined
  const selected = pending !== undefined ? pending : data.profile
  const profile = data.profiles.find((p) => p.id === selected)
  // A removed profile still binds (fails closed at start): show its id.
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
  const mark = (v: string) => (v === (selected ?? LOCAL_VALUE)
    ? <Check size={13} className="ml-auto shrink-0 text-accent" aria-hidden /> : null)
  // While starting, the stage and elapsed time replace the name.
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

  // Which locked status follows where the chat is BOUND, not a pick still in
  // flight: a switch the lock overtook never ran, so it gets no health card.
  // A Kiro Cloud sandbox has no machine metrics Kiro Crew can read (only its own
  // agent reaches it), so its fixed chip is the plain status, with no health card.
  const bound = data.profiles.find((p) => p.id === data.profile)
  const cloudSandbox = (data.provider ?? bound?.provider) === 'kiro_cloud'
  if (locked && (data.profile == null || cloudSandbox)) {
    // A muted status with a lock, so it does not read as the picker.
    return (
      <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
        <span
          data-testid="composer-environment-chip"
          title={title}
          aria-label={title}
          className="inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 text-muted"
        >
          {content}
          <Lock size={11} className="shrink-0 opacity-70" aria-hidden />
          {/* Said in words, not by the padlock alone; the full sentence is the name. */}
          {!compact && <span aria-hidden className="text-[11px]">{t('sessionEnvironments.picker.fixedLabel')}</span>}
        </span>
        {/* A switch refused because the chat locked meanwhile still says why. */}
        {chooseError}
        {announce}
      </div>
    )
  }
  if (locked) {
    // A fixed REMOTE environment: muted with a lock like the local status, so it
    // does not read as the picker; hover (or focus and click) shows its live
    // state and machine metrics.
    return (
      <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
        <LockedRemoteChip slot={slot} name={name} title={title}>
          {content}
          <Lock size={11} className="shrink-0 opacity-70" aria-hidden />
          {!compact && <span aria-hidden className="text-[11px]">{t('sessionEnvironments.picker.fixedLabel')}</span>}
        </LockedRemoteChip>
        {chooseError}
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
            // A pending switch decides the placement; a second pick would race it.
            disabled={disabled || choose.isPending}
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
              if (choose.isPending) return
              const next = value === LOCAL_VALUE ? null : value
              if (next !== data.profile) choose.mutate(next)
            }}
          >
            <DropdownMenuRadioItem value={LOCAL_VALUE} disabled={choose.isPending}>
              <Laptop className="lucide-inline shrink-0" aria-hidden />
              <span className="truncate">{t('sessionEnvironments.picker.thisMachine')}</span>
              {mark(LOCAL_VALUE)}
            </DropdownMenuRadioItem>
            <DropdownMenuSeparator />
            {data.profiles.map((p) => (
              <DropdownMenuRadioItem key={p.id} value={p.id} title={p.workdir} disabled={choose.isPending}>
                <ProviderGlyph provider={p.provider} size={14} />
                <span className="flex flex-col min-w-0 flex-1">
                  <span className="truncate">{p.name}</span>
                  {/* Profiles often share a folder; the template tells them apart. A Kiro
                      Cloud profile has no directory Kiro Crew can see. */}
                  <span className="truncate text-[12px] text-muted font-mono">
                    {p.template || p.workdir || p.repositories?.join(', ') || t('sessionEnvironments.kiroCloud.emptyWorkspace')}
                  </span>
                </span>
                {mark(p.id)}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
          <p className="px-3 py-1.5 text-[11px] text-muted leading-snug">
            {t('sessionEnvironments.picker.appliesNext')}
          </p>
        </DropdownMenuContent>
      </DropdownMenu>
      {chooseError}
      {/* Announces each STAGE once. The visible label ticks every second, so
          it must not be the live region itself. */}
      {announce}
    </div>
  )
}
