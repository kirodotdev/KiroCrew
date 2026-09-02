/** Connect a vault: clone a GitHub repo, or attach a folder already on disk. */
import { useState } from 'react'
import type { CSSProperties } from 'react'
import { FolderOpen } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { ACCENT, ACCENT_FG, FONT_BODY } from './constants'
import { ApiError, knowledgeRegister, notesApi } from './api'
import { Switch } from './bits'
import type { Vault } from './types'

const field: CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  fontSize: '12px',
  padding: '7px 10px',
  borderRadius: '6px',
  border: '1px solid var(--border)',
  background: 'var(--bg)',
  color: 'var(--text)',
  outline: 'none',
  marginTop: '4px',
}

const label: CSSProperties = {
  fontSize: '11px',
  color: 'var(--muted)',
  display: 'block',
  marginTop: '12px',
}

export function ConnectVault({
  onConnected,
  onCancel,
}: {
  onConnected: (v: Vault) => void
  onCancel?: (() => void) | null
}) {
  const [mode, setMode] = useState<'clone' | 'attach'>('clone')
  const [url, setUrl] = useState('')
  const [folder, setFolder] = useState('')
  const [pat, setPat] = useState('')
  const [branch, setBranch] = useState('main')
  const [subfolder, setSubfolder] = useState('')
  const [knowledge, setKnowledge] = useState(false)
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const attaching = mode === 'attach'

  const segment = (active: boolean): CSSProperties => ({
    flex: 1,
    padding: '6px 10px',
    borderRadius: '8px',
    border: 'none',
    cursor: 'pointer',
    fontSize: '12px',
    fontWeight: 500,
    fontFamily: FONT_BODY,
    background: active ? ACCENT : 'transparent',
    color: active ? ACCENT_FG : 'var(--muted)',
  })

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const sub = subfolder.replace(/\/$/, '') || undefined
      const { vault } = attaching
        ? await notesApi.attachVault({ path: folder.trim(), subfolder: sub, knowledge })
        : await notesApi.cloneVault({ url, pat: pat || undefined, branch, subfolder: sub, knowledge })
      // Register with the Knowledge library only once the vault exists on disk.
      // A failure here must not lose the vault, so it clears the flag and reports.
      if (knowledge) {
        try {
          const { sourceId } = await knowledgeRegister(vault)
          await notesApi.setVaultKnowledge(vault.id, true, sourceId)
        } catch (e) {
          await notesApi.setVaultKnowledge(vault.id, false).catch(() => undefined)
          setError(
            i18nT('apps.mdNotebook.connect.knowledgeFailed', {
              message: e instanceof Error ? e.message : String(e),
            }),
          )
        }
      }
      onConnected(vault)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function choose() {
    setPicking(true)
    setError(null)
    try {
      const { path, cancelled } = await notesApi.pickFolder()
      if (!cancelled && path) setFolder(path)
    } catch (e) {
      // 501 = not macOS; anything else is a real failure. Either way the user
      // can still type the path in.
      setError(
        e instanceof ApiError && e.status === 501
          ? i18nT('apps.mdNotebook.connect.pickerNeedsMac')
          : i18nT('apps.mdNotebook.connect.pickerFailed', {
              message: e instanceof Error ? e.message : String(e),
            }),
      )
    } finally {
      setPicking(false)
    }
  }

  return (
    // Centred both ways. minHeight 100% keeps a tall form scrolling rather than
    // clipping on a short pane.
    <div
      style={{
        minHeight: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
        boxSizing: 'border-box',
        fontFamily: FONT_BODY,
        color: 'var(--text)',
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: '440px',
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: '6px',
          padding: '20px',
        }}
      >
        <div style={{ fontSize: '18px', fontWeight: 600 }}>
          {i18nT('apps.mdNotebook.connect.title')}
        </div>

        <div
          style={{
            display: 'flex',
            gap: '4px',
            marginTop: '14px',
            padding: '4px',
            background: 'var(--card)',
            borderRadius: '10px',
          }}
        >
          <button type="button" style={segment(!attaching)} onClick={() => setMode('clone')}>
            {i18nT('apps.mdNotebook.connect.cloneTab')}
          </button>
          <button type="button" style={segment(attaching)} onClick={() => setMode('attach')}>
            {i18nT('apps.mdNotebook.connect.attachTab')}
          </button>
        </div>

        <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '8px' }}>
          {attaching
            ? i18nT('apps.mdNotebook.connect.attachHelp')
            : i18nT('apps.mdNotebook.connect.cloneHelp')}
        </div>
        {/* A folder with no git remote is a supported case, not an error — say so
            here rather than letting the user discover it by failing to attach. */}
        {attaching && (
          <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '4px' }}>
            {i18nT('apps.mdNotebook.connect.attachNoRemoteHelp')}
          </div>
        )}

        {attaching ? (
          <label style={label} htmlFor="mdnb-localFolder">
            {i18nT('apps.mdNotebook.connect.localFolder')}
            <div
              style={{ display: 'flex', gap: '8px', alignItems: 'stretch', marginTop: '4px' }}
            >
              {/* The field stays editable — typing or pasting a path still
                  works, and it is the only route when the chooser is absent. */}
              <input
                id="mdnb-localFolder"
                aria-label={i18nT('apps.mdNotebook.connect.localFolder')}
                style={{ ...field, marginTop: 0, flex: 1, minWidth: 0 }}
                value={folder}
                onChange={e => setFolder(e.target.value)}
                placeholder={i18nT('apps.mdNotebook.connect.localFolderPlaceholder')}
              />
              <button
                type="button"
                disabled={picking}
                onClick={choose}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  background: 'transparent',
                  color: 'var(--muted)',
                  border: '1px solid var(--border)',
                  padding: '0 12px',
                  borderRadius: '8px',
                  fontSize: '11px',
                  fontWeight: 500,
                  cursor: picking ? 'default' : 'pointer',
                  flexShrink: 0,
                  whiteSpace: 'nowrap',
                }}
              >
                <FolderOpen size={13} />
                {picking
                  ? i18nT('apps.mdNotebook.connect.choosing')
                  : i18nT('apps.mdNotebook.connect.choose')}
              </button>
            </div>
          </label>
        ) : (
          <>
            <label style={label} htmlFor="mdnb-repoUrl">
              {i18nT('apps.mdNotebook.connect.repoUrl')}
              <input
                id="mdnb-repoUrl"
                style={field}
                value={url}
                onChange={e => setUrl(e.target.value)}
                placeholder={i18nT('apps.mdNotebook.connect.repoUrlPlaceholder')}
              />
            </label>
            <label style={label} htmlFor="mdnb-branch">
              {i18nT('apps.mdNotebook.connect.branch')}
              <input id="mdnb-branch" style={field} value={branch} onChange={e => setBranch(e.target.value)} />
            </label>
            <label style={label} htmlFor="mdnb-token">
              {i18nT('apps.mdNotebook.connect.token')}
              <input
                id="mdnb-token"
                style={field}
                type="password"
                value={pat}
                onChange={e => setPat(e.target.value)}
                placeholder={i18nT('apps.mdNotebook.connect.tokenPlaceholder')}
              />
            </label>
          </>
        )}

        <label style={label}>
          {i18nT('apps.mdNotebook.connect.subfolder')}
          <input
            id="mdnb-subfolder"
            style={field}
            value={subfolder}
            onChange={e => setSubfolder(e.target.value)}
            placeholder={i18nT('apps.mdNotebook.connect.subfolderPlaceholder')}
          />
        </label>

        {/* Knowledge sync is off by default: it indexes every note in the vault,
            so it should be opt-in. */}
        <div
          style={{ display: 'flex', alignItems: 'flex-start', gap: '10px', marginTop: '16px' }}
        >
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: '13px', fontWeight: 500 }}>
              {i18nT('apps.mdNotebook.knowledge.label')}
            </div>
            <div style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}>
              {i18nT('apps.mdNotebook.knowledge.connectHelp')}
            </div>
          </div>
          <Switch
            on={knowledge}
            onChange={setKnowledge}
            label={i18nT('apps.mdNotebook.knowledge.label')}
          />
        </div>

        {error && (
          <div style={{ marginTop: '10px', fontSize: '11px', color: 'var(--danger)' }}>
            {error}
          </div>
        )}

        <button
          type="button"
          disabled={busy || (attaching ? !folder : !url)}
          onClick={submit}
          style={{
            marginTop: '16px',
            background: busy ? 'transparent' : ACCENT,
            color: busy ? 'var(--muted)' : ACCENT_FG,
            border: 'none',
            padding: '7px 18px',
            borderRadius: '12px',
            fontSize: '11px',
            fontWeight: 500,
            cursor: busy ? 'default' : 'pointer',
          }}
        >
          {busy
            ? attaching
              ? i18nT('apps.mdNotebook.connect.attaching')
              : i18nT('apps.mdNotebook.connect.cloning')
            : attaching
              ? i18nT('apps.mdNotebook.connect.attachAction')
              : i18nT('apps.mdNotebook.connect.cloneAction')}
        </button>

        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            style={{
              marginTop: '16px',
              marginLeft: '8px',
              background: 'transparent',
              color: 'var(--muted)',
              border: '1px solid var(--border)',
              padding: '7px 14px',
              borderRadius: '8px',
              fontSize: '11px',
              fontWeight: 500,
              cursor: 'pointer',
            }}
          >
            {i18nT('apps.mdNotebook.connect.cancel')}
          </button>
        )}
      </div>
    </div>
  )
}
