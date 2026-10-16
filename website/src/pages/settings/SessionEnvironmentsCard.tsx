import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, ChevronRight, ExternalLink, Minus, Plus, Trash2, X } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import type { ReactNode } from 'react'

import { api, type SessionEnvironmentsCheck } from '../../api/client'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { safeHttpUrl } from '../../lib/safeUrl'
import { BoxdIcon, CoderIcon, FlyIcon } from '../../components/BrandIcon'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsField, SettingsInput, SettingsSelect, SettingsStepper, SettingsToggle } from '../../components/settings'
import { Btn, Input, PanelSectionHeader } from '../../components/ui'

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
interface SpritesProfile {
  id: string
  name: string
  /** Name of the gateway vault secret holding the Sprites API token. */
  token_secret: string
  workdir: string
}
interface BoxdProfile {
  id: string
  name: string
  /** Snapshot (name or id) new machines start from; "" starts a fresh one. */
  snapshot: string
  /** 0 is the org's default size. A snapshot keeps its own size. */
  vcpu: number
  workdir: string
}
/** Mirrors BOXD_VCPU_CHOICES in config/session_environments.py. */
const BOXD_VCPU_CHOICES = [0, 1, 2, 4, 8]
/** Mirrors MAX_PROFILES in config/session_environments.py. */
const MAX_PROFILES = 20
interface EnvironmentSettings {
  coder_url: string
  profiles: EnvironmentProfile[]
  /** Fly.io Sprites profiles. Older gateways omit it. */
  sprites_profiles?: SpritesProfile[]
  /** boxd machine profiles. Older gateways omit it. */
  boxd_profiles?: BoxdProfile[]
  /** Profile id new chats start on; "" is this machine. Older gateways omit it. */
  default_profile?: string
  /** Days an archived session's workspace is kept. Older gateways omit it. */
  archived_retention_days?: number
  /** Stop every remote environment when the gateway shuts down. Older gateways omit it. */
  stop_on_shutdown?: boolean
}
// Symbols stay local to the editor: JSON configuration writes cannot include them.
const PROFILE_ROW_ID = Symbol('profile-row-id')
type ProfileDraft<T> = T & { readonly [PROFILE_ROW_ID]: string }
type EnvironmentDraft = Omit<EnvironmentSettings, 'profiles' | 'sprites_profiles' | 'boxd_profiles'> & {
  profiles: ProfileDraft<EnvironmentProfile>[]
  sprites_profiles?: ProfileDraft<SpritesProfile>[]
  boxd_profiles?: ProfileDraft<BoxdProfile>[]
}
function newProfileDraft<T>(profile: T): ProfileDraft<T> {
  return { ...profile, [PROFILE_ROW_ID]: crypto.randomUUID() }
}
/** Configuration responses carry fields, not editor identity. Match against
 * the submitted draft on save (ids are editable), or the last saved rows on
 * refetch, to keep each surviving row's local state out of the query cache. */
function environmentDraft(section: EnvironmentSettings | undefined, previous?: EnvironmentDraft): EnvironmentDraft | undefined {
  if (!section) return undefined
  const reconcile = <T extends { id: string }>(profiles: T[], rows?: ProfileDraft<T>[]): ProfileDraft<T>[] => profiles.map(profile => {
    const row = rows?.find(candidate => candidate.id === profile.id)
    return row ? { ...profile, [PROFILE_ROW_ID]: row[PROFILE_ROW_ID] } : newProfileDraft(profile)
  })
  return {
    ...section,
    profiles: reconcile(section.profiles, previous?.profiles),
    sprites_profiles: section.sprites_profiles && reconcile(section.sprites_profiles, previous?.sprites_profiles),
    boxd_profiles: section.boxd_profiles && reconcile(section.boxd_profiles, previous?.boxd_profiles),
  }
}
/** A removed profile cannot stay the default (the gateway refuses that save),
 * so removing the default falls back to this machine. */
function withoutDefault<T extends EnvironmentSettings>(next: T, removedId: string): T {
  return next.default_profile === removedId ? { ...next, default_profile: '' } : next
}
/** Mirrors ARCHIVED_RETENTION_DAYS_MAX in config/session_environments.py. */
const ARCHIVED_RETENTION_DAYS_MAX = 7
interface ConfigResponse {
  session_environments?: EnvironmentSettings
}

type ProviderId = 'coder' | 'sprites' | 'boxd'
const PROVIDERS: readonly ProviderId[] = ['coder', 'sprites', 'boxd']
const SETUP_DOCS: Record<ProviderId, string> = {
  coder: 'https://coder.com/docs/admin/setup',
  sprites: 'https://docs.sprites.dev',
  boxd: 'https://docs.boxd.sh/quickstart',
}
const emptyRows = (): Record<string, boolean> => ({})

/** One saved profile as a disclosure. The card stays one element: its header
 * keeps the profile's name in place and only the field body expands below it.
 * Keep the body mounted so field-local drafts, including tokens, survive
 * collapse. Hidden and inert controls stay outside the focus order. */
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
        <motion.div key="body" id={bodyId} hidden={!open} {...(!open ? { inert: '' } : {})}
          initial={false} animate={{ height: open ? 'auto' : 0, opacity: open ? 1 : 0 }}
          transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }} style={{ overflow: 'hidden' }}>
          <fieldset aria-label={title} className="min-w-0 space-y-3 px-3 pb-3">{children}</fieldset>
        </motion.div>
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

/** One provider row of the readiness check: ok, or the failure as a notice. */
function ProviderCheckLine({ row, name, okText }: {
  row: { id: string; ok: boolean; error?: string }
  name: string
  okText: string
}) {
  const { t } = useTranslation()
  return row.ok ? (
    <CheckLine status="ok">{okText}</CheckLine>
  ) : (
    <li><ErrorNotice variant="inline" message={t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })} askAgent /></li>
  )
}

/** What the readiness check found, in the order a person fixes things.
 *
 * Follows the provider tab, like the Manage step: a Sprites setup never reports
 * Coder rows even when a Coder URL is still saved. Git rows apply to every
 * provider. */
function CheckResults({ result, provider, profiles, spritesProfiles, boxdProfiles }: {
  result: SessionEnvironmentsCheck
  provider: ProviderId
  profiles: EnvironmentProfile[]
  spritesProfiles: SpritesProfile[]
  boxdProfiles: BoxdProfile[]
}) {
  const { t } = useTranslation()
  const names = new Map(profiles.map(p => [p.id, p]))
  const spritesNames = new Map(spritesProfiles.map(p => [p.id, p.name || p.id]))
  const boxdNames = new Map(boxdProfiles.map(p => [p.id, p.name || p.id]))
  return (
    <ul aria-label={t('sessionEnvironments.checkResults')} className="space-y-1.5">
      {provider === 'coder' && (result.coder.ok ? (
        <CheckLine status="ok">{t('sessionEnvironments.checkCoderOk', { owner: result.coder.owner ?? '' })}</CheckLine>
      ) : (
        <li><ErrorNotice variant="inline" message={t('sessionEnvironments.checkCoderFailed', { error: result.coder.error ?? '' })} askAgent /></li>
      ))}
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
      {provider === 'sprites' && (result.sprites ?? []).map(row => {
        const name = spritesNames.get(row.id) ?? row.id
        return <ProviderCheckLine key={`sprites:${row.id}`} row={row} name={name}
          okText={t('sessionEnvironments.checkSpritesTokenOk', { name })} />
      })}
      {provider === 'boxd' && (result.boxd ?? []).map(row => {
        const name = boxdNames.get(row.id) ?? row.id
        return <ProviderCheckLine key={`boxd:${row.id}`} row={row} name={name}
          okText={t('sessionEnvironments.checkBoxdOk', { name })} />
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

/** Store a Sprites API token straight into the gateway vault under the
 * profile's secret name. Masked while typed, cleared once stored, and never
 * read back: the vault is write-only to the dashboard, so the field can only
 * say whether a token is saved, never show one. Saved on its own button, not
 * with the profile draft, so the token never enters config or the draft. */
function SpritesTokenField({ secretName, profileSaved }: { secretName: string; profileSaved: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const inputId = useId()
  const [value, setValue] = useState('')
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: () => api.secretsList(), retry: false })
  const save = useMutation({
    mutationFn: () => api.secretsSave(secretName, value),
    onSuccess: () => {
      setValue('')
      void qc.invalidateQueries({ queryKey: ['secrets'] })
    },
  })
  const stored = !!secretName && (secrets.data?.names ?? []).includes(secretName)
  return (
    <div className="space-y-1.5">
      <label htmlFor={inputId} className="block text-[13px] font-medium text-text">{t('sessionEnvironments.apiToken')}</label>
      <div className="flex min-w-0 gap-2">
        <Input id={inputId} type="password" autoComplete="new-password" spellCheck={false}
          className="min-w-0 flex-1 font-mono" value={value} onChange={e => setValue(e.target.value)}
          placeholder={stored ? t('sessionEnvironments.apiTokenSavedPlaceholder') : t('sessionEnvironments.apiTokenPlaceholder')} />
        <Btn type="button" disabled={!value || !secretName || save.isPending} onClick={() => save.mutate()}>
          {t('sessionEnvironments.apiTokenSave')}
        </Btn>
      </div>
      {secrets.isSuccess && <p role="status" className="text-[13px] text-muted">
        {stored ? t('sessionEnvironments.apiTokenStored', { name: secretName }) : t('sessionEnvironments.apiTokenHint', { name: secretName })}
      </p>}
      {/* No hand-off: a retry preserves both the typed token and profile draft. */}
      <ErrorNotice message={secrets.isError ? t('sessionEnvironments.apiTokenListError') : null}
        footer={<Btn type="button" onClick={() => void secrets.refetch()}>{t('sessionEnvironments.retry')}</Btn>} />
      {/* Saving the token does not save the profile: say so until it is saved,
        * or the composer shows no Sprites choice and nothing explains why. */}
      {!profileSaved && (
        <div data-testid="sprites-profile-unsaved" className="flex flex-wrap items-center gap-2">
          <p className="min-w-0 flex-1 text-[13px] text-warn">{t('sessionEnvironments.spritesProfileUnsaved')}</p>
          {/* The same submit as the section's Save: it saves the whole draft. */}
          <Btn type="submit" primary>{t('sessionEnvironments.save')}</Btn>
        </div>
      )}
      {/* No hand-off: navigating would discard the typed token. */}
      <ErrorNotice message={save.isError ? t('sessionEnvironments.apiTokenError') : null} />
    </div>
  )
}

/** The workspaces Kiro Crew owns for one provider, from the gateway's registries
 * (no remote call). */
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
            {row.provider === 'sprites'
              ? t('sessionEnvironments.workspaceSprite')
              : row.provider === 'boxd'
                ? t('sessionEnvironments.workspaceBoxd')
                : t('sessionEnvironments.workspaceSession')}
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
  const section = query.data?.session_environments
  const [savedRows, setSavedRows] = useState<{
    section: EnvironmentSettings | undefined
    value: EnvironmentDraft | undefined
  }>(() => ({ section, value: environmentDraft(section) }))
  // Reconcile before committing children, so a settings refresh cannot remount
  // a surviving row and discard its field-local token.
  if (savedRows.section !== section) {
    setSavedRows({ section, value: environmentDraft(section, savedRows.value) })
  }
  const savedConfig = savedRows.value
  // Null follows fresh server data. Once edited, background refetches never
  // replace the draft. Saving freezes controls until the server responds.
  const [draft, setDraft] = useState<EnvironmentDraft | null>(null)
  const [saved, setSaved] = useState(false)
  // Null until the person picks a tab; until then the tab follows what is saved
  // (see `provider` below), so a Sprites-only setup does not open on Coder.
  const [providerChoice, setProvider] = useState<ProviderId | null>(null)
  // Disclosure and token state belong to an immutable editor row, never its
  // position or editable profile id. Removing a row retires its local state.
  const [openRows, setOpenRows] = useState(emptyRows)
  const toggleRow = (rowId: string) => setOpenRows(rows => ({ ...rows, [rowId]: !rows[rowId] }))
  const addRow = (rowId: string) => setOpenRows(rows => ({ ...rows, [rowId]: true }))
  const removeRow = (rowId: string) => setOpenRows(rows => {
    const next = { ...rows }
    delete next[rowId]
    return next
  })
  const mutation = useMutation({
    mutationFn: (value: EnvironmentDraft) => api.patchConfig('session_environments', value) as Promise<ConfigResponse>,
    onMutate: () => qc.cancelQueries({ queryKey: ['kirocrewConfig'] }),
    onSuccess: (response, submitted) => {
      const cached = qc.setQueryData<ConfigResponse>(['kirocrewConfig'], response)
      setSavedRows({
        section: cached?.session_environments,
        value: environmentDraft(cached?.session_environments, submitted),
      })
      setDraft(null)
      setOpenRows(emptyRows())
      setSaved(true)
    },
  })
  const config = draft ?? savedConfig
  // The check reads the SAVED configuration on the gateway, so it is offered
  // only with no unsaved draft, and its answer is dropped when one appears.
  const savedCoderUrl = safeHttpUrl(query.data?.session_environments?.coder_url ?? '')
  const savedProfiles = query.data?.session_environments?.profiles ?? []
  const savedSpritesProfiles = query.data?.session_environments?.sprites_profiles ?? []
  const savedBoxdProfiles = query.data?.session_environments?.boxd_profiles ?? []
  // The providers are separate choices, so open on the one in use. Coder wins a
  // tie because it is the only one with deployment-level settings.
  const provider: ProviderId = providerChoice
    ?? (savedCoderUrl || savedProfiles.length > 0 ? 'coder'
      : savedSpritesProfiles.length > 0 ? 'sprites'
        : savedBoxdProfiles.length > 0 ? 'boxd'
          : 'coder')
  // Whether the open tab has anything saved for the check and the Manage list.
  const providerSaved = provider === 'coder' ? !!savedCoderUrl
    : provider === 'sprites' ? savedSpritesProfiles.length > 0
      : savedBoxdProfiles.length > 0
  const checkMutation = useMutation({ mutationFn: () => api.sessionEnvironmentsCheck() })
  const change = (next: EnvironmentDraft) => {
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
    const row = newProfileDraft({ id: `profile-${n}`, name: '', template: '', workdir: '' })
    addRow(row[PROFILE_ROW_ID])
    change({ ...config, profiles: [...config.profiles, row] })
  }

  const spritesProfiles = config.sprites_profiles ?? []
  const spritesFields: { key: 'id' | 'name' | 'token_secret' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'token_secret', label: t('sessionEnvironments.tokenSecret') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateSprites = (index: number, updates: Partial<SpritesProfile>) => {
    change({ ...config, sprites_profiles: spritesProfiles.map((profile, i) => i === index ? { ...profile, ...updates } : profile) })
  }
  const addSprites = () => {
    let n = 1
    while (spritesProfiles.some(profile => profile.id === `sprites-${n}`)) n++
    // A name is required to save; start from one so a new profile saves as-is.
    const name = n === 1 ? t('sessionEnvironments.sprites') : `${t('sessionEnvironments.sprites')} ${fmtNumber(n)}`
    const row = newProfileDraft({
      id: `sprites-${n}`, name, token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace',
    })
    addRow(row[PROFILE_ROW_ID])
    change({ ...config, sprites_profiles: [...spritesProfiles, row] })
  }
  const boxdProfiles = config.boxd_profiles ?? []
  const boxdFields: { key: 'id' | 'name' | 'snapshot' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'snapshot', label: t('sessionEnvironments.boxdSnapshot') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateBoxd = (index: number, updates: Partial<BoxdProfile>) => {
    // A snapshot keeps its own size (the gateway refuses both), so naming one
    // returns the size to the default.
    const next = updates.snapshot ? { ...updates, vcpu: 0 } : updates
    change({ ...config, boxd_profiles: boxdProfiles.map((profile, i) => i === index ? { ...profile, ...next } : profile) })
  }
  const addBoxd = () => {
    let n = 1
    while (boxdProfiles.some(profile => profile.id === `boxd-${n}`)) n++
    const name = n === 1 ? t('sessionEnvironments.boxd') : `${t('sessionEnvironments.boxd')} ${fmtNumber(n)}`
    const row = newProfileDraft({
      id: `boxd-${n}`, name, snapshot: '', vcpu: 0, workdir: '/home/boxd/workspace',
    })
    addRow(row[PROFILE_ROW_ID])
    change({ ...config, boxd_profiles: [...boxdProfiles, row] })
  }
  const providerOptions: Record<ProviderId, { label: string; icon: ReactNode }> = {
    coder: { label: t('sessionEnvironments.coder'), icon: <CoderIcon size={14} /> },
    sprites: { label: t('sessionEnvironments.sprites'), icon: <FlyIcon size={18} /> },
    boxd: { label: t('sessionEnvironments.boxd'), icon: <BoxdIcon size={16} /> },
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
      {/* Global: these apply to every profile, so they sit above the profile
        * list. Older gateways omit either key, and then its control stays
        * hidden. */}
      {(config.default_profile !== undefined || config.archived_retention_days !== undefined || config.stop_on_shutdown !== undefined) && (() => {
        const all = [...config.profiles, ...spritesProfiles, ...boxdProfiles].filter(p => p.id)
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
              {config.stop_on_shutdown !== undefined && (
                <SettingsToggle configKey="session_environments.stop_on_shutdown"
                  label={t('sessionEnvironments.stopOnShutdown')}
                  description={t('sessionEnvironments.stopOnShutdownHint')}
                  checked={config.stop_on_shutdown}
                  onChange={on => change({ ...config, stop_on_shutdown: on })} />
              )}
            </fieldset>
          </section>
        )
      })()}
      <fieldset disabled={mutation.isPending} className="min-w-0 pt-3">
        <legend className="mb-2 text-[13px] font-semibold text-text">{t('sessionEnvironments.provider')}</legend>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {PROVIDERS.map(id => (
            <label key={id} className={`flex items-center gap-3 rounded-lg border p-3 cursor-pointer text-[13px] ${provider === id ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-bg text-text'}`}>
              <input type="radio" name={`${controlId}-provider`} value={id} checked={provider === id}
                aria-label={providerOptions[id].label} onChange={() => setProvider(id)} className="accent-[var(--accent)]" />
              {providerOptions[id].icon}
              {providerOptions[id].label}
            </label>
          ))}
        </div>
      </fieldset>
      <section aria-label={t('sessionEnvironments.configureStep')} className="space-y-3 pt-3">
      <PanelSectionHeader label={t('sessionEnvironments.configureStep')} />
      <details className="rounded-lg border border-border p-3">
        <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.setup')}</summary>
        <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
          {provider === 'coder' ? <>
            <li>{t('sessionEnvironments.coderSetup1')}</li>
            <li>{t('sessionEnvironments.coderSetup2')}</li>
            <li>{t('sessionEnvironments.coderSetup3')}</li>
          </> : provider === 'sprites' ? <>
            <li>{t('sessionEnvironments.spritesSetup1')}</li>
            <li>{t('sessionEnvironments.spritesSetup2')}</li>
            <li>{t('sessionEnvironments.spritesSetup3')}</li>
          </> : <>
            <li>{t('sessionEnvironments.boxdSetup1')}</li>
            <li>{t('sessionEnvironments.boxdSetup2')}</li>
            <li>{t('sessionEnvironments.boxdSetup3')}</li>
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
              <ProfileDisclosure key={profile[PROFILE_ROW_ID]} title={profile.name || profile.id} subtitle={profile.template}
                open={!!openRows[profile[PROFILE_ROW_ID]]} onToggle={() => toggleRow(profile[PROFILE_ROW_ID])}>
                {profileFields.map(field => (
                  <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateProfile(index, field.key, value)} />
                ))}
                <SettingsToggle anchor={false} label={survivesStopLabel}
                  description={t('sessionEnvironments.computeSurvivesStopHint')}
                  checked={profile.compute_survives_stop !== false}
                  onChange={on => change({ ...config, profiles: config.profiles.map((p, i) => i === index ? { ...p, compute_survives_stop: on } : p) })} />
                <Btn type="button" onClick={() => {
                  removeRow(profile[PROFILE_ROW_ID])
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
        <div hidden={provider !== 'sprites'} className="space-y-3">
          {/* Mounted only on its own tab, so an older gateway shows one notice. */}
          {config.sprites_profiles === undefined ? (provider === 'sprites' && <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />) : (
            <SettingsField configKey="session_environments.sprites_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesSpritesHint')}>
              <div className="min-w-0 space-y-3">
                {spritesProfiles.map((profile, index) => (
                  <ProfileDisclosure key={profile[PROFILE_ROW_ID]} title={profile.name || profile.id} subtitle={profile.token_secret}
                    open={!!openRows[profile[PROFILE_ROW_ID]]} onToggle={() => toggleRow(profile[PROFILE_ROW_ID])}>
                    {spritesFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateSprites(index, { [field.key]: value })} />)}
                    <SpritesTokenField secretName={profile.token_secret}
                      profileSaved={savedSpritesProfiles.some(saved => saved.id === profile.id)} />
                    <Btn type="button" onClick={() => {
                      removeRow(profile[PROFILE_ROW_ID])
                      const remaining = spritesProfiles.filter((_, i) => i !== index)
                      change(withoutDefault({ ...config, sprites_profiles: remaining }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={spritesProfiles.length >= MAX_PROFILES} onClick={addSprites}>
                  <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
                </Btn>
              </div>
            </SettingsField>
          )}
        </div>
        <div hidden={provider !== 'boxd'} className="space-y-3">
          {config.boxd_profiles === undefined ? (provider === 'boxd' && <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />) : (
            <SettingsField configKey="session_environments.boxd_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesBoxdHint')}>
              <div className="min-w-0 space-y-3">
                {boxdProfiles.map((profile, index) => (
                  <ProfileDisclosure key={profile[PROFILE_ROW_ID]} title={profile.name || profile.id} subtitle={profile.snapshot}
                    open={!!openRows[profile[PROFILE_ROW_ID]]} onToggle={() => toggleRow(profile[PROFILE_ROW_ID])}>
                    {boxdFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateBoxd(index, { [field.key]: value })} />)}
                    {/* A snapshot keeps its own size, so the size applies only without one. */}
                    <SettingsSelect label={t('sessionEnvironments.boxdSize')} description={t('sessionEnvironments.boxdSizeHint')}
                      value={String(profile.snapshot ? 0 : profile.vcpu)} disabled={!!profile.snapshot}
                      options={BOXD_VCPU_CHOICES.map(String)}
                      optionLabels={BOXD_VCPU_CHOICES.map(vcpu => vcpu === 0
                        ? t('sessionEnvironments.boxdSizeDefault')
                        : t('sessionEnvironments.boxdSizeVcpu', { vcpu: fmtNumber(vcpu), memory: fmtNumber(vcpu * 4) }))}
                      onChange={value => updateBoxd(index, { vcpu: Number(value) })} />
                    <Btn type="button" onClick={() => {
                      removeRow(profile[PROFILE_ROW_ID])
                      change(withoutDefault({ ...config, boxd_profiles: boxdProfiles.filter((_, i) => i !== index) }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={boxdProfiles.length >= MAX_PROFILES} onClick={addBoxd}>
                  <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
                </Btn>
              </div>
            </SettingsField>
          )}
        </div>
        {/* Sticks to the bottom of the panel while there are unsaved edits, so
          * Save stays in view below a long profile list instead of scrolling
          * out of sight under the default and retention controls. */}
        <div data-testid="session-environments-save-bar"
          className={`flex flex-wrap items-center gap-2 ${draft ? 'sticky bottom-0 z-10 -mx-1 border-t border-border bg-bg px-1 py-2' : ''}`}>
          <Btn type="submit" primary={!!draft} disabled={!draft || mutation.isPending}>{t('sessionEnvironments.save')}</Btn>
          <Btn type="button" disabled={!draft || mutation.isPending} onClick={() => {
            setDraft(null)
            setSaved(false)
            setOpenRows(emptyRows())
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
            : provider === 'coder' ? t('sessionEnvironments.verifyHint')
              : provider === 'sprites' ? t('sessionEnvironments.verifyHintSprites')
                : t('sessionEnvironments.verifyHintBoxd')}
        </p>
        <Btn type="button" disabled={!!draft || !providerSaved || checkMutation.isPending}
          aria-describedby={`${controlId}-verification`} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? t('sessionEnvironments.checkRunning') : t('sessionEnvironments.check')}
        </Btn>
        {checkMutation.data && !draft && <CheckResults result={checkMutation.data} provider={provider}
          profiles={savedProfiles} spritesProfiles={savedSpritesProfiles} boxdProfiles={savedBoxdProfiles} />}
        <ErrorNotice askAgent message={checkMutation.isError ? t('sessionEnvironments.checkError') : null} />
      </section>
      <section aria-label={t('sessionEnvironments.manageStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.manageStep')} />
        <p className="text-[13px] text-muted">
          {provider === 'coder' ? t('sessionEnvironments.managementHint')
            : provider === 'sprites' ? t('sessionEnvironments.managementHintSprites')
              : t('sessionEnvironments.managementHintBoxd')}
        </p>
        {providerSaved && <WorkspaceList provider={provider} />}
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
