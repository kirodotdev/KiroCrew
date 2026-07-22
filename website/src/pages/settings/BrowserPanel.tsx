import { useState, useCallback } from 'react'
import { ExternalLink, Check, AlertTriangle } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { api } from '../../api/client'

type BrowserConfig = { extension_mode: boolean; token: boolean }

export function BrowserPanel() {
  const [token, setToken] = useState('')
  const [showExtension, setShowExtension] = useState<boolean | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const qc = useQueryClient()

  const { data: config, isLoading, isError } = useQuery<BrowserConfig>({
    queryKey: ['browser-config'],
    queryFn: api.getBrowserConfig,
    retry: false,
  })

  const saveMut = useMutation({
    mutationFn: async (body: { extension_mode: boolean; token: string }) => {
      await api.saveBrowserConfig(body)
      await api.restartSessions()
    },
    onError: () => {
      setError('Cannot reach gateway. Is it running?')
      setTimeout(() => setError(''), 5000)
    },
    onSuccess: () => {
      setSaved(true)
      setTimeout(() => setSaved(false), 4000)
      qc.invalidateQueries({ queryKey: ['browser-config'] })
    },
  })

  const extensionMode = showExtension ?? config?.extension_mode ?? false
  const displayToken = token || (config?.extension_mode && config?.token ? '••••••••' : '')

  const handleToggle = useCallback((enabled: boolean) => {
    setError('')
    setSaved(false)
    if (!enabled) {
      setToken('')
    } else if (config?.token) {
      setToken('••••••••')
    }
    setShowExtension(enabled)
  }, [config?.token])

  const handleSave = useCallback(() => {
    setError('')
    if (extensionMode) {
      if (!token || token === '••••••••') return
      let cleanToken = token.trim()
      if (cleanToken.startsWith('PLAYWRIGHT_MCP_EXTENSION_TOKEN=')) {
        cleanToken = cleanToken.substring(cleanToken.indexOf('=') + 1)
      }
      saveMut.mutate({ extension_mode: true, token: cleanToken }, {
        onSuccess: () => setToken('••••••••'),
      })
    } else {
      saveMut.mutate({ extension_mode: false, token: '' })
    }
  }, [extensionMode, token, saveMut])

  if (isLoading) return <p style={{ fontSize: 13, color: 'var(--muted)', padding: 16 }}>Loading browser config...</p>
  if (isError) return <p style={{ fontSize: 13, color: 'var(--error)', padding: 16 }}>Cannot load browser config. Is the gateway running?</p>

  return (
    <>
      <SettingsSection title="Browser Mode">
        <SettingsCard>
          <SettingsToggle
            label="Chrome Extension Mode"
            description="Attach to your running Chrome with all existing logins and sessions. Recommended for macOS."
            checked={extensionMode}
            onChange={handleToggle}
          />
          {!extensionMode && (
            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
              Headless mode active — browser uses cookie injection via storage state.
            </p>
          )}
        </SettingsCard>
      </SettingsSection>

      {extensionMode && (
        <SettingsSection title="Extension Token">
          <SettingsCard>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <p style={{ fontSize: 13, color: 'var(--text)', margin: 0 }}>
                1. Install the{' '}
                <a
                  href="https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm"
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: 'var(--accent)' }}
                >
                  Playwright Chrome Extension <ExternalLink size={12} style={{ display: 'inline' }} />
                </a>
              </p>
              <p style={{ fontSize: 13, color: 'var(--text)', margin: 0 }}>
                2. Click the extension icon in Chrome and copy the token
              </p>
              <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                <div style={{ flex: 1 }}>
                  <SettingsInput
                    label="Connection Token"
                    description="Paste PLAYWRIGHT_MCP_EXTENSION_TOKEN value from the extension popup"
                    value={displayToken}
                    onChange={setToken}
                    placeholder="Paste token here..."
                  />
                </div>
                <button
                  onClick={handleSave}
                  disabled={!token || token === '••••••••' || saveMut.isPending}
                  className="px-4 py-2 text-[13px] font-medium rounded border border-border bg-card hover:bg-bg-hover disabled:opacity-50 transition-colors"
                  style={{ color: 'var(--text)', marginBottom: 4 }}
                >
                  {saveMut.isPending ? 'Saving...' : 'Save'}
                </button>
              </div>
              {error && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--error)' }}>
                  <AlertTriangle size={14} />
                  <span style={{ fontSize: 12 }}>{error}</span>
                </div>
              )}
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {!extensionMode && showExtension === false && config?.extension_mode && (
        <SettingsSection title="">
          <SettingsCard>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
              <p style={{ fontSize: 12, color: 'var(--muted)', margin: 0 }}>
                Switch to headless mode? This will remove the saved extension token.
              </p>
              <button
                onClick={handleSave}
                disabled={saveMut.isPending}
                className="px-4 py-2 text-[13px] font-medium rounded border border-border bg-card hover:bg-bg-hover disabled:opacity-50 transition-colors"
                style={{ color: 'var(--text)' }}
              >
                {saveMut.isPending ? 'Saving...' : 'Confirm'}
              </button>
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {saved && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--success)', padding: 16 }}>
          <Check size={14} />
          <span style={{ fontSize: 12 }}>Saved and applied. Sessions restarted.</span>
        </div>
      )}
    </>
  )
}
