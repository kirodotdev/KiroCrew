/**
 * The `openai_compatible` provider's one editable setting: the model id.
 *
 * The server address and the API key are deliberately not editable here. They
 * live in two fixed vault entries (Settings → Secrets), so the agent-writable
 * config file can never redirect where the audio goes
 * (`docs/system-specs/modules/stt-streaming.md#openai-compatible-provider`).
 */
import { useState } from 'react'
import { SettingsInput } from '../../components/settings'
import { i18nT } from '../../i18n/t'

export const OPENAI_STT_BASE_URL_SECRET = 'OPENAI_STT_BASE_URL'
export const OPENAI_STT_API_KEY_SECRET = 'OPENAI_STT_API_KEY'

interface Props {
  /** The stored model id, as served by `GET /api/config/stt`. */
  model: string
  disabled?: boolean
  onSave: (model: string) => void
}

export default function SttOpenAiCompatibleSettings({ model, disabled, onSave }: Props) {
  const [draft, setDraft] = useState(model)
  return (
    <>
      <SettingsInput
        configKey="stt.openai_model"
        label={i18nT('pages.settings.sttSettings.openai_model')}
        hint={i18nT('pages.settings.sttSettings.openai_model_desc')}
        value={draft}
        onChange={setDraft}
        onBlur={() => {
          const next = draft.trim()
          if (next && next !== model) onSave(next)
          else setDraft(model)
        }}
        placeholder="gpt-4o-transcribe"
        disabled={disabled}
      />
      <p className="-mt-1 mb-3 text-[13px] text-muted" data-testid="stt-openai-secrets-note">
        {i18nT('pages.settings.sttSettings.openai_secrets_note', {
          baseUrl: OPENAI_STT_BASE_URL_SECRET,
          apiKey: OPENAI_STT_API_KEY_SECRET,
        })}
      </p>
    </>
  )
}
