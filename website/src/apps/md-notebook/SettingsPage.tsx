/**
 * Settings, rendered as a PAGE in the note pane rather than a floating modal —
 * same header geometry and reading column as a note, so switching between a
 * note and Settings does not shift the layout.
 *
 * Escape leaves the page; a click outside does NOT, because this is a
 * destination rather than a dialog.
 */
import { useCallback, useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { Folder as FolderIcon, GitBranch, Settings, X } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import {
  ACCENT,
  ACCENT_BG,
  ACCENT_FG,
  AUTO_COMMIT_MINS,
  COLUMN_MAX_WIDTH,
  COLUMN_PAD_X,
  DEFAULT_SYNC_SHORTCUT,
  FONT_BODY,
  FONT_MONO,
  MAX_AUTO_SYNC_MINS,
  MIN_AUTO_SYNC_MINS,
} from './constants'
import Clickable from '../../components/Clickable'
import { GithubIcon, Switch, TextLink } from './bits'
import { knowledgeRegister, knowledgeUnregister, notesApi } from './api'
import { formatShortcut } from './utils'
import type { Shortcut, Vault } from './types'

const field: CSSProperties = {
  boxSizing: 'border-box',
  fontSize: '12px',
  padding: '6px 10px',
  borderRadius: '6px',
  border: '1px solid var(--border)',
  background: 'var(--bg)',
  color: 'var(--text)',
  outline: 'none',
}

const sectionLabel: CSSProperties = {
  fontSize: '11px',
  textTransform: 'uppercase',
  letterSpacing: '.04em',
  color: 'var(--muted)',
  marginBottom: '6px',
}

export interface SettingsPageProps {
  vaults: Vault[]
  activeVaultId: string | null
  hasPat: boolean
  hasGhAuth: boolean
  autoSync: boolean
  autoSyncMins: number
  autoCommit: boolean
  shortcut: Shortcut
  onClose: () => void
  onSwitchVault: (id: string) => void
  onConnect: () => void
  onForget: (id: string) => Promise<void>
  onSetPat: (pat: string) => Promise<void>
  onSetKnowledge: (id: string, on: boolean, sourceId?: string) => Promise<void>
  onAutoSync: (on: boolean) => void
  onAutoSyncMins: (n: number) => void
  onAutoCommit: (on: boolean) => void
  onSetShortcut: (sc: Shortcut) => void
  onRecordingChange: (on: boolean) => void
}

export function SettingsPage({
  vaults,
  activeVaultId,
  hasPat,
  hasGhAuth,
  autoSync,
  autoSyncMins,
  autoCommit,
  shortcut,
  onClose,
  onSwitchVault,
  onConnect,
  onForget,
  onSetPat,
  onSetKnowledge,
  onAutoSync,
  onAutoSyncMins,
  onAutoCommit,
  onSetShortcut,
  onRecordingChange,
}: SettingsPageProps) {
  const [pat, setPat] = useState('')
  const [busy, setBusy] = useState(false)
  // Messages are scoped to the control that produced them, so feedback appears
  // where the click happened rather than at the foot of the page.
  const [patNote, setPatNote] = useState<string | null>(null)
  const [knowledgeMsg, setKnowledgeMsg] = useState<
    Record<string, { text: string; error?: boolean } | null>
  >({})
  // Scoped per vault for the same reason as knowledgeMsg: one vault's "trash is
  // empty" must not appear under another's row.
  const [trashMsg, setTrashMsg] = useState<Record<string, string | null>>({})

  // Nothing is reported on success — the file manager coming to the front is the
  // result. Only the two cases with no visible effect get a message.
  const revealTrash = useCallback(async (vault: Vault) => {
    setTrashMsg(prev => ({ ...prev, [vault.id]: null }))
    try {
      const r = await notesApi.openTrash(vault.id)
      if (r.empty) {
        setTrashMsg(prev => ({ ...prev, [vault.id]: i18nT('apps.mdNotebook.trash.empty') }))
      }
    } catch (e) {
      const code = (e as { body?: { code?: string } }).body?.code
      setTrashMsg(prev => ({
        ...prev,
        [vault.id]:
          code === 'folder_open_unsupported'
            ? i18nT('apps.mdNotebook.trash.unsupported')
            : i18nT('apps.mdNotebook.trash.failed', {
                message: e instanceof Error ? e.message : String(e),
              }),
      }))
    }
  }, [])
  const [knowledgeBusy, setKnowledgeBusy] = useState<string | null>(null)
  const [confirmForget, setConfirmForget] = useState<string | null>(null)
  const [recording, setRecording] = useState(false)
  const [shortcutError, setShortcutError] = useState<string | null>(null)

  const toggleKnowledge = useCallback(
    async (vault: Vault, next: boolean) => {
      setKnowledgeBusy(vault.id)
      setKnowledgeMsg(m => ({ ...m, [vault.id]: null }))
      const say = (text: string, error?: boolean) =>
        setKnowledgeMsg(m => ({ ...m, [vault.id]: { text, error } }))
      try {
        if (next) {
          const { sourceId, fileCount } = await knowledgeRegister(vault)
          try {
            await onSetKnowledge(vault.id, true, sourceId)
          } catch (e) {
            // Registered with the host but not recorded here — undo the
            // registration so the two sides cannot drift apart.
            await knowledgeUnregister(sourceId).catch(() => undefined)
            throw e
          }
          say(
            typeof fileCount === 'number'
              ? i18nT('apps.mdNotebook.settings.knowledgeAddedCount', { n: fileCount })
              : i18nT('apps.mdNotebook.settings.knowledgeAdded'),
          )
        } else {
          const priorSourceId = vault.knowledgeSourceId
          // Persist the disabled state FIRST, then delete the host source. If
          // the host delete fails we restore the enabled record, so the two
          // sides never drift into "host source gone but vault still marked
          // indexed with a stale source id".
          await onSetKnowledge(vault.id, false)
          try {
            await knowledgeUnregister(priorSourceId)
          } catch (e) {
            await onSetKnowledge(vault.id, true, priorSourceId ?? undefined).catch(() => undefined)
            throw e
          }
          say(i18nT('apps.mdNotebook.settings.knowledgeRemoved'))
        }
      } catch (e) {
        say(e instanceof Error ? e.message : String(e), true)
      } finally {
        setKnowledgeBusy(null)
      }
    },
    [onSetKnowledge],
  )

  // Escape leaves the page. No outside-click dismissal — this is a page, so a
  // click anywhere should not navigate away.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // Shortcut capture, in the capture phase so the pressed combination is
  // swallowed here rather than firing its normal action (or a sync).
  useEffect(() => {
    if (!recording) return
    onRecordingChange(true)
    setShortcutError(null)
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault()
      e.stopPropagation()
      if (e.key === 'Escape') {
        setRecording(false)
        return
      }
      // A bare letter would fire on every keystroke while typing a note.
      if (!e.metaKey && !e.ctrlKey && !e.altKey) {
        setShortcutError(i18nT('apps.mdNotebook.settings.shortcutNeedsModifier'))
        return
      }
      onSetShortcut({
        key: e.key.length === 1 ? e.key.toLowerCase() : e.key,
        meta: e.metaKey,
        ctrl: e.ctrlKey,
        alt: e.altKey,
        shift: e.shiftKey,
      })
      setShortcutError(null)
      setRecording(false)
    }
    window.addEventListener('keydown', onKey, true)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      onRecordingChange(false)
    }
  }, [recording, onSetShortcut, onRecordingChange])

  const pill = (primary: boolean): CSSProperties => ({
    background: primary ? ACCENT : 'transparent',
    color: primary ? ACCENT_FG : 'var(--muted)',
    border: primary ? 'none' : '1px solid var(--border)',
    padding: '5px 14px',
    borderRadius: primary ? '12px' : '8px',
    fontSize: '11px',
    fontWeight: 500,
    cursor: busy ? 'default' : 'pointer',
    flexShrink: 0,
    fontFamily: FONT_BODY,
  })

  const authStatus = hasPat
    ? i18nT('apps.mdNotebook.settings.authStored')
    : hasGhAuth
      ? i18nT('apps.mdNotebook.settings.authGh')
      : i18nT('apps.mdNotebook.settings.authNone')

  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
        minHeight: 0,
        fontFamily: FONT_BODY,
        color: 'var(--text)',
      }}
    >
      <div style={{ position: 'relative' }}>
        {/* Close pinned to the pane's right edge, matching the note controls. */}
        <button
          type="button"
          onClick={onClose}
          aria-label={i18nT('apps.mdNotebook.settings.close')}
          title={i18nT('apps.mdNotebook.settings.close')}
          style={{
            position: 'absolute',
            top: '24px',
            right: `${COLUMN_PAD_X}px`,
            zIndex: 2,
            width: '28px',
            height: '28px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            borderRadius: '8px',
            background: 'transparent',
            border: 'none',
            color: 'var(--muted)',
            cursor: 'pointer',
          }}
        >
          <X size={15} />
        </button>
        {/* Title in the same column and at the same scale as a note's title. */}
        <div
          style={{
            margin: '0 auto',
            width: '100%',
            maxWidth: `${COLUMN_MAX_WIDTH}px`,
            boxSizing: 'border-box',
            padding: `24px ${COLUMN_PAD_X}px 14px`,
          }}
        >
          <div style={{ fontSize: '23px', fontWeight: 700, lineHeight: 1.25 }}>
            {i18nT('apps.mdNotebook.settings.title')}
          </div>
          <div
            style={{
              fontSize: '11px',
              fontWeight: 400,
              color: 'var(--muted)',
              marginTop: '2px',
            }}
          >
            {i18nT('apps.mdNotebook.settings.subtitle')}
          </div>
        </div>
        <div style={{ borderTop: '1px solid var(--border)', margin: `0 ${COLUMN_PAD_X}px` }} />
      </div>

      {/* Body scrolls within the pane, in the same 800px column. */}
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
        <div
          style={{
            margin: '0 auto',
            width: '100%',
            maxWidth: `${COLUMN_MAX_WIDTH}px`,
            boxSizing: 'border-box',
            padding: `16px ${COLUMN_PAD_X}px 32px`,
            display: 'flex',
            flexDirection: 'column',
            gap: '20px',
          }}
        >
          {/* ---- vaults ---- */}
          <div>
            <div style={sectionLabel}>{i18nT('apps.mdNotebook.settings.vaultsSection')}</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {vaults.map(v => (
                <div
                  key={v.id}
                  // No accent tint: it sat behind the row's buttons and muted
                  // text and dropped their contrast. The active vault is marked
                  // by its "Active" label instead.
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '8px',
                    padding: '8px 10px',
                    borderRadius: '8px',
                    border: '1px solid var(--border)',
                    background: 'var(--bg)',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <span
                          style={{
                            fontSize: '13px',
                            fontWeight: 600,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {v.name}
                        </span>
                        <span
                          style={{
                            fontSize: '10px',
                            fontWeight: 500,
                            padding: '1px 6px',
                            borderRadius: '4px',
                            border: '1px solid var(--border)',
                            color: 'var(--muted)',
                            flexShrink: 0,
                          }}
                        >
                          {v.external
                            ? i18nT('apps.mdNotebook.settings.badgeLocal')
                            : i18nT('apps.mdNotebook.settings.badgeCloned')}
                        </span>
                        {v.id === activeVaultId && (
                          <span style={{ fontSize: '10px', color: ACCENT, flexShrink: 0 }}>
                            {i18nT('apps.mdNotebook.settings.active')}
                          </span>
                        )}
                      </div>
                      {/* Local path, prefixed with a folder icon. */}
                      <div
                        title={v.localPath}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: '5px',
                          fontSize: '11px',
                          color: 'var(--muted)',
                          marginTop: '2px',
                          minWidth: 0,
                        }}
                      >
                        <FolderIcon size={12} style={{ flexShrink: 0 }} />
                        <span
                          style={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {v.localPath}
                        </span>
                      </div>
                      {/* Repo · branch · subfolder — or, with no remote, the
                          branch alone under a git mark instead of the GitHub
                          one, since nothing about this vault is on GitHub. */}
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: '5px',
                          fontSize: '11px',
                          color: 'var(--muted)',
                          marginTop: '1px',
                          minWidth: 0,
                        }}
                      >
                        {v.localOnly ? <GitBranch size={12} /> : <GithubIcon size={12} />}
                        <span
                          style={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {`${
                            v.localOnly ? i18nT('apps.mdNotebook.settings.noRemote') : v.repo
                          } · ${v.branch}${v.subfolder ? ` · /${v.subfolder}` : ''}`}
                        </span>
                      </div>
                      {/* The deleted-notes folder is dotted, so it is invisible in
                          this app's listing AND in Finder's default view. This is
                          the only place a user can get to it without knowing the
                          path. Per-vault, because each vault has its own. */}
                      <div style={{ marginTop: '4px' }}>
                        <TextLink
                          label={i18nT('apps.mdNotebook.trash.open')}
                          onClick={() => void revealTrash(v)}
                          title={`${v.localPath}${v.subfolder ? `/${v.subfolder}` : ''}/.trash`}
                        />
                        {trashMsg[v.id] && (
                          <div
                            style={{ marginTop: '3px', fontSize: '11px', color: 'var(--muted)' }}
                          >
                            {trashMsg[v.id]}
                          </div>
                        )}
                      </div>
                    </div>
                    {v.id !== activeVaultId && (
                      <button
                        type="button"
                        onClick={() => onSwitchVault(v.id)}
                        style={pill(false)}
                      >
                        {i18nT('apps.mdNotebook.settings.open')}
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => setConfirmForget(v.id)}
                      style={{
                        ...pill(false),
                        color: 'var(--danger)',
                        borderColor: 'var(--danger)',
                      }}
                    >
                      {i18nT('apps.mdNotebook.settings.remove')}
                    </button>
                  </div>

                  {/* Per-vault knowledge sync, with its own busy and message state
                      so one vault's failure does not blank the section. */}
                  <div style={{ borderTop: '1px solid var(--border)', paddingTop: '8px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '12px' }}>
                          {i18nT('apps.mdNotebook.knowledge.label')}
                        </div>
                        <div
                          style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '1px' }}
                        >
                          {knowledgeBusy === v.id
                            ? v.knowledge
                              ? i18nT('apps.mdNotebook.settings.knowledgeRemoving')
                              : i18nT('apps.mdNotebook.settings.knowledgeAdding')
                            : v.knowledge
                              ? i18nT('apps.mdNotebook.settings.knowledgeIndexed')
                              : i18nT('apps.mdNotebook.settings.knowledgeNotIndexed')}
                        </div>
                      </div>
                      <Switch
                        on={Boolean(v.knowledge)}
                        disabled={knowledgeBusy === v.id}
                        label={i18nT('apps.mdNotebook.knowledge.label')}
                        onChange={next => void toggleKnowledge(v, next)}
                      />
                    </div>
                    {/* The result sits directly under the toggle that produced it
                        and pushes the rest of the list down. */}
                    {knowledgeMsg[v.id] && (
                      <div
                        role={knowledgeMsg[v.id]?.error ? 'alert' : 'status'}
                        style={{
                          marginTop: '8px',
                          padding: '6px 8px',
                          borderRadius: '6px',
                          fontSize: '11px',
                          lineHeight: 1.45,
                          background: knowledgeMsg[v.id]?.error
                            ? 'var(--danger-subtle)'
                            : ACCENT_BG,
                          color: knowledgeMsg[v.id]?.error ? 'var(--danger)' : ACCENT,
                        }}
                      >
                        {knowledgeMsg[v.id]?.text}
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* Removal is non-destructive, and the copy says so. */}
              {confirmForget && (
                <div
                  style={{
                    padding: '8px 10px',
                    borderRadius: '8px',
                    background: 'var(--danger-subtle)',
                    color: 'var(--danger)',
                    fontSize: '11px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '10px',
                    flexWrap: 'wrap',
                  }}
                >
                  <span style={{ flex: 1, minWidth: '180px' }}>
                    {i18nT('apps.mdNotebook.settings.removeConfirm')}
                  </span>
                  <button
                    type="button"
                    onClick={async () => {
                      const id = confirmForget
                      setConfirmForget(null)
                      setBusy(true)
                      await onForget(id)
                      setBusy(false)
                    }}
                    style={{
                      ...pill(false),
                      color: 'var(--danger)',
                      borderColor: 'var(--danger)',
                    }}
                  >
                    {i18nT('apps.mdNotebook.settings.removeIt')}
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmForget(null)}
                    style={pill(false)}
                  >
                    {i18nT('apps.mdNotebook.connect.cancel')}
                  </button>
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={onConnect}
              style={{ ...pill(false), marginTop: '8px' }}
            >
              {i18nT('apps.mdNotebook.panel.connectVault')}
            </button>
          </div>

          {/* ---- github access ---- */}
          <div>
            <div style={sectionLabel}>{i18nT('apps.mdNotebook.settings.githubSection')}</div>
            <div style={{ fontSize: '11px', color: 'var(--muted)', marginBottom: '6px' }}>
              {authStatus}
            </div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <input
                type="password"
                value={pat}
                onChange={e => setPat(e.target.value)}
                aria-label={i18nT('apps.mdNotebook.connect.token')}
                placeholder={
                  hasPat
                    ? i18nT('apps.mdNotebook.settings.tokenPlaceholderStored')
                    : i18nT('apps.mdNotebook.settings.tokenPlaceholderEmpty')
                }
                style={{ ...field, flex: 1, minWidth: 0 }}
              />
              <button
                type="button"
                disabled={busy || !pat}
                onClick={async () => {
                  setBusy(true)
                  await onSetPat(pat)
                  setPat('')
                  setPatNote(i18nT('apps.mdNotebook.settings.tokenSaved'))
                  setBusy(false)
                }}
                style={pill(true)}
              >
                {i18nT('apps.mdNotebook.settings.save')}
              </button>
              {hasPat && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true)
                    await onSetPat('')
                    setPatNote(i18nT('apps.mdNotebook.settings.tokenCleared'))
                    setBusy(false)
                  }}
                  style={pill(false)}
                >
                  {i18nT('apps.mdNotebook.settings.clear')}
                </button>
              )}
            </div>
            {/* Result under the token controls, not at the foot of the page. */}
            {patNote && (
              <div
                role="status"
                style={{ fontSize: '11px', color: ACCENT, marginTop: '6px' }}
              >
                {patNote}
              </div>
            )}
          </div>

          {/* ---- sync ---- */}
          <div>
            <div style={sectionLabel}>{i18nT('apps.mdNotebook.settings.syncSection')}</div>
            {/* Autosave first: it is on by default and it is what most people
                mean by "save", so it reads before the remote-facing option. */}
            <div
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: '10px',
                marginBottom: '16px',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '13px', fontWeight: 500 }}>
                  {i18nT('apps.mdNotebook.settings.autoCommit')}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
                  {i18nT('apps.mdNotebook.settings.autoCommitHelp', { n: AUTO_COMMIT_MINS })}
                </div>
              </div>
              <Switch
                on={autoCommit}
                onChange={onAutoCommit}
                label={i18nT('apps.mdNotebook.settings.autoCommit')}
              />
            </div>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '13px', fontWeight: 500 }}>
                  {i18nT('apps.mdNotebook.settings.autoSync')}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
                  {i18nT('apps.mdNotebook.settings.autoSyncHelp')}
                </div>
              </div>
              <Switch
                on={autoSync}
                onChange={onAutoSync}
                label={i18nT('apps.mdNotebook.settings.autoSync')}
              />
            </div>
            {/* The interval only means anything once auto sync is on. */}
            {autoSync && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '8px',
                  marginTop: '10px',
                  paddingLeft: '12px',
                  borderLeft: `2px solid ${ACCENT_BG}`,
                }}
              >
                <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
                  {i18nT('apps.mdNotebook.settings.every')}
                </span>
                <input
                  type="number"
                  min={MIN_AUTO_SYNC_MINS}
                  max={MAX_AUTO_SYNC_MINS}
                  value={autoSyncMins}
                  aria-label={i18nT('apps.mdNotebook.settings.intervalAria')}
                  onChange={e => onAutoSyncMins(Number(e.target.value))}
                  style={{ ...field, width: '68px', textAlign: 'center' }}
                />
                <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
                  {i18nT('apps.mdNotebook.settings.minutes')}
                </span>
              </div>
            )}

            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                marginTop: '16px',
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '13px', fontWeight: 500 }}>
                  {i18nT('apps.mdNotebook.settings.shortcut')}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
                  {recording
                    ? i18nT('apps.mdNotebook.settings.shortcutHelpRecording')
                    : i18nT('apps.mdNotebook.settings.shortcutHelpIdle')}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setRecording(r => !r)}
                aria-label={i18nT('apps.mdNotebook.settings.shortcut')}
                style={{
                  flexShrink: 0,
                  minWidth: '78px',
                  padding: '5px 12px',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  fontFamily: FONT_MONO,
                  fontSize: '12px',
                  border: `1px solid ${recording ? ACCENT : 'var(--border)'}`,
                  background: recording ? ACCENT_BG : 'var(--bg)',
                  color: recording ? ACCENT : 'var(--text)',
                }}
              >
                {recording
                  ? i18nT('apps.mdNotebook.settings.recording')
                  : formatShortcut(shortcut)}
              </button>
              {formatShortcut(shortcut) !== formatShortcut(DEFAULT_SYNC_SHORTCUT) && (
                <button
                  type="button"
                  onClick={() => {
                    setRecording(false)
                    onSetShortcut(DEFAULT_SYNC_SHORTCUT)
                  }}
                  style={pill(false)}
                >
                  {i18nT('apps.mdNotebook.settings.reset')}
                </button>
              )}
            </div>
            {shortcutError && (
              <div
                style={{ fontSize: '11px', color: 'var(--danger)', marginTop: '6px' }}
              >
                {shortcutError}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** The panel's bottom Settings row. Shows a selected state while it is open. */
export function SettingsBar({
  open,
  onOpen,
}: {
  open: boolean
  onOpen: () => void
}) {
  return (
    <div
      style={{
        flexShrink: 0,
        marginTop: '4px',
        borderTop: '1px solid var(--border-strong)',
        paddingTop: '8px',
        marginBottom: '8px',
      }}
    >
      <Clickable
        className="mdnb-row"
        aria-label={i18nT('apps.mdNotebook.settings.title')}
        aria-current={open ? 'page' : undefined}
        onClick={onOpen}
        // Selected state matches a note row — same accent-subtle fill, radius
        // and full-strength label, since Settings is a peer destination. The
        // separator lives on the wrapper so the fill stays a clean row, and the
        // 8px side margins line it up with the list's own padding.
        //
        // The 2px vertical padding is load-bearing, not cosmetic: it makes the
        // row 28px, so the wrapper's separator lands on the SAME baseline as the
        // dashboard left-nav's community-row separator ("Star us · Report issue").
        // Both cards share a grid row and both end 8px above the pane, so the
        // budget below this border must match the nav's: 8px gap + row + 8px
        // margin = 44px there too (nav: 10 + 24 + 10). Changing this padding, the
        // gear size, or either 8px moves the line out of alignment with the nav.
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '4px',
          padding: '2px 4px 2px 12px',
          margin: '0 8px',
          borderRadius: '8px',
          whiteSpace: 'nowrap',
          cursor: 'pointer',
          ...(open ? { background: ACCENT_BG } : null),
        }}
      >
        <span
          style={{
            fontSize: '13px',
            fontWeight: open ? 600 : 400,
            color: open ? 'var(--text)' : 'var(--muted)',
            flex: 1,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {i18nT('apps.mdNotebook.settings.title')}
        </span>
        {/* Gear pinned right, as its own button with the icon-button hover
            treatment — the row itself is already the click target, so this
            stops propagation rather than firing twice. */}
        <button
          type="button"
          title={i18nT('apps.mdNotebook.settings.title')}
          aria-label={i18nT('apps.mdNotebook.settings.title')}
          onClick={e => {
            e.stopPropagation()
            onOpen()
          }}
          onMouseEnter={e => {
            e.currentTarget.style.background = 'var(--bg-hover)'
            e.currentTarget.style.color = 'var(--text)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.background = 'transparent'
            e.currentTarget.style.color = open ? 'var(--text)' : 'var(--muted)'
          }}
          style={{
            width: '24px',
            height: '24px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            borderRadius: '8px',
            color: open ? 'var(--text)' : 'var(--muted)',
            background: 'transparent',
            border: 'none',
            cursor: 'pointer',
            flexShrink: 0,
            transition: 'color .15s, background .15s',
          }}
        >
          <Settings size={15} />
        </button>
      </Clickable>
    </div>
  )
}
