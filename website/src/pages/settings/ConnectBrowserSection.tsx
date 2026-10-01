import { ExternalLink, KeyRound, Puzzle } from 'lucide-react'

import { SettingsSection, SettingsCard } from '../../components/settings'
import { Badge, Btn, Input } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useImeGuard } from '../../hooks/useImeGuard'
import { i18nT } from '../../i18n/t'

/**
 * Where the attach-mode extension is published. The id is the one `playwright-cli`
 * itself points at, so this link and the tool agree on which extension counts.
 */
const EXTENSION_URL =
  'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm'

/**
 * "Connect your existing browser": attach to a browser the operator already runs,
 * with its tabs and logins.
 *
 * Nothing here can install the extension or prove a connection. An extension is
 * granted inside the browser by the person using it, and a saved token only skips
 * that browser's approval prompt, so neither is shown as "connected".
 *
 * Rendered whatever the install state, and the draft is owned by the panel: the
 * field must not unmount while a pasted token is still unsaved.
 */
export function ConnectBrowserSection({
  tokenStored,
  draft,
  onDraftChange,
  onSave,
  onClear,
  saving,
  saveError,
}: {
  tokenStored: boolean
  draft: string
  onDraftChange: (value: string) => void
  onSave: () => void
  onClear: () => void
  saving: boolean
  saveError: string | null
}) {
  const ime = useImeGuard()
  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.connect_title')}>
      <SettingsCard>
        <div className="flex items-start gap-3">
          <Puzzle size={18} className="text-muted shrink-0 mt-[2px]" />
          <div className="min-w-0 flex-1 flex flex-col gap-1.5">
            <div className="text-sm font-medium">{i18nT('pages.settings.browserPanel.attach_title')}</div>
            <p className="text-[13px] m-0">{i18nT('pages.settings.browserPanel.connect_explains')}</p>
            <p className="text-[13px] text-muted m-0">{i18nT('pages.settings.browserPanel.connect_host_note')}</p>
            <p className="text-[13px] text-muted m-0">{i18nT('pages.settings.browserPanel.connect_consent')}</p>
            <a
              href={EXTENSION_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-[13px] text-accent hover:underline"
            >
              {i18nT('pages.settings.browserPanel.get_the_extension')}
              <ExternalLink size={13} />
            </a>
          </div>
        </div>
      </SettingsCard>

      {/* Optional on purpose: the token is a stored credential whose absence costs
          one click, and that prompt is the moment a human is told a program is
          about to drive their logged-in browser. */}
      <SettingsCard>
        <div className="flex items-start gap-3" data-setting-label={i18nT('pages.settings.browserPanel.token_label')}>
          <KeyRound size={18} className="text-muted shrink-0 mt-[2px]" />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <label htmlFor="pw-attach-token" className="text-sm font-medium">
                {i18nT('pages.settings.browserPanel.token_label')}
              </label>
              {tokenStored && <Badge variant="ok">{i18nT('pages.settings.browserPanel.token_stored')}</Badge>}
            </div>
            <p className="text-[13px] text-muted mt-1 mb-2">{i18nT('pages.settings.browserPanel.token_explains')}</p>
            <div className="flex items-center gap-2">
              <Input
                id="pw-attach-token"
                type="password"
                autoComplete="off"
                value={draft}
                onChange={(e) => onDraftChange(e.target.value)}
                {...ime.bindEnter({
                  onEnter: () => {
                    if (!saving && draft.trim()) onSave()
                  },
                })}
                placeholder={
                  tokenStored
                    ? i18nT('pages.settings.browserPanel.token_set')
                    : i18nT('pages.settings.browserPanel.token_placeholder')
                }
                aria-label={i18nT('pages.settings.browserPanel.token_label')}
                className="flex-1 min-w-0"
              />
              <Btn primary onClick={onSave} disabled={saving || !draft.trim()}>
                {i18nT('pages.settings.browserPanel.token_save')}
              </Btn>
              {tokenStored && (
                <Btn onClick={onClear} disabled={saving}>
                  {i18nT('pages.settings.browserPanel.token_clear')}
                </Btn>
              )}
            </div>
            {/* No hand-off: a rejected save is exactly when the pasted token is
                still unsaved, and the hand-off would unmount the field. The
                message is the gateway's error text, never the draft. */}
            {saveError && <ErrorNotice variant="inline" className="mt-2" message={saveError} />}
          </div>
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
