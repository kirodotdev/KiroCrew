import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Check, AlertTriangle, Lock, MonitorOff } from 'lucide-react'
import { IMessageIcon } from '../../components/IMessageIcon'
import { SettingsSection, SettingsCard, SettingsInput, SettingsSelect, SettingsToggle } from '../../components/settings'
import { Btn } from '../../components/ui'
import { TagListEditor } from './SlackPanel'
import { api, type IMessageConfigData, type IMessageConfigSave } from '../../api/client'

import { i18nT } from '../../i18n/t'
/** Brand name — do-not-translate, so it lives here rather than in the catalog. */
const CHANNEL_NAME = "iMessage"
const BRIDGE_URL = 'https://github.com/steipete/imsg'
const SETUP_GUIDE = 'https://github.com/kirodotdev/KiroCrew/blob/main/src/kiro_crew/docs/imessage-integration.md'

const SERVICES = ['imessage', 'sms', 'auto']

/**
 * Accept an Apple Account email or a phone-shaped handle. Mirrors the backend
 * check, and uses linear string ops for the same reason it does — a
 * backtracking pattern run over a pasted list is a denial-of-service shape.
 *
 * A phone handle may carry dialling punctuation, spaces included: the backend
 * normalizes formatting away before comparing, so rejecting "+1 (555) 123-4567"
 * would refuse a handle the transport treats as identical to the digits.
 */
function isValidHandle(v: string): boolean {
  if (!v || v.length > 254) return false
  if (v.includes('@')) {
    if (/\s/.test(v)) return false
    const at = v.indexOf('@')
    if (at <= 0 || v.indexOf('@', at + 1) !== -1) return false
    return v.slice(at + 2, -1).includes('.')
  }
  const body = v.startsWith('+') ? v.slice(1) : v
  let digits = 0
  for (const ch of body) {
    if (ch >= '0' && ch <= '9') digits++
    else if (!'()-. '.includes(ch)) return false
  }
  return digits >= 4 && digits <= 18
}

type Draft = {
  enabled: boolean
  allowed_handles: string[]
  db_path: string
  service: string
  /** Whether this channel files its sessions in a folder at all (off = unfiled). */
  session_folder_on: boolean
  /** Folder name, kept while the toggle is off so turning it back on restores it. */
  session_folder: string
}

function draftFrom(c: IMessageConfigData): Draft {
  return {
    enabled: c.enabled,
    allowed_handles: [...c.allowed_handles],
    db_path: c.db_path ?? '',
    service: SERVICES.includes(c.service) ? c.service : 'imessage',
    // A configured name IS the on-state — the backend has one field, where ""
    // means off, so the toggle is derived rather than separately persisted.
    session_folder_on: !!c.session_folder,
    session_folder: c.session_folder ?? '',
  }
}

/** Status pill mirroring the other channel panels' connection states. */
function StatusBadge({ config }: { config: IMessageConfigData }) {
  const [dot, text, cls] = config.connected
    ? ['var(--ok)', i18nT('pages.settings.iMessagePanel.active'), 'text-ok']
    : config.configured
      ? ['var(--warn)', i18nT('pages.settings.iMessagePanel.not_active'), 'text-warn']
      : ['var(--muted)', i18nT('pages.settings.iMessagePanel.needs_setup'), 'text-muted']
  return (
    <span className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${cls}`}>
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: dot }} />
      {text}
    </span>
  )
}

/** One-line explanation of WHY iMessage is not active, with the fix. */
function connectionHint(config: IMessageConfigData): string {
  if (config.connected || !config.configured) return ''
  if (config.connect_error) {
    return i18nT('pages.settings.iMessagePanel.connection_failed', { error: config.connect_error })
  }
  return i18nT('pages.settings.iMessagePanel.saved_but_not_running')
}

/** iMessage channel-integration settings. */
export function IMessagePanel() {
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery<IMessageConfigData>({
    queryKey: ['imessage-config'],
    queryFn: api.getIMessageConfig,
    retry: false,
    // An ambient focus refetch mid-edit would hand back a fresh `data`
    // object and clobber unsaved edits via the sync effect below.
    refetchOnWindowFocus: false,
  })

  const [draft, setDraft] = useState<Draft | null>(null)
  const [saved, setSaved] = useState(false)
  const [restartHint, setRestartHint] = useState(false)
  const [error, setError] = useState('')

  // Seed the local draft once, when server config first arrives. Nothing re-arms
  // it: neither a background refetch nor a post-save invalidation may discard
  // edits the user has made since. See the save mutation's onSuccess.
  const syncArmed = useRef(true)
  useEffect(() => {
    if (data && syncArmed.current) {
      syncArmed.current = false
      setDraft(draftFrom(data))
    }
  }, [data])

  const saveMut = useMutation({
    mutationFn: (body: Partial<IMessageConfigSave>) => api.saveIMessageConfig(body),
    onError: (e: unknown) => {
      let msg = i18nT('pages.settings.iMessagePanel.save_failed')
      if (e instanceof Error && e.message) {
        try {
          msg = JSON.parse(e.message).error ?? e.message
        } catch {
          msg = e.message
        }
      }
      setError(msg)
      setTimeout(() => setError(''), 8000)
    },
    onSuccess: (res) => {
      setSaved(true)
      setRestartHint(!!res.restart_required)
      // Deliberately NOT re-arming the draft sync here. The save is validated,
      // so a success means the server took the values we sent -- there is
      // nothing to reseed from. Re-arming lost any field the user edited while
      // the request was in flight: the invalidation below refetches, the sync
      // effect fired again, and it overwrote the newer draft with the values
      // that had just been saved. The draft stays authoritative after the
      // initial load; the invalidation is still wanted so the status badge and
      // any other consumer of this query see the new server state.
      setTimeout(() => setSaved(false), 6000)
      qc.invalidateQueries({ queryKey: ['imessage-config'] })
    },
  })

  const handleSave = useCallback(() => {
    if (!draft) return
    setError('')
    saveMut.mutate({
      enabled: draft.enabled,
      allowed_handles: draft.allowed_handles,
      db_path: draft.db_path.trim(),
      service: draft.service,
      // Off sends "" (the field's off-state); on with a blank name falls back
      // to "iMessage", which is what the toggle's description promises.
      session_folder: draft.session_folder_on ? (draft.session_folder.trim() || CHANNEL_NAME) : '',
    })
  }, [draft, saveMut])

  if (isLoading) return <p className="text-[13px] text-muted p-4">{i18nT('pages.settings.iMessagePanel.loading')}</p>
  if (isError || !data || !draft) return <p className="text-[13px] text-danger p-4">{i18nT('pages.settings.iMessagePanel.cannot_load')}</p>

  const upd = (patch: Partial<Draft>) => setDraft(d => (d ? { ...d, ...patch } : d))
  // Off macOS every input would persist a setting that can never take effect,
  // so the form is locked for the same reason a remote session is.
  const ro = data.read_only || !data.supported

  return (
    <>
      {/* ── Header ── */}
      <div className="flex items-start gap-3 mb-1 mt-1">
        <div className="w-9 h-9 rounded-lg bg-bg-elevated border border-border flex items-center justify-center flex-none text-text">
          <IMessageIcon size={20} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap">
            <h3 className="text-[15px] font-semibold text-text-strong">{CHANNEL_NAME}</h3>
            <StatusBadge config={data} />
          </div>
          <p className="text-[12px] text-muted mt-1">
            {i18nT('pages.settings.iMessagePanel.tagline')}
          </p>
          {connectionHint(data) && (
            <p className="text-[12px] text-warn mt-1 flex items-center gap-1.5">
              <AlertTriangle size={12} className="flex-none" />
              {connectionHint(data)}
            </p>
          )}
        </div>
      </div>

      {/* ── Unsupported host ── */}
      {!data.supported && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mb-3">
          <MonitorOff size={13} className="text-muted flex-none" />
          <span className="text-[12px] text-muted">
            {i18nT('pages.settings.iMessagePanel.macos_only')}
          </span>
        </div>
      )}

      {/* ── Read-only notice (remote session) ── */}
      {data.read_only && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2 mb-3">
          <Lock size={13} className="text-muted flex-none" />
          <span className="text-[12px] text-muted">
            {i18nT('pages.settings.iMessagePanel.managed_locally')}
          </span>
        </div>
      )}

      {/* ── Prerequisites ── */}
      <SettingsSection title={i18nT('pages.settings.iMessagePanel.before_you_start')}>
        <SettingsCard>
          <p className="text-[13px] text-text m-0">
            {i18nT('pages.settings.iMessagePanel.prerequisites_body')}
          </p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <a href={SETUP_GUIDE} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium border transition-all bg-accent text-accent-fg border-accent hover:bg-accent-hover">
              {i18nT('pages.settings.iMessagePanel.setup_guide')} <ExternalLink size={13} />
            </a>
            <a href={BRIDGE_URL} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline">
              {i18nT('pages.settings.iMessagePanel.bridge_project')} <ExternalLink size={13} />
            </a>
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Access ── */}
      <SettingsSection title={i18nT('pages.settings.iMessagePanel.access')}>
        <SettingsCard index={1}>
          <SettingsToggle
            label={i18nT('pages.settings.iMessagePanel.enable')}
            description={i18nT('pages.settings.iMessagePanel.enable_desc')}
            checked={draft.enabled}
            onChange={v => upd({ enabled: v })}
            disabled={ro}
          />
          <TagListEditor
            label={i18nT('pages.settings.iMessagePanel.allowed_handles')}
            description={i18nT('pages.settings.iMessagePanel.allowed_handles_desc')}
            values={draft.allowed_handles}
            placeholder={i18nT('pages.settings.iMessagePanel.handle_placeholder')}
            onChange={v => upd({ allowed_handles: v })}
            validate={isValidHandle}
            readOnly={ro}
          />
          {draft.enabled && draft.allowed_handles.length === 0 && (
            <p className="text-[12px] text-warn mt-2 mb-0 flex items-center gap-1.5">
              <AlertTriangle size={12} className="flex-none" />
              {i18nT('pages.settings.iMessagePanel.empty_allowlist_hint')}
            </p>
          )}
          {/* Optional per-channel session filing. Off by default: iMessage
              conversations stay unfiled in the sidebar. */}
          <div className="border-t border-border mt-4 pt-4">
            <SettingsToggle
              label={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder')}
              description={i18nT('pages.settings.botChannelPanel.file_sessions_in_folder_desc', { channel: CHANNEL_NAME })}
              checked={draft.session_folder_on}
              onChange={v => upd({ session_folder_on: v })}
              disabled={ro}
            />
            {draft.session_folder_on && (
              <div className="mt-4">
                <SettingsInput
                  label={i18nT('pages.settings.botChannelPanel.session_folder_name')}
                  description={i18nT('pages.settings.botChannelPanel.session_folder_name_desc')}
                  value={draft.session_folder}
                  onChange={v => upd({ session_folder: v })}
                  placeholder={CHANNEL_NAME}
                  disabled={ro}
                />
              </div>
            )}
          </div>
        </SettingsCard>
      </SettingsSection>

      {/* ── Bridge ── */}
      <SettingsSection title={i18nT('pages.settings.iMessagePanel.bridge')}>
        <SettingsCard index={2}>
          <SettingsInput
            label={i18nT('pages.settings.iMessagePanel.db_path')}
            description={i18nT('pages.settings.iMessagePanel.db_path_desc')}
            value={draft.db_path}
            onChange={v => upd({ db_path: v })}
            placeholder="~/Library/Messages/chat.db"
            disabled={ro}
          />
          <SettingsSelect
            label={i18nT('pages.settings.iMessagePanel.service')}
            description={i18nT('pages.settings.iMessagePanel.service_desc')}
            value={draft.service}
            options={SERVICES}
            optionLabels={[
              i18nT('pages.settings.iMessagePanel.service_imessage'),
              i18nT('pages.settings.iMessagePanel.service_sms'),
              i18nT('pages.settings.iMessagePanel.service_auto'),
            ]}
            onChange={v => upd({ service: v })}
            disabled={ro}
          />
        </SettingsCard>
      </SettingsSection>

      {/* ── Save (hidden on read-only / unsupported hosts) ── */}
      {!ro && <div className="flex items-center gap-3 mt-1 mb-4">
        <Btn primary onClick={handleSave} disabled={saveMut.isPending}>
          {saveMut.isPending ? i18nT('pages.settings.iMessagePanel.saving') : i18nT('pages.settings.iMessagePanel.save')}
        </Btn>
        {saved && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-ok">
            <Check size={14} /> {restartHint ? i18nT('pages.settings.iMessagePanel.saved_restart') : i18nT('pages.settings.iMessagePanel.saved')}
          </span>
        )}
        {error && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-danger">
            <AlertTriangle size={14} /> {error}
          </span>
        )}
      </div>}
    </>
  )
}
