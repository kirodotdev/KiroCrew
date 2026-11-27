import { useCallback, useEffect, useRef, useState } from 'react'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound } from 'lucide-react'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsInput, SettingsSelect } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  readStrandsEnv,
  readStrandsModel,
  readStrandsProvider,
  STRANDS_ENV_KEY,
  STRANDS_MODEL_AUTO,
  STRANDS_MODEL_KEY,
  STRANDS_PROVIDER_KEY,
  STRANDS_PROVIDERS,
  strandsModelRequired,
  strandsVaultName,
  type StrandsProvider,
  type StrandsProviderField,
} from './strandsProviders'
import {
  CONFIG_QUERY_KEY,
  failureNotice,
  FIELD_WRITE_KEY,
  SECRETS_QUERY_KEY,
  VaultEnvField,
  type VaultEnvFieldTarget,
} from './VaultEnvField'

/** The settings-search anchor and test id of the section. */
export const STRANDS_SETUP_ANCHOR = 'strands-setup'


/** The provider's display name. One literal key per provider, so the catalog
 *  extractor sees every key this card can ask for. */
export function strandsProviderName(id: StrandsProvider['id']): string {
  switch (id) {
    case 'bedrock': return i18nT('pages.developer.strandsSetup.provider_bedrock')
    case 'bedrock-mantle': return i18nT('pages.developer.strandsSetup.provider_bedrock_mantle')
    case 'anthropic': return i18nT('pages.developer.strandsSetup.provider_anthropic')
    case 'openai': return i18nT('pages.developer.strandsSetup.provider_openai')
    case 'google': return i18nT('pages.developer.strandsSetup.provider_google')
    case 'ollama': return i18nT('pages.developer.strandsSetup.provider_ollama')
    case 'litellm': return i18nT('pages.developer.strandsSetup.provider_litellm')
  }
}

/** What the provider needs, in one sentence under the picker. */
function providerNote(id: StrandsProvider['id']): string {
  switch (id) {
    case 'bedrock': return i18nT('pages.developer.strandsSetup.note_bedrock')
    case 'bedrock-mantle': return i18nT('pages.developer.strandsSetup.note_bedrock_mantle')
    case 'anthropic': return i18nT('pages.developer.strandsSetup.note_anthropic')
    case 'openai': return i18nT('pages.developer.strandsSetup.note_openai')
    case 'google': return i18nT('pages.developer.strandsSetup.note_google')
    case 'ollama': return i18nT('pages.developer.strandsSetup.note_ollama')
    case 'litellm': return i18nT('pages.developer.strandsSetup.note_litellm')
  }
}

/** The field's help line, by the variable it fills. */
function fieldDescription(env: string): string {
  switch (env) {
    case 'AWS_BEARER_TOKEN_BEDROCK': return i18nT('pages.developer.strandsSetup.desc_aws_bearer_token_bedrock')
    case 'AWS_REGION': return i18nT('pages.developer.strandsSetup.desc_aws_region')
    case 'ANTHROPIC_API_KEY': return i18nT('pages.developer.strandsSetup.desc_anthropic_api_key')
    case 'OPENAI_API_KEY': return i18nT('pages.developer.strandsSetup.desc_openai_api_key')
    case 'GEMINI_API_KEY': return i18nT('pages.developer.strandsSetup.desc_gemini_api_key')
    case 'OLLAMA_HOST': return i18nT('pages.developer.strandsSetup.desc_ollama_host')
    case 'LITELLM_BASE_URL': return i18nT('pages.developer.strandsSetup.desc_litellm_base_url')
    case 'LITELLM_API_KEY': return i18nT('pages.developer.strandsSetup.desc_litellm_api_key')
    default: return ''
  }
}

/** Where a Strands provider field writes, and its words. The field-wide
 *  "save a new value" line names Strands, so it stays this card's. */
function strandsFieldTarget(field: StrandsProviderField): VaultEnvFieldTarget {
  return {
    configKey: STRANDS_ENV_KEY,
    env: field.env,
    vaultName: strandsVaultName(field.env),
    label: field.optional
      ? i18nT('pages.developer.strandsSetup.field_label_optional', { name: field.env })
      : field.env,
    description: fieldDescription(field.env),
    missingText: i18nT('pages.developer.strandsSetup.value_missing'),
    readEnv: readStrandsEnv,
    testIdPrefix: 'strands',
  }
}

/**
 * Settings > Agent Harness > Strands: the one place a user sets up the model
 * provider the Strands harness talks to.
 *
 * ## Where each value goes
 *
 * - The provider and the model go to `agent.strands_provider` and
 *   `agent.strands_model`. Neither is a secret, so both read back. An empty
 *   model field is stored as `auto`: no `--model`, so the provider's own default
 *   runs.
 * - A typed key, region, host or URL goes to the dashboard's Secrets VAULT, under
 *   one fixed name per variable (`strandsVaultName`), and only the `secret://`
 *   reference to it goes to `agent.strands_env`. The config refuses a literal value
 *   there, and the vault is write-only to the dashboard, so none of these is ever
 *   read back: a field shows saved or not saved, with Replace and Remove.
 *
 * Each write sends only its own key (`{NAME: ref}` to set, `{NAME: null}` to
 * remove); the gateway merges it into the saved map under its config lock, so
 * saves of different fields cannot drop each other's entry.
 *
 * Save writes the vault FIRST and the reference second, so the config never names
 * a vault entry that does not exist; a failed reference write leaves the entry in
 * the vault, where Settings > Secrets lists it and the next Save overwrites it.
 * Remove only unmaps the variable. The card never deletes a vault entry: other
 * readers may name it, and Settings > Secrets is where an entry is deleted.
 *
 * Switching provider writes the provider alone. Another provider's saved values
 * stay in the vault and the map until their own Remove, so switching back costs
 * nothing.
 */
export function StrandsSetupCard() {
  const qc = useQueryClient()
  const configQ = useQuery({ queryKey: CONFIG_QUERY_KEY, queryFn: () => api.kirocrewConfig() })
  const secretsQ = useQuery<{ names?: string[] }>({
    queryKey: SECRETS_QUERY_KEY,
    queryFn: () => api.secretsList(),
  })
  const env = readStrandsEnv(configQ.data)
  const savedProvider = readStrandsProvider(configQ.data)
  const savedModel = readStrandsModel(configQ.data)
  const [modelDraft, setModelDraft] = useState<string | null>(null)
  const fieldWriting = useIsMutating({ mutationKey: FIELD_WRITE_KEY }) > 0
  // Every write on the card runs one after another, so a save's vault write and
  // its reference are never interleaved with another field's.
  const tail = useRef<Promise<unknown>>(Promise.resolve())
  const serial = useCallback(<T,>(write: () => Promise<T>): Promise<T> => {
    const run = tail.current.then(write, write)
    tail.current = run.catch(() => undefined)
    return run
  }, [])
  const providerMut = useMutation({
    // The provider alone. The saved model stays: the backend always runs it under
    // the provider picked here (`<provider>/<model>`), so a stale id cannot send the
    // conversation to the old provider; it fails at start, naming the model, and
    // the field showing it sits right below. A second write here would be one more
    // that can half-land.
    mutationFn: (id: StrandsProvider['id']) =>
      serial(() => api.patchConfig(STRANDS_PROVIDER_KEY, id)),
    // An unsaved model draft goes when the switch STARTS: it was typed for the old
    // provider, and a Save queued behind the switch would land it under the new one.
    onMutate: () => setModelDraft(null),
    onSettled: () => qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
  })
  // Save and Cancel live only while the model is dirty, so a saved or dropped
  // draft unmounts the button that was pressed. Focus goes back to the model
  // field once it is enabled again, instead of falling to the page.
  const modelRowRef = useRef<HTMLDivElement>(null)
  const refocusModel = useRef(false)
  const leaveModelDraft = () => {
    if (modelRowRef.current?.contains(document.activeElement)) refocusModel.current = true
    setModelDraft(null)
  }
  const modelMut = useMutation({
    mutationFn: (model: string) =>
      serial(() => api.patchConfig(STRANDS_MODEL_KEY, model || STRANDS_MODEL_AUTO)),
    onSuccess: leaveModelDraft,
    onSettled: () => qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
  })
  const modelBusy = modelMut.isPending || providerMut.isPending
  const modelInputEnabled = configQ.isSuccess && !modelBusy
  useEffect(() => {
    if (!refocusModel.current || !modelInputEnabled || modelDraft !== null) return
    refocusModel.current = false
    // Only pick focus up where it fell: a control the user moved to while the
    // save settled keeps it.
    const active = document.activeElement
    const fell = !active || active === document.body || modelRowRef.current?.contains(active)
    if (!fell) return
    modelRowRef.current?.querySelector<HTMLInputElement | HTMLTextAreaElement>('input, textarea')?.focus()
  }, [modelInputEnabled, modelDraft])
  // The pick on screen while its write is in flight, so the fields follow the click.
  const provider =
    (providerMut.isPending && STRANDS_PROVIDERS.find(p => p.id === providerMut.variables)) ||
    savedProvider
  const title = i18nT('pages.developer.strandsSetup.title')
  const known = configQ.isSuccess && secretsQ.isSuccess
  const names = secretsQ.data?.names ?? []
  const model = modelDraft ?? savedModel
  const modelDirty = modelDraft !== null && modelDraft.trim() !== savedModel
  // With no model the project's own Bedrock default runs whatever provider is
  // picked, so a provider other than Bedrock needs one, and the card says so.
  const modelMissing = configQ.isSuccess && strandsModelRequired(provider) && !savedModel

  return (
    <section
      aria-label={title}
      className="border-t border-border pt-3"
      data-setting-key={STRANDS_SETUP_ANCHOR}
      data-testid="strands-setup-card"
    >
      <h3 className="mb-1 flex items-center gap-1.5 text-[13px] font-semibold text-text-strong">
        <KeyRound className="lucide-inline" aria-hidden="true" />
        {title}
      </h3>
      <p className="mb-1 text-[12px] leading-relaxed text-muted">
        {i18nT('pages.developer.strandsSetup.intro')}
      </p>
      {/* No hand-off: the key and model fields below may hold unsaved drafts. */}
      {(configQ.isError || secretsQ.isError) && (
        <ErrorNotice
          message={i18nT('pages.developer.strandsSetup.read_failed')}
          variant="inline"
        />
      )}
      {/* busy while its OWN write runs, so the picker keeps keyboard focus
          instead of a native disabled dropping it to the page. The rest stay
          native disabled: not yet loaded, or another control (the model save, a
          field save) is busy, which is not this control's own doing. (Kept
          outside the element: the settings extractor scans the tag's text to a
          4000-char ceiling and a backtick in an inline comment would hide the
          closing tag and drop the setting from the registry.) */}
      <SettingsSelect
        label={i18nT('pages.developer.strandsSetup.provider_label')}
        value={provider.id}
        options={STRANDS_PROVIDERS.map(p => p.id)}
        optionLabels={STRANDS_PROVIDERS.map(p => strandsProviderName(p.id))}
        onChange={v => {
          if (v !== savedProvider.id) providerMut.mutate(v as StrandsProvider['id'])
        }}
        busy={providerMut.isPending}
        disabled={!configQ.isSuccess || modelMut.isPending || fieldWriting}
        configKey={STRANDS_PROVIDER_KEY}
      />
      <p className="mb-0 text-[12px] leading-relaxed text-muted" data-testid="strands-provider-note">
        {providerNote(provider.id)}
      </p>
      {/* No hand-off: the key and model fields below may hold unsaved drafts. */}
      {providerMut.isError && (
        <ErrorNotice
          {...failureNotice(providerMut.error, i18nT('pages.developer.strandsSetup.save_failed'))}
          variant="inline"
          onDismiss={() => providerMut.reset()}
        />
      )}
      {provider.fields.map(field => (
        <VaultEnvField
          // Keyed by provider too: a draft typed for one provider's field must not
          // carry over to a field of the same name under another.
          key={`${provider.id}:${field.env}`}
          target={strandsFieldTarget(field)}
          env={env}
          vaultNames={names}
          known={known}
          serial={serial}
        />
      ))}
      <div ref={modelRowRef}>
      <SettingsInput
        label={i18nT('pages.developer.strandsSetup.model_label')}
        description={
          strandsModelRequired(provider)
            ? i18nT('pages.developer.strandsSetup.model_desc_required')
            : i18nT('pages.developer.strandsSetup.model_desc_default')
        }
        value={model}
        onChange={setModelDraft}
        disabled={!configQ.isSuccess || modelMut.isPending || providerMut.isPending}
        configKey={STRANDS_MODEL_KEY}
        after={
          modelDirty ? (
            <div className="flex items-center gap-2">
              {/* `aria-disabled` plus the guard, NOT `disabled`: a native
                  `disabled` takes the pressed button out of the tab order and
                  drops a keyboard user's focus to the page while the save runs,
                  and a refused save leaves it there. */}
              <Btn
                aria-disabled={modelBusy || undefined}
                className="aria-disabled:opacity-30 aria-disabled:cursor-default"
                onClick={() => { if (!modelBusy) modelMut.mutate(model.trim()) }}
              >
                {i18nT('pages.developer.strandsSetup.save')}
              </Btn>
              <Btn
                aria-disabled={modelMut.isPending || undefined}
                className="aria-disabled:opacity-30 aria-disabled:cursor-default"
                onClick={() => { if (!modelMut.isPending) leaveModelDraft() }}
              >
                {i18nT('pages.developer.strandsSetup.cancel')}
              </Btn>
            </div>
          ) : undefined
        }
      />
      </div>
      {modelMissing && (
        <p className="m-0 text-[12px] leading-relaxed text-warn" role="status" data-testid="strands-model-missing">
          {i18nT('pages.developer.strandsSetup.model_missing')}
        </p>
      )}
      {/* No hand-off: the model field holds an unsaved draft the agent would not see. */}
      {modelMut.isError && (
        <ErrorNotice
          {...failureNotice(modelMut.error, i18nT('pages.developer.strandsSetup.save_failed'))}
          variant="inline"
          onDismiss={() => modelMut.reset()}
        />
      )}
    </section>
  )
}
