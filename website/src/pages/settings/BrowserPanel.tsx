import { useState, useCallback } from 'react'
import { ExternalLink, Check, Loader2, Info, AlertTriangle } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsInput } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'

type BrowserConfig = {
  enabled: boolean
  engine: string
  engines: string[]
  extension_mode: boolean
  token: boolean
  installed: boolean
}
type InstallResult = {
  ok: boolean
  step: string
  detail: string
  engine: string
  // Present on an attempted-and-failed browser download: a copy-pasteable manual
  // fallback command, and a sanitized one-line cause (allowlisted error label,
  // never raw stderr).
  manual_command?: string
  reason?: string
}
type SaveResult = {
  ok: boolean
  mcp_status?: string
  enabled?: boolean
  engine?: string
  install?: InstallResult
}

// Microsoft's official "Playwright Extension" on the Chrome Web Store. It
// installs into any Chromium-family browser (Chrome, Edge, Brave, Arc, Opera all
// accept Chrome Web Store extensions); Microsoft publishes no separate Edge
// Add-ons listing, so a single verified link covers the family. Firefox and
// Safari are intentionally absent: Playwright ships no attach extension for them.
const PLAYWRIGHT_EXTENSION_URL =
  'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm'

// Human labels for the launch engines. firefox/webkit are Playwright's OWN
// browser builds (not the user's Firefox/Safari and without their logins), so
// the copy names that honestly rather than implying it drives their install.
const ENGINE_LABEL_KEY: Record<string, string> = {
  chromium: 'pages.settings.browserPanel.engine_chromium',
  firefox: 'pages.settings.browserPanel.engine_firefox',
  webkit: 'pages.settings.browserPanel.engine_webkit',
}

export function BrowserPanel() {
  const [token, setToken] = useState('')
  const [showExtension, setShowExtension] = useState<boolean | null>(null)
  const [enabledOverride, setEnabledOverride] = useState<boolean | null>(null)
  const [engineOverride, setEngineOverride] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const [install, setInstall] = useState<InstallResult | null>(null)
  const qc = useQueryClient()

  const { data: config, isLoading, isError } = useQuery<BrowserConfig>({
    queryKey: ['browser-config'],
    queryFn: api.getBrowserConfig,
    retry: false,
  })

  const saveMut = useMutation({
    mutationFn: async (body: { enabled: boolean; engine: string; extension_mode: boolean; token: string }) => {
      const res = (await api.saveBrowserConfig(body)) as SaveResult
      await api.restartSessions()
      return res
    },
    onError: () => {
      setError(i18nT('pages.settings.browserPanel.cannot_reach_gateway_is_it_running'))
      setTimeout(() => setError(''), 5000)
    },
    onSuccess: (res: SaveResult) => {
      // Never leave the raw extension token sitting in the input after a save:
      // mask it once it has been persisted (it is write-only server-side and is
      // a credential). An empty field stays empty.
      setToken((t) => (t ? '••••••••' : t))
      // Keep any provisioning note (deferred download, or a setup hint) to show as
      // a MUTED advisory — never an error. Browser Mode is on regardless, so a
      // note with `ok:true` (e.g. "downloads on first use") still shows the saved
      // tick alongside; only a genuinely-not-usable note (`ok:false`: no Node/npm)
      // withholds the tick, but it is still styled as guidance, not a failure.
      setInstall(res.install && res.install.detail ? res.install : null)
      if (!res.install || res.install.ok) {
        setSaved(true)
        setTimeout(() => setSaved(false), 4000)
      }
      qc.invalidateQueries({ queryKey: ['browser-config'] })
    },
  })

  const enabled = enabledOverride ?? config?.enabled ?? false
  const engine = engineOverride ?? config?.engine ?? 'chromium'
  const engines = config?.engines ?? ['chromium', 'firefox', 'webkit']
  const extensionMode = showExtension ?? config?.extension_mode ?? false
  const displayToken = token || (config?.extension_mode && config?.token ? '••••••••' : '')

  const persist = useCallback(
    (next: { enabled: boolean; engine: string; extension_mode: boolean; token: string }) => {
      setError('')
      setInstall(null)
      saveMut.mutate(next)
    },
    [saveMut],
  )

  // Enable and engine saves are orthogonal to the attach flow, so they carry the
  // PERSISTED extension mode (config.extension_mode), never the transient display
  // toggle, and send an empty token. The backend keeps the stored token when
  // extension_mode stays true and no token is supplied — so these saves never
  // silently delete a saved token (only handleConfirmHeadless turns attach off).
  const persistedExtension = config?.extension_mode ?? false

  // Flipping the main enable saves immediately: enabling downloads Playwright and
  // the engine browser (reported via the install result), disabling tears the
  // capability down.
  const handleEnableToggle = useCallback(
    (next: boolean) => {
      setEnabledOverride(next)
      setSaved(false)
      persist({ enabled: next, engine, extension_mode: persistedExtension, token: '' })
    },
    [persist, engine, persistedExtension],
  )

  const handleEngineSelect = useCallback(
    (next: string) => {
      if (next === engine) return
      setEngineOverride(next)
      persist({ enabled, engine: next, extension_mode: persistedExtension, token: '' })
    },
    [persist, enabled, engine, persistedExtension],
  )

  const handleExtensionToggle = useCallback((on: boolean) => {
    setError('')
    setSaved(false)
    setShowExtension(on)
    if (on) {
      // Persist attach mode immediately — the token is optional (it only skips
      // the per-connection approval), so waiting for one would let the toggle
      // silently revert on reload. Carry the existing token, if any.
      if (config?.token) setToken('••••••••')
      const cleanToken = token && token !== '••••••••' ? token.trim() : ''
      persist({ enabled, engine, extension_mode: true, token: cleanToken })
    } else {
      // Turning attach off is a token-deleting change; confirm via the headless
      // switch card (handleConfirmHeadless) rather than persisting eagerly here.
      setToken('')
    }
  }, [persist, enabled, engine, token, config?.token])

  const handleSaveExtension = useCallback(() => {
    if (!token || token === '••••••••') return
    let cleanToken = token.trim()
    if (cleanToken.startsWith('PLAYWRIGHT_MCP_EXTENSION_TOKEN=')) {
      cleanToken = cleanToken.substring(cleanToken.indexOf('=') + 1)
    }
    persist({ enabled, engine, extension_mode: true, token: cleanToken })
  }, [persist, enabled, engine, token])

  const handleConfirmHeadless = useCallback(() => {
    persist({ enabled, engine, extension_mode: false, token: '' })
  }, [persist, enabled, engine])

  if (isLoading) return <p style={{ fontSize: 13, color: 'var(--muted)', padding: 16 }}>{i18nT('pages.settings.browserPanel.loading_browser_config')}</p>
  if (isError) return <p style={{ fontSize: 13, color: 'var(--danger)', padding: 16 }}>{i18nT('pages.settings.browserPanel.cannot_load_browser_config_is_the_gateway_runnin')}</p>

  return (
    <>
      <SettingsSection title={i18nT('pages.settings.browserPanel.browser_mode')}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('pages.settings.browserPanel.enable_browser_mode')}
            description={i18nT('pages.settings.browserPanel.let_the_agent_read_and_operate_web_pages')}
            checked={enabled}
            onChange={handleEnableToggle}
            disabled={saveMut.isPending}
          />
          {saveMut.isPending && (
            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Loader2 size={12} className="lucide-inline animate-spin" />
              {i18nT('pages.settings.browserPanel.setting_up_the_browser_this_can_take_a_minute')}
            </p>
          )}
          {/*
            Provisioning is ALWAYS advisory, never an error surface: enabling
            Browser Mode registers the proxy regardless, and the browser downloads
            on first use. So any `install.detail` (a soft "downloads on first use"
            note, or a calm "install Node to finish setup" hint) renders as a muted
            info line, not a red alert — the user is never shown a raw install
            failure. Shown whenever the server returned a detail to convey.
          */}
          {install && install.detail && (
            // A not-yet-usable failure (ok:false) is visually differentiated from
            // the benign "downloads on first use" note (ok:true): a warning icon +
            // full-strength text, versus the muted info row — so an operator
            // scanning the panel can tell "act" from "fine, later" without reading
            // the whole paragraph. Still calm (no red), still never a raw stderr dump.
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8, color: install.ok ? 'var(--muted)' : 'var(--text)' }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
                {install.ok
                  ? <Info size={13} className="lucide-inline" style={{ marginTop: 2, color: 'var(--muted)' }} />
                  : <AlertTriangle size={13} className="lucide-inline" style={{ marginTop: 2, color: 'var(--danger)' }} />}
                <span style={{ fontSize: 12 }}>{install.detail}</span>
              </div>
              {/*
                Attempted-and-failed download: render the manual fallback command as
                a selectable <code> block so the user has a concrete next step. It
                WRAPS (break-all) rather than clipping, so the whole command is
                visible without a hidden horizontal scroll. The command is a
                server-provided constant (public-registry-pinned), never user input.
              */}
              {install.manual_command && (
                <code
                  style={{
                    fontSize: 11,
                    padding: '4px 8px',
                    borderRadius: 4,
                    background: 'var(--bg-hover)',
                    border: '1px solid var(--border)',
                    color: 'var(--text)',
                    userSelect: 'all',
                    wordBreak: 'break-all',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {install.manual_command}
                </code>
              )}
            </div>
          )}
          {/*
            Persistent "enabled but browser not on disk yet" surface, read from the
            server's durable `installed` flag (not the one-shot mutation result),
            so it still tells the truth after the user leaves and comes back.
            Muted advisory, not an error. Suppressed while a save is mid-flight or
            a fresh note is already shown above.
          */}
          {enabled && config?.installed === false && !saveMut.isPending && !install && (
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginTop: 8, color: 'var(--muted)' }}>
              <Info size={13} className="lucide-inline" style={{ marginTop: 2 }} />
              <span style={{ fontSize: 12 }}>{i18nT('pages.settings.browserPanel.browser_not_installed_retry')}</span>
            </div>
          )}
        </SettingsCard>
      </SettingsSection>

      {enabled && !extensionMode && (
        <SettingsSection title={i18nT('pages.settings.browserPanel.browser_engine')}>
          <SettingsCard>
            <p style={{ fontSize: 12, color: 'var(--muted)', margin: '0 0 10px' }}>
              {i18nT('pages.settings.browserPanel.pick_the_browser_the_agent_launches')}
            </p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {engines.map((eng) => (
                <button
                  key={eng}
                  type="button"
                  onClick={() => handleEngineSelect(eng)}
                  disabled={saveMut.isPending}
                  aria-pressed={engine === eng}
                  className="px-3 py-1.5 text-[13px] font-medium rounded border transition-colors disabled:opacity-50"
                  style={{
                    borderColor: engine === eng ? 'var(--accent)' : 'var(--border)',
                    background: engine === eng ? 'var(--accent-subtle, var(--bg-hover))' : 'var(--card)',
                    color: 'var(--text)',
                  }}
                >
                  {i18nT(ENGINE_LABEL_KEY[eng] ?? 'pages.settings.browserPanel.engine_chromium')}
                </button>
              ))}
            </div>
            {engine !== 'chromium' && (
              <p style={{ fontSize: 12, color: 'var(--muted)', margin: '10px 0 0' }}>
                {i18nT('pages.settings.browserPanel.firefox_webkit_run_playwrights_own_build')}
              </p>
            )}
          </SettingsCard>
        </SettingsSection>
      )}

      {enabled && (
        <SettingsSection title={i18nT('pages.settings.browserPanel.attach_to_my_browser')}>
          <SettingsCard>
            <SettingsToggle
              label={i18nT('pages.settings.browserPanel.attach_to_my_running_browser')}
              description={i18nT('pages.settings.browserPanel.use_my_chromium_browser_with_existing_logins')}
              checked={extensionMode}
              onChange={handleExtensionToggle}
              disabled={saveMut.isPending}
            />
            {!extensionMode && (
              <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
                {i18nT('pages.settings.browserPanel.headless_mode_active_browser_uses_cookie_injecti')}
              </p>
            )}
          </SettingsCard>
        </SettingsSection>
      )}

      {enabled && extensionMode && (
        <SettingsSection title={i18nT('pages.settings.browserPanel.connect_your_browser')}>
          <SettingsCard>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <p style={{ fontSize: 12, color: 'var(--muted)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.attach_uses_your_real_tabs_and_logins')}
              </p>
              <p style={{ fontSize: 13, color: 'var(--text)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.step1_install_the_playwright_extension')}
              </p>
              <a
                href={PLAYWRIGHT_EXTENSION_URL}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: 'var(--accent)', fontSize: 13 }}
              >
                {i18nT('pages.settings.browserPanel.playwright_extension_chromium_browsers')}{' '}
                <ExternalLink size={12} className="lucide-inline" />
              </a>
              <p style={{ fontSize: 13, color: 'var(--text)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.step2_pick_the_tab_when_the_agent_connects')}
              </p>
              <p style={{ fontSize: 12, color: 'var(--muted)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.optional_paste_token_to_skip_approval')}
              </p>
              <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                <div style={{ flex: 1 }}>
                  <SettingsInput
                    label={i18nT('pages.settings.browserPanel.connection_token_optional')}
                    description={i18nT('pages.settings.browserPanel.paste_playwright_mcp_extension_token_value_from')}
                    value={displayToken}
                    onChange={setToken}
                    placeholder={i18nT('pages.settings.browserPanel.paste_token_here')}
                  />
                </div>
                <button
                  onClick={handleSaveExtension}
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
                came out of the browser extension, so navigating to the chat would
                send the user back there to fetch it again.
              */}
              <ErrorNotice message={error} variant="inline" />
            </div>
          </SettingsCard>
        </SettingsSection>
      )}

      {enabled && !extensionMode && showExtension === false && config?.extension_mode && (
        <SettingsSection title="">
          <SettingsCard>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'space-between' }}>
              <p style={{ fontSize: 12, color: 'var(--muted)', margin: 0 }}>
                {i18nT('pages.settings.browserPanel.switch_to_headless_mode_this_will_remove_the_sav')}
              </p>
              <button
                onClick={handleConfirmHeadless}
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
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--ok)', padding: 16 }}>
          <Check size={14} className="lucide-inline" />
          <span style={{ fontSize: 12 }}>{i18nT('pages.settings.browserPanel.saved_and_applied_sessions_restarted')}</span>
        </div>
      )}
    </>
  )
}
