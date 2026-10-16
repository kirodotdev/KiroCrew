import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, ChevronRight, ExternalLink, Minus, Plus, Trash2, X } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import type { ReactNode } from 'react'

import { api, type SessionEnvironmentsCheck } from '../../api/client'
import { fmtDateTime } from '../../i18n/format'
import { safeHttpUrl } from '../../lib/safeUrl'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { CoderIcon, FargateIcon } from '../../components/BrandIcon'
import { SettingsField, SettingsInput, SettingsSelect, SettingsStepper, SettingsToggle } from '../../components/settings'
import { Btn, PanelSectionHeader } from '../../components/ui'

interface EnvironmentProfile {
  id: string
  name: string
  template: string
  workdir: string
  /** False for a template whose stop destroys its files. Absent means true:
   * the card writes it only once toggled, so an older gateway never sees it. */
  compute_survives_stop?: boolean
}
/** How often the card re-reads the workspace list. */
const WORKSPACES_POLL_MS = 15_000
type ProfileTextKey = 'id' | 'name' | 'template' | 'workdir'
/** Mirrors validate_fargate_profile in config/session_environments.py. */
interface FargateProfile {
  id: string
  name: string
  aws_profile: string
  region: string
  cluster: string
  task_definition: string
  subnets: string[]
  security_groups: string[]
  assign_public_ip: boolean
  workdir: string
}
type FargateTextKey = 'id' | 'name' | 'aws_profile' | 'region' | 'cluster' | 'task_definition' | 'workdir'
type FargateListKey = 'subnets' | 'security_groups'
/** Mirrors MAX_PROFILES in config/session_environments.py. */
const MAX_PROFILES = 20
interface EnvironmentSettings {
  coder_url: string
  profiles: EnvironmentProfile[]
  /** AWS Fargate profiles. Older gateways omit it, and then only Coder is offered. */
  fargate_profiles?: FargateProfile[]
  /** Profile id new chats start on; "" is this machine. Older gateways omit it. */
  default_profile?: string
  /** Days an archived session's workspace is kept. Older gateways omit it. */
  archived_retention_days?: number
  /** Stop every remote environment when the gateway shuts down. Older gateways omit it. */
  stop_on_shutdown?: boolean
}
/** A removed profile cannot stay the default (the gateway refuses that save),
 * so removing the default falls back to this machine. */
function withoutDefault(next: EnvironmentSettings, removedId: string): EnvironmentSettings {
  return next.default_profile === removedId ? { ...next, default_profile: '' } : next
}
/** Mirrors ARCHIVED_RETENTION_DAYS_MAX in config/session_environments.py. */
const ARCHIVED_RETENTION_DAYS_MAX = 7
interface ConfigResponse {
  session_environments?: EnvironmentSettings
}

type ProviderId = 'coder' | 'fargate'
const SETUP_DOCS: Record<ProviderId, string> = {
  coder: 'https://coder.com/docs/admin/setup',
  fargate: 'https://docs.aws.amazon.com/AmazonECS/latest/developerguide/getting-started-fargate.html',
}

/** One saved profile as a disclosure. The card stays one element: its header
 * keeps the profile's name in place and only the field body expands below it.
 * The body unmounts when collapsed, which loses nothing because every value
 * lives in the parent draft. */
function ProfileDisclosure({ title, subtitle, open, onToggle, children }: {
  title: string
  subtitle?: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const bodyId = useId()
  return (
    <div className="min-w-0 rounded-lg border border-border">
      <Clickable onClick={onToggle} aria-expanded={open} aria-controls={bodyId}
        className="group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 cursor-pointer hover:bg-bg-hover/40 transition-colors">
        <motion.span className="flex shrink-0 items-center text-muted group-hover:text-text"
          animate={{ rotate: open ? 90 : 0 }} transition={{ type: 'spring', damping: 22, stiffness: 300 }}>
          <ChevronRight className="lucide-inline" aria-hidden="true" />
        </motion.span>
        <span className="truncate text-[13px] font-semibold text-text">{title}</span>
        {subtitle && <span className="truncate text-[12px] text-muted">{subtitle}</span>}
      </Clickable>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div key="body" id={bodyId}
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }} style={{ overflow: 'hidden' }}>
            <fieldset aria-label={title} className="min-w-0 space-y-3 px-3 pb-3">{children}</fieldset>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** One check line: a status glyph and its text, announced as a list item. */
function CheckLine({ status, children }: { status: 'ok' | 'fail' | 'unknown'; children: ReactNode }) {
  const Icon = status === 'ok' ? Check : status === 'fail' ? X : Minus
  const tone = status === 'ok' ? 'text-ok' : status === 'fail' ? 'text-danger' : 'text-muted'
  return (
    <li className="flex min-w-0 items-start gap-2 text-[13px] text-text">
      <Icon size={14} className={`mt-0.5 shrink-0 ${tone}`} aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
    </li>
  )
}

/** Display names for git hosts whose brand is a do-not-translate term; any
 * other host is shown as its host name. */
const GIT_HOST_NAMES: Record<string, string> = {
  'github.com': 'GitHub',
  'gitlab.com': 'GitLab',
  'dev.azure.com': 'Azure DevOps',
}

/** What the readiness check found, in the order a person fixes things.
 *
 * Follows the provider choice, like the Manage step: a Fargate setup never
 * reports Coder rows, even when a Coder URL is still saved. Git rows apply to
 * every provider. */
function CheckResults({ result, provider, profiles, fargateProfiles }: {
  result: SessionEnvironmentsCheck
  provider: ProviderId
  profiles: EnvironmentProfile[]
  fargateProfiles: FargateProfile[]
}) {
  const { t } = useTranslation()
  const names = new Map(profiles.map(p => [p.id, p]))
  const fargateNames = new Map(fargateProfiles.map(p => [p.id, p]))
  return (
    <ul aria-label={t('sessionEnvironments.checkResults')} className="space-y-1.5">
      {provider === 'coder' && (result.coder.ok ? (
        <CheckLine status="ok">{t('sessionEnvironments.checkCoderOk', { owner: result.coder.owner ?? '' })}</CheckLine>
      ) : (
        <li><ErrorNotice variant="inline" message={t('sessionEnvironments.checkCoderFailed', { error: result.coder.error ?? '' })} askAgent /></li>
      ))}
      {provider === 'fargate' && (result.fargate ?? []).map(row => {
        const profile = fargateNames.get(row.id)
        const name = profile?.name || row.id
        return row.ok ? (
          <CheckLine key={`fargate:${row.id}`} status="ok">
            {t('sessionEnvironments.checkTaskDefinitionOk', { name, taskDefinition: profile?.task_definition ?? '' })}
          </CheckLine>
        ) : (
          <li key={`fargate:${row.id}`}><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent /></li>
        )
      })}
      {provider === 'coder' && result.profiles.map(row => {
        const profile = names.get(row.id)
        const name = profile?.name || row.id
        return row.ok ? (
          <CheckLine key={row.id} status="ok">
            {t('sessionEnvironments.checkTemplateOk', { name, template: profile?.template ?? '' })}
          </CheckLine>
        ) : (
          <li key={row.id}><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent /></li>
        )
      })}
      {Object.entries(result.git).map(([host, status]) => {
        const name = GIT_HOST_NAMES[host] ?? host
        return (
          <CheckLine key={host} status={status === 'signed_in' ? 'ok' : status === 'not_signed_in' ? 'fail' : 'unknown'}>
            {status === 'signed_in'
              ? t('sessionEnvironments.checkGitSignedIn', { name })
              : status === 'not_signed_in'
                ? t('sessionEnvironments.checkGitSignedOut', { name })
                : t('sessionEnvironments.checkGitUnchecked', { name })}
          </CheckLine>
        )
      })}
    </ul>
  )
}

/** The workspaces Kiro Crew owns, from the gateway's registry (no provider call). */
function WorkspaceList({ provider }: { provider: ProviderId }) {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ['sessionEnvironmentsWorkspaces'],
    queryFn: () => api.sessionEnvironmentsWorkspaces(),
    refetchInterval: WORKSPACES_POLL_MS,
    retry: false,
  })
  if (query.isPending) return null
  if (query.isError) return <ErrorNotice askAgent message={t('sessionEnvironments.workspacesError')} />
  // A row without a provider comes from an older gateway, which only had Coder.
  const rows = query.data.workspaces.filter(row => (row.provider ?? 'coder') === provider)
  if (!rows.length) return <p className="text-[13px] text-muted">{t('sessionEnvironments.workspacesNone')}</p>
  return (
    <ul aria-label={t('sessionEnvironments.workspaces')} className="divide-y divide-border rounded-lg border border-border">
      {rows.map(row => (
        <li key={row.workspace} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-[13px]">
          <span className="truncate font-mono text-[12px] text-text">{row.workspace}</span>
          <span className="text-muted">
            {row.provider === 'fargate' ? t('sessionEnvironments.workspaceFargateTask') : t('sessionEnvironments.workspaceSession')}
          </span>
          {(row.state === 'deleting' || row.deletion_due_at) && (
            <span className="ml-auto text-[12px] text-muted">
              {row.state === 'deleting'
                ? t('sessionEnvironments.workspaceDeleting')
                : t('sessionEnvironments.workspaceDeletes', { when: fmtDateTime(row.deletion_due_at) })}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

/** Configuration only: neither the flag nor a save enables remote execution.
 * The preview switch is indexed in FeaturePreviewsSection. The URL setting is
 * searchable only after opting in; profile rows are per-record CRUD fields.
 */
export function SessionEnvironmentsCard({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const controlId = useId()
  const query = useQuery<ConfigResponse>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled,
  })
  // Null follows fresh server data. Once edited, background refetches never
  // replace the draft. Saving freezes controls until the server responds.
  const [draft, setDraft] = useState<EnvironmentSettings | null>(null)
  const [saved, setSaved] = useState(false)
  // Null until the person picks a provider; until then the choice follows
  // what is saved (see `provider` below), so a Fargate-only setup opens on it.
  const [providerChoice, setProvider] = useState<ProviderId | null>(null)
  // Saved profiles start collapsed; a newly added one opens so it can be
  // filled in. Tracked by row index per provider, so add/remove keep it aligned.
  const [openRows, setOpenRows] = useState<Record<ProviderId, boolean[]>>({ coder: [], fargate: [] })
  const toggleRow = (kind: ProviderId, index: number) => setOpenRows(rows => {
    const next = [...rows[kind]]
    next[index] = !next[index]
    return { ...rows, [kind]: next }
  })
  const addRow = (kind: ProviderId, index: number) => setOpenRows(rows => {
    const next = [...rows[kind]]
    next[index] = true
    return { ...rows, [kind]: next }
  })
  const removeRow = (kind: ProviderId, index: number) => setOpenRows(rows => ({
    ...rows,
    [kind]: rows[kind].filter((_, i) => i !== index),
  }))
  const mutation = useMutation({
    mutationFn: (value: EnvironmentSettings) => api.patchConfig('session_environments', value) as Promise<ConfigResponse>,
    onMutate: () => qc.cancelQueries({ queryKey: ['kirocrewConfig'] }),
    onSuccess: response => {
      qc.setQueryData(['kirocrewConfig'], response)
      setDraft(null)
      setSaved(true)
    },
  })
  const config = draft ?? query.data?.session_environments
  // The check reads the SAVED configuration on the gateway, so it is offered
  // only with no unsaved draft, and its answer is dropped when one appears.
  const savedCoderUrl = safeHttpUrl(query.data?.session_environments?.coder_url ?? '')
  const savedProfiles = query.data?.session_environments?.profiles ?? []
  const savedFargateProfiles = query.data?.session_environments?.fargate_profiles ?? []
  // A Fargate-only setup is checkable and lists its tasks without a Coder URL.
  const anythingSaved = !!savedCoderUrl || savedFargateProfiles.length > 0
  // The providers are separate choices, so open on the one in use. Coder wins
  // a tie because it is the only one with a gateway-level URL.
  const provider: ProviderId = providerChoice
    ?? (savedCoderUrl || savedProfiles.length > 0 ? 'coder' : savedFargateProfiles.length > 0 ? 'fargate' : 'coder')
  const checkMutation = useMutation({ mutationFn: () => api.sessionEnvironmentsCheck() })
  const change = (next: EnvironmentSettings) => {
    setDraft(next)
    setSaved(false)
    mutation.reset()
  }

  if (!enabled && !draft) return null
  if (query.isPending) return <p role="status" className="text-[13px] text-muted">{t('sessionEnvironments.loading')}</p>
  if (query.isError && !query.data) return (
    <ErrorNotice message={t('sessionEnvironments.loadError')} askAgent footer={
      <Btn onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
    } />
  )
  // Older gateways do not expose the section. Do not let an apparently empty
  // form pretend this backend can save it, or fabricate a successful connection.
  if (!config) return <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />

  const profileFields: { key: ProfileTextKey; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'template', label: t('sessionEnvironments.template') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  // Per-profile like the fields above, so not a searchable settings row.
  const survivesStopLabel = t('sessionEnvironments.computeSurvivesStop')
  const updateProfile = (index: number, field: ProfileTextKey, value: string) => {
    change({ ...config, profiles: config.profiles.map((profile, i) => i === index ? { ...profile, [field]: value } : profile) })
  }
  const addProfile = () => {
    let n = 1
    while (config.profiles.some(profile => profile.id === `profile-${n}`)) n++
    addRow('coder', config.profiles.length)
    change({ ...config, profiles: [...config.profiles, { id: `profile-${n}`, name: '', template: '', workdir: '' }] })
  }
  const fargateProfiles = config.fargate_profiles ?? []
  const fargateFields: { key: FargateTextKey; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'aws_profile', label: t('sessionEnvironments.awsProfile') },
    { key: 'region', label: t('sessionEnvironments.region') },
    { key: 'cluster', label: t('sessionEnvironments.cluster') },
    { key: 'task_definition', label: t('sessionEnvironments.taskDefinition') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const fargateListFields: { key: FargateListKey; label: string }[] = [
    { key: 'subnets', label: t('sessionEnvironments.subnets') },
    { key: 'security_groups', label: t('sessionEnvironments.securityGroups') },
  ]
  // A per-profile control: a variable label keeps it out of the global
  // settings index, which cannot deep-link to one configured row.
  const publicIpLabel = t('sessionEnvironments.publicIp')
  const updateFargate = (index: number, updates: Partial<FargateProfile>) => {
    change({ ...config, fargate_profiles: fargateProfiles.map((profile, i) => i === index ? { ...profile, ...updates } : profile) })
  }
  const addFargateProfile = () => {
    let n = 1
    while (fargateProfiles.some(profile => profile.id === `fargate-${n}`)) n++
    addRow('fargate', fargateProfiles.length)
    // Private networking by default: a public IP is an explicit choice.
    change({ ...config, fargate_profiles: [...fargateProfiles, {
      id: `fargate-${n}`, name: '', aws_profile: '', region: '', cluster: '', task_definition: '',
      subnets: [], security_groups: [], assign_public_ip: false, workdir: '/workspace',
    }] })
  }

  return (
    <form className="min-w-0 space-y-3 pt-3" onSubmit={event => {
      event.preventDefault()
      if (draft && !mutation.isPending) mutation.mutate(draft)
    }}>
      {/* No hand-off: navigating would discard the unsaved environment draft. */}
      <ErrorNotice message={query.isError ? t('sessionEnvironments.loadError') : null} footer={
        <Btn type="button" onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
      } />
      {/* Global: these apply to every provider's profiles, so they sit above
        * the provider choice. Older gateways omit either key, and then its
        * control stays hidden. */}
      {(config.default_profile !== undefined || config.archived_retention_days !== undefined || config.stop_on_shutdown !== undefined) && (() => {
        const all = [...config.profiles, ...fargateProfiles].filter(p => p.id)
        const defaultOn = !!config.default_profile
        return (
          <section aria-label={t('sessionEnvironments.globalDefaults')} className="space-y-3">
            <PanelSectionHeader label={t('sessionEnvironments.globalDefaults')} />
            <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
              {config.default_profile !== undefined && <>
                <SettingsToggle configKey="session_environments.default_profile"
                  label={t('sessionEnvironments.defaultEnabled')}
                  description={all.length ? t('sessionEnvironments.defaultEnabledHint') : t('sessionEnvironments.defaultEnabledNoProfiles')}
                  checked={defaultOn} disabled={!all.length && !defaultOn}
                  onChange={on => change({ ...config, default_profile: on ? all[0]?.id ?? '' : '' })} />
                {defaultOn && (
                  <SettingsSelect label={t('sessionEnvironments.defaultProfile')}
                    description={t('sessionEnvironments.defaultProfileHint')}
                    value={config.default_profile ?? ''}
                    options={all.map(p => p.id)}
                    optionLabels={all.map(p => p.name || p.id)}
                    onChange={value => change({ ...config, default_profile: value })} />
                )}
              </>}
              {config.archived_retention_days !== undefined && (
                <SettingsStepper configKey="session_environments.archived_retention_days"
                  label={t('sessionEnvironments.archivedRetention')}
                  description={t('sessionEnvironments.archivedRetentionHint')}
                  value={config.archived_retention_days}
                  onDecrement={() => change({ ...config, archived_retention_days: Math.max(0, (config.archived_retention_days ?? 0) - 1) })}
                  onIncrement={() => change({ ...config, archived_retention_days: Math.min(ARCHIVED_RETENTION_DAYS_MAX, (config.archived_retention_days ?? 0) + 1) })} />
              )}
              {config.stop_on_shutdown !== undefined && <div>
                <SettingsToggle configKey="session_environments.stop_on_shutdown"
                  label={t('sessionEnvironments.stopOnShutdown')}
                  description={t('sessionEnvironments.stopOnShutdownHint')}
                  checked={config.stop_on_shutdown}
                  describedBy={config.fargate_profiles !== undefined ? `${controlId}-shutdown-fargate` : undefined}
                  onChange={on => change({ ...config, stop_on_shutdown: on })} />
                {/* The switch never stops a Fargate task (stopping one deletes
                  * its files); the guest bootstrap's idle exit ends it instead. */}
                {config.fargate_profiles !== undefined && (
                  <p id={`${controlId}-shutdown-fargate`} className="mr-14 text-[12px] text-muted">
                    {t('sessionEnvironments.stopOnShutdownFargate')}
                  </p>
                )}
              </div>}
            </fieldset>
          </section>
        )
      })()}
      {/* An older gateway without Fargate profiles offers Coder alone. */}
      {config.fargate_profiles !== undefined && (
        <fieldset disabled={mutation.isPending} className="min-w-0 pt-3">
          <legend className="mb-2 text-[13px] font-semibold text-text">{t('sessionEnvironments.provider')}</legend>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {([
              { id: 'coder', label: t('sessionEnvironments.coder'), icon: <CoderIcon size={14} /> },
              { id: 'fargate', label: t('sessionEnvironments.fargate'), icon: <FargateIcon size={20} /> },
            ] as const).map(option => (
              <label key={option.id} className={`flex items-center gap-3 rounded-lg border p-3 cursor-pointer text-[13px] ${provider === option.id ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-bg text-text'}`}>
                <input type="radio" name={`${controlId}-provider`} value={option.id} checked={provider === option.id}
                  aria-label={option.label} onChange={() => setProvider(option.id)} className="accent-[var(--accent)]" />
                {option.icon}
                {option.label}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <section aria-label={t('sessionEnvironments.configureStep')} className="space-y-3 pt-3">
      <PanelSectionHeader label={t('sessionEnvironments.configureStep')} />
      <details className="rounded-lg border border-border p-3">
        <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.setup')}</summary>
        <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
          {provider === 'coder' ? <>
            <li>{t('sessionEnvironments.coderSetup1')}</li>
            <li>{t('sessionEnvironments.coderSetup2')}</li>
            <li>{t('sessionEnvironments.coderSetup3')}</li>
          </> : <>
            <li>{t('sessionEnvironments.fargateSetup1')}</li>
            <li>{t('sessionEnvironments.fargateSetup2')}</li>
            <li>{t('sessionEnvironments.fargateSetup3')}</li>
          </>}
        </ol>
        <a className="inline-block pt-3 text-[13px] text-accent underline" target="_blank" rel="noopener noreferrer"
          href={SETUP_DOCS[provider]}>
          {t('sessionEnvironments.setupDocs')}
        </a>
      </details>
      <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
        <div hidden={provider !== 'coder'} className="space-y-3">
        <SettingsInput
          configKey="session_environments.coder_url"
          label={t('sessionEnvironments.url')}
          description={t('sessionEnvironments.urlHint')}
          value={config.coder_url}
          onChange={value => change({ ...config, coder_url: value })}
        />
        <SettingsField configKey="session_environments.profiles" label={t('sessionEnvironments.profiles')}
          description={t('sessionEnvironments.profilesCoderHint')}>
          <div className="min-w-0 space-y-3">
            {config.profiles.map((profile, index) => (
              // Index remains stable while typing the editable id. Removing a
              // row is explicit and every input is controlled from this draft.
              <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.template}
                open={!!openRows.coder[index]} onToggle={() => toggleRow('coder', index)}>
                {profileFields.map(field => (
                  <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateProfile(index, field.key, value)} />
                ))}
                <SettingsToggle anchor={false} label={survivesStopLabel}
                  description={t('sessionEnvironments.computeSurvivesStopHint')}
                  checked={profile.compute_survives_stop !== false}
                  onChange={on => change({ ...config, profiles: config.profiles.map((p, i) => i === index ? { ...p, compute_survives_stop: on } : p) })} />
                <Btn type="button" onClick={() => {
                  removeRow('coder', index)
                  change(withoutDefault({ ...config, profiles: config.profiles.filter((_, i) => i !== index) }, profile.id))
                }}>
                  <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                </Btn>
              </ProfileDisclosure>
            ))}
            <Btn type="button" disabled={config.profiles.length >= MAX_PROFILES} onClick={addProfile}>
              <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
            </Btn>
          </div>
        </SettingsField>
        </div>
        {config.fargate_profiles !== undefined && (
          <div hidden={provider !== 'fargate'} className="space-y-3">
            {/* Deploy my crew hosts a whole headless crew; a session environment
              * keeps state on this gateway. Say so where the two look alike. */}
            <p className="text-[13px] text-muted">{t('sessionEnvironments.fargateVsDeploy')}</p>
            <SettingsField configKey="session_environments.fargate_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesFargateHint')}>
              <div className="min-w-0 space-y-3">
                {fargateProfiles.map((profile, index) => (
                  <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.task_definition}
                    open={!!openRows.fargate[index]} onToggle={() => toggleRow('fargate', index)}>
                    {fargateFields.map(field => (
                      <SettingsInput key={field.key} label={field.label} value={profile[field.key]}
                        onChange={value => updateFargate(index, { [field.key]: value })} />
                    ))}
                    {fargateListFields.map(field => (
                      <SettingsInput key={field.key} label={field.label} value={profile[field.key].join(', ')}
                        onChange={value => updateFargate(index, { [field.key]: value.split(',').map(item => item.trim()).filter(Boolean) })} />
                    ))}
                    <SettingsToggle label={publicIpLabel} description={t('sessionEnvironments.publicIpHint')}
                      checked={profile.assign_public_ip} onChange={value => updateFargate(index, { assign_public_ip: value })} />
                    <Btn type="button" onClick={() => {
                      removeRow('fargate', index)
                      change(withoutDefault({ ...config, fargate_profiles: fargateProfiles.filter((_, i) => i !== index) }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={fargateProfiles.length >= MAX_PROFILES} onClick={addFargateProfile}>
                  <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
                </Btn>
              </div>
            </SettingsField>
          </div>
        )}
        {/* Sticks to the bottom of the panel while there are unsaved edits, so
          * Save stays in view below a long profile list instead of scrolling
          * out of sight under the default and retention controls. */}
        <div data-testid="session-environments-save-bar"
          className={`flex flex-wrap items-center gap-2 ${draft ? 'sticky bottom-0 z-10 -mx-1 border-t border-border bg-bg px-1 py-2' : ''}`}>
          <Btn type="submit" primary={!!draft} disabled={!draft || mutation.isPending}>{t('sessionEnvironments.save')}</Btn>
          <Btn type="button" disabled={!draft || mutation.isPending} onClick={() => {
            setDraft(null)
            setSaved(false)
            setOpenRows({ coder: [], fargate: [] })
            mutation.reset()
          }}>{t('sessionEnvironments.discard')}</Btn>
          {saved && <span role="status" className="text-[13px] text-muted">{t('sessionEnvironments.saved')}</span>}
        </div>
      </fieldset>
      {/* No hand-off: navigating would discard the unsaved environment draft. */}
      <ErrorNotice
        title={t('sessionEnvironments.saveError')}
        message={mutation.isError ? mutation.error.message || t('sessionEnvironments.saveError') : null}
      />
      </section>
      <section aria-label={t('sessionEnvironments.verifyStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.verifyStep')} />
        <p id={`${controlId}-verification`} className="text-[13px] text-muted">
          {draft ? t('sessionEnvironments.checkSaveFirst')
            : provider === 'fargate' ? t('sessionEnvironments.verifyHintFargate') : t('sessionEnvironments.verifyHint')}
        </p>
        <Btn type="button" disabled={!!draft || !anythingSaved || checkMutation.isPending}
          aria-describedby={`${controlId}-verification`} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? t('sessionEnvironments.checkRunning') : t('sessionEnvironments.check')}
        </Btn>
        {checkMutation.data && !draft && <CheckResults result={checkMutation.data} provider={provider}
          profiles={savedProfiles} fargateProfiles={savedFargateProfiles} />}
        <ErrorNotice askAgent message={checkMutation.isError ? t('sessionEnvironments.checkError') : null} />
      </section>
      <section aria-label={t('sessionEnvironments.manageStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.manageStep')} />
        {/* Follows the provider choice: each one lists and explains its own. */}
        <p className="text-[13px] text-muted">
          {provider === 'fargate' ? t('sessionEnvironments.managementHintFargate') : t('sessionEnvironments.managementHint')}
        </p>
        {anythingSaved && <WorkspaceList provider={provider} />}
        {provider === 'coder' && savedCoderUrl && (
          <a href={savedCoderUrl} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline">
            {t('sessionEnvironments.openCoder')}<ExternalLink size={12} className="lucide-inline" aria-hidden="true" />
          </a>
        )}
      </section>
    </form>
  )
}
