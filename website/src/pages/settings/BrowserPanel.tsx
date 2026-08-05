import { useState, useCallback } from 'react'
import { ExternalLink, Check } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
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
      setError(i18nT('pages.settings.browserPanel.cannot_reach_gateway_is_it_running'))
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

  if (isLoading) return <p style={{ fontSize: 13, color: 'var(--muted)', padding: 16 }}>{i18nT('pages.settings.browserPanel.loading_browser_config')}</p>
  if (isError) return <p style={{ fontSize: 13, color: 'var(--error)', padding: 16 }}>{i18nT('pages.settings.browserPanel.cannot_load_browser_config_is_the_gateway_runnin')}</p>

  return (
    <>
      <SettingsSection title={i18nT('pages.settings.browserPanel.browser_mode')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.browserPanel.chrome_extension_mode')}
            description={i18nT('pages.settings.browserPanel.attach_to_your_running_chrome_with_all_existing')}
            checked={extensionMode}
            onChange={handleToggle}
          />
          {!extensionMode && (
            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
              {i18nT('pages.settings.browserPanel.headless_mode_active_browser_uses_cookie_injecti')}
            </p>
          )}
        </SettingsCard>
      </SettingsSection>

      {extensionMode && (
        <SettingsSection title={i18nT('pages.settings.browserPanel.extension_token')}>
          <SettingsCard>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <p style={{ fontSize: 13, color: 'var(--text)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.1_install_the')}{' '}
                <a
                  href="https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm"
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: 'var(--accent)' }}
                >
                  {i18nT('pages.settings.browserPanel.playwright_chrome_extension')} <ExternalLink size={12} style={{ display: 'inline' }} />
                </a>
              </p>
              <p style={{ fontSize: 13, color: 'var(--text)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.2_click_the_extension_icon_in_chrome_and_copy_th')}
              </p>
              <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                <div style={{ flex: 1 }}>
                  <SettingsInput
                    label={i18nT('pages.settings.browserPanel.connection_token')}
                    description={i18nT('pages.settings.browserPanel.paste_playwright_mcp_extension_token_value_from')}
                    value={displayToken}
                    onChange={setToken}
                    placeholder={i18nT('pages.settings.browserPanel.paste_token_here')}
                  />
                </div>
                <button
                  onClick={handleSave}
                  disabled={!token || token === '••••••••' || saveMut.isPending}
                  className="px-4 py-2 text-[13px] font-medium rounded border border-border bg-card hover:bg-bg-hover disabled:opacity-50 transition-colors"
                  style={{ color: 'var(--text)', marginBottom: 4 }}
                >
                  {saveMut.isPending ? i18nT('pages.settings.browserPanel.saving') : i18nT('pages.settings.browserPanel.save')}
                </button>
              </div>
              {/*
                Deliberately NOT opted in to the agent hand-off: the token being
                saved lives in local state (`useState('')`, never persisted) and
                came out of the Chrome extension, so navigating to the chat would
                send the user back there to fetch it again.
              */}
              <ErrorNotice message={error} variant="inline" />
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {!extensionMode && showExtension === false && config?.extension_mode && (
        <SettingsSection title="">
          <SettingsCard>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
              <p style={{ fontSize: 12, color: 'var(--muted)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.switch_to_headless_mode_this_will_remove_the_sav')}
              </p>
              <button
                onClick={handleSave}
                disabled={saveMut.isPending}
                className="px-4 py-2 text-[13px] font-medium rounded border border-border bg-card hover:bg-bg-hover disabled:opacity-50 transition-colors"
                style={{ color: 'var(--text)' }}
              >
                {saveMut.isPending ? i18nT('pages.settings.browserPanel.saving') : i18nT('pages.settings.browserPanel.confirm')}
              </button>
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {saved && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--success)', padding: 16 }}>
          <Check size={14} />
          <span style={{ fontSize: 12 }}>{i18nT('pages.settings.browserPanel.saved_and_applied_sessions_restarted')}</span>
        </div>
      )}
    </>
  )
}
