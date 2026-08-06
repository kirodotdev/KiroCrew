// Meetings settings: which provider files tasks, where the calendar comes from,
// the agent roster, and the speech-correction dictionary.
//
// The two provider pickers are populated from the BACKEND's registries, not a
// hardcoded list — that is what lets an out-of-repo edition add its own provider
// and have it appear here with no frontend change.

import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, CalendarClock, ListChecks, Plus, Trash2 } from 'lucide-react'

import { i18nT } from '../../i18n/t'
import SimpleSelect from '../../components/SimpleSelect'
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  EmptyState,
  Input,
  PageHeader,
  SendBtn,
  Toggle,
} from '../../components/ui'
import {
  meetingsApi,
  WIDGET_TYPE_LABEL_KEY,
  type AgentDef,
  type ConfigResponse,
  type MeetingsConfig,
} from './api'

interface Props {
  onBack: () => void
  notify: (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void
}

export default function SettingsView({ onBack, notify }: Props) {
  const queryClient = useQueryClient()
  const configQuery = useQuery({ queryKey: ['meetings', 'config'], queryFn: meetingsApi.config })
  const dictionaryQuery = useQuery({
    queryKey: ['meetings', 'dictionary'],
    queryFn: meetingsApi.dictionary,
  })

  const aliasRef = useRef<HTMLInputElement>(null)
  const correctRef = useRef<HTMLInputElement>(null)
  const [sourceDraft, setSourceDraft] = useState<string | null>(null)

  const config = configQuery.data?.config
  const calendarProviders = configQuery.data?.calendar_providers ?? []
  const taskProviders = configQuery.data?.task_providers ?? []
  const terms = dictionaryQuery.data?.terms ?? []

  const saveConfig = useMutation({
    mutationFn: (patch: Partial<MeetingsConfig>) => meetingsApi.saveConfig(patch),
    onSuccess: response => {
      queryClient.setQueryData<ConfigResponse>(['meetings', 'config'], previous =>
        previous ? { ...previous, config: response.config } : previous,
      )
      notify(i18nT('apps.meetings.settings.saved'), { type: 'success' })
    },
    onError: () => notify(i18nT('apps.meetings.settings.saveFailed'), { type: 'error' }),
  })

  const addTerm = useMutation({
    mutationFn: (vars: { correct: string; aliases: string[] }) =>
      meetingsApi.addTerm(vars.correct, vars.aliases),
    onSuccess: response => {
      queryClient.setQueryData(['meetings', 'dictionary'], { terms: response.terms })
      if (aliasRef.current) aliasRef.current.value = ''
      if (correctRef.current) correctRef.current.value = ''
    },
    onError: () => notify(i18nT('apps.meetings.settings.termFailed'), { type: 'error' }),
  })

  const removeTerm = useMutation({
    mutationFn: (correct: string) => meetingsApi.removeTerm(correct),
    onSuccess: response =>
      queryClient.setQueryData(['meetings', 'dictionary'], { terms: response.terms }),
  })

  /** Persist the whole config with one field replaced.
   *
   *  The backend's PUT is a full, validated replace, so a patch must carry the
   *  current values or an unrelated setting would silently reset.
   *
   *  Two things make that safe under rapid changes, and both are load-bearing:
   *
   *  1. The base is read from the CACHE at send time, not from the render-time
   *     `config` snapshot. `saveConfig.onSuccess` writes the server's response back
   *     into the cache, so the second of two saves builds on the first's result
   *     instead of on what was on screen when the component last rendered.
   *  2. Saves are CHAINED. Even reading the cache is not enough on its own: with
   *     two requests in flight, the first has not landed in the cache when the
   *     second is built, so both would send a base missing the other's change and
   *     the later response would revert it. Awaiting the previous save first means
   *     each payload is derived from a config the server has already accepted.
   */
  const savesRef = useRef<Promise<unknown>>(Promise.resolve())
  const patch = (
    changes:
      | Partial<MeetingsConfig>
      | ((latest: MeetingsConfig) => Partial<MeetingsConfig>),
  ) => {
    if (!config) return
    savesRef.current = savesRef.current
      .catch(() => undefined) // a failed save must not wedge the chain
      .then(() => {
        const latest =
          queryClient.getQueryData<ConfigResponse>(['meetings', 'config'])?.config ?? config
        // A FUNCTION is resolved here, against `latest`. A plain object is merged
        // shallowly, which is correct for a scalar field but NOT for one derived
        // from the config — `meeting_agents` is rebuilt by mapping the existing
        // array, and computing that at call time captured the stale snapshot, so two
        // rapid toggles each queued an array missing the other's change.
        const resolved = typeof changes === 'function' ? changes(latest) : changes
        return saveConfig.mutateAsync({ ...latest, ...resolved })
      })
    // Swallow at the tail: `mutateAsync` rejects on failure, and `onError` already
    // notifies. An unhandled rejection here would surface as a console error for a
    // case the UI has already reported.
    void savesRef.current.catch(() => undefined)
  }

  const updateAgent = (agentId: string, changes: Partial<AgentDef>) => {
    if (!config) return
    // Function form: the array is mapped from the config the previous save landed,
    // not from the render-time snapshot. Two rapid toggles of DIFFERENT agents both
    // survive; computing the array here would have queued two arrays each missing
    // the other's change.
    patch(latest => ({
      meeting_agents: latest.meeting_agents.map(agent =>
        agent.id === agentId ? { ...agent, ...changes } : agent,
      ),
    }))
  }

  const activeCalendar = calendarProviders.find(row => row.id === config?.calendar.provider)
  const source = sourceDraft ?? config?.calendar.source ?? ''

  const submitTerm = () => {
    const correct = correctRef.current?.value.trim() ?? ''
    const aliases = (aliasRef.current?.value ?? '')
      .split(',')
      .map(alias => alias.trim())
      .filter(Boolean)
    if (!correct || aliases.length === 0) {
      notify(i18nT('apps.meetings.settings.termIncomplete'), { type: 'error' })
      return
    }
    addTerm.mutate({ correct, aliases })
  }

  return (
    <>
      <PageHeader
        title={i18nT('apps.meetings.settings.title')}
        subtitle={i18nT('apps.meetings.settings.subtitle')}
        actions={
          <Btn onClick={onBack}>
            <ArrowLeft className="lucide-inline" />
            {i18nT('apps.meetings.settings.back')}
          </Btn>
        }
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <Card>
          <CardTitle>
            <ListChecks className="lucide-inline" />
            {i18nT('apps.meetings.settings.taskProviderTitle')}
          </CardTitle>
          <p className="text-[13px] text-muted mb-3">
            {i18nT('apps.meetings.settings.taskProviderHelp')}
          </p>
          <SimpleSelect
            options={taskProviders.map(row => row.id)}
            optionLabels={taskProviders.map(row => row.label)}
            value={config?.task_provider ?? ''}
            aria-label={i18nT('apps.meetings.settings.taskProviderTitle')}
            onChange={value => patch({ task_provider: value })}
            style={{ maxWidth: 280 }}
          />
        </Card>

        <Card>
          <CardTitle>
            <CalendarClock className="lucide-inline" />
            {i18nT('apps.meetings.settings.calendarTitle')}
          </CardTitle>
          <p className="text-[13px] text-muted mb-3">
            {i18nT('apps.meetings.settings.calendarHelp')}
          </p>
          <div className="flex items-center gap-2 flex-wrap">
            <SimpleSelect
              options={calendarProviders.map(row => row.id)}
              optionLabels={calendarProviders.map(row => row.label)}
              value={config?.calendar.provider ?? ''}
              aria-label={i18nT('apps.meetings.settings.calendarProviderLabel')}
              onChange={value =>
                patch(latest => ({
                  // Function form for the same reason as `updateAgent`: `calendar` is
                  // a nested object, so the field we are NOT changing has to come
                  // from the latest config rather than the render snapshot.
                  calendar: { ...latest.calendar, provider: value },
                }))
              }
              style={{ minWidth: 180 }}
            />
            {activeCalendar?.requires_source && (
              <>
                <Input
                  value={source}
                  className="flex-1 min-w-[280px]"
                  placeholder={i18nT('apps.meetings.settings.calendarSourcePlaceholder')}
                  aria-label={i18nT('apps.meetings.settings.calendarSourceLabel')}
                  onChange={e => setSourceDraft(e.target.value)}
                />
                <SendBtn
                  onClick={() => {
                    patch(latest => ({
                      calendar: { ...latest.calendar, source: source.trim() },
                    }))
                    setSourceDraft(null)
                  }}
                  aria-label={i18nT('apps.meetings.settings.saveSource')}
                >
                  {i18nT('apps.meetings.settings.saveSource')}
                </SendBtn>
              </>
            )}
          </div>
          {activeCalendar?.requires_source && (
            <p className="text-[12px] text-muted mt-2">
              {i18nT('apps.meetings.settings.calendarSourceHint')}
            </p>
          )}
        </Card>

        <Card>
          <CardTitle>{i18nT('apps.meetings.settings.agentsTitle')}</CardTitle>
          <p className="text-[13px] text-muted mb-3">
            {i18nT('apps.meetings.settings.agentsHelp')}
          </p>
          <div className="flex flex-col gap-2">
            {(config?.meeting_agents ?? []).map(agent => (
              <div
                key={agent.id}
                className="flex items-center gap-3 px-3 py-2.5 border border-border rounded-md"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-text font-medium truncate">{agent.name}</div>
                  <div className="text-[12px] text-muted font-mono truncate">{agent.id}</div>
                </div>
                <Badge variant="muted">
                  {i18nT(WIDGET_TYPE_LABEL_KEY[agent.widget_type])}
                </Badge>
                <Toggle
                  checked={agent.enabled_by_default !== false}
                  onChange={value => updateAgent(agent.id, { enabled_by_default: value })}
                  label={i18nT('apps.meetings.settings.enabledByDefault', { name: agent.name })}
                />
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <CardTitle>{i18nT('apps.meetings.settings.dictionaryTitle')}</CardTitle>
          <p className="text-[13px] text-muted mb-3">
            {i18nT('apps.meetings.settings.dictionaryHelp')}
          </p>
          {terms.length === 0 ? (
            <EmptyState
              icon={<ArrowRight className="lucide-inline" />}
              title={i18nT('apps.meetings.settings.noTerms')}
              subtitle={i18nT('apps.meetings.settings.noTermsHint')}
            />
          ) : (
            <div className="flex flex-col gap-1 mb-3">
              {terms.map(term => (
                <div
                  key={term.correct}
                  className="flex items-center justify-between gap-2 px-3 py-1.5 rounded-md bg-bg-hover"
                >
                  <div className="min-w-0 text-[13px]">
                    <span className="text-muted">{term.aliases.join(', ')}</span>
                    <ArrowRight className="lucide-inline mx-2 text-muted" />
                    <span className="text-text font-medium">{term.correct}</span>
                  </div>
                  <Btn
                    danger
                    onClick={() => removeTerm.mutate(term.correct)}
                    aria-label={i18nT('apps.meetings.settings.removeTerm', {
                      term: term.correct,
                    })}
                  >
                    <Trash2 className="lucide-inline" />
                  </Btn>
                </div>
              ))}
            </div>
          )}
          <div className="flex items-center gap-2 flex-wrap">
            <Input
              ref={aliasRef}
              className="flex-1 min-w-[200px]"
              placeholder={i18nT('apps.meetings.settings.aliasesPlaceholder')}
              aria-label={i18nT('apps.meetings.settings.aliasesLabel')}
            />
            <ArrowRight className="lucide-inline text-muted" />
            <Input
              ref={correctRef}
              className="w-48"
              placeholder={i18nT('apps.meetings.settings.correctPlaceholder')}
              aria-label={i18nT('apps.meetings.settings.correctLabel')}
              onKeyDown={e => {
                if (e.key === 'Enter') submitTerm()
              }}
            />
            <SendBtn onClick={submitTerm} aria-label={i18nT('apps.meetings.settings.addTerm')}>
              <Plus className="lucide-inline" />
              {i18nT('apps.meetings.settings.addTerm')}
            </SendBtn>
          </div>
        </Card>
      </div>
    </>
  )
}
