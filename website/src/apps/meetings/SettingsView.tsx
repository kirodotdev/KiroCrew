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
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  EmptyState,
  Input,
  PageHeader,
  Select,
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
   *  The backend's PUT is a full, validated replace, so a patch must carry the
   *  current values or an unrelated setting would silently reset. */
  const patch = (changes: Partial<MeetingsConfig>) => {
    if (!config) return
    saveConfig.mutate({ ...config, ...changes })
  }

  const updateAgent = (agentId: string, changes: Partial<AgentDef>) => {
    if (!config) return
    patch({
      meeting_agents: config.meeting_agents.map(agent =>
        agent.id === agentId ? { ...agent, ...changes } : agent,
      ),
    })
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
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0 flex flex-col gap-4">
        <Card>
          <CardTitle>
            <ListChecks className="lucide-inline" />
            {i18nT('apps.meetings.settings.taskProviderTitle')}
          </CardTitle>
          <p className="text-[13px] text-muted mb-3">
            {i18nT('apps.meetings.settings.taskProviderHelp')}
          </p>
          <Select
            value={config?.task_provider ?? ''}
            aria-label={i18nT('apps.meetings.settings.taskProviderTitle')}
            onChange={e => patch({ task_provider: e.target.value })}
          >
            {taskProviders.map(row => (
              <option key={row.id} value={row.id}>
                {row.label}
              </option>
            ))}
          </Select>
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
            <Select
              value={config?.calendar.provider ?? ''}
              aria-label={i18nT('apps.meetings.settings.calendarProviderLabel')}
              onChange={e =>
                patch({ calendar: { provider: e.target.value, source: source } })
              }
            >
              {calendarProviders.map(row => (
                <option key={row.id} value={row.id}>
                  {row.label}
                </option>
              ))}
            </Select>
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
                    patch({
                      calendar: { provider: config?.calendar.provider ?? '', source: source.trim() },
                    })
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
