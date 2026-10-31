import { useCallback, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound } from 'lucide-react'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SecretField } from '../../components/SecretField'
import { SettingsInput, SettingsSelect } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  readStrandsEnv,
  readStrandsModel,
  readStrandsProvider,
  referencedVaultName,
  SECRET_REF_PREFIX,
  STRANDS_ENV_KEY,
  STRANDS_MODEL_AUTO,
  STRANDS_MODEL_KEY,
  STRANDS_PROVIDER_KEY,
  STRANDS_PROVIDERS,
  strandsVaultName,
  type StrandsProvider,
  type StrandsProviderField,
} from './strandsProviders'

/** Masked stand-in for a stored value; the vault never hands one back. */
const SECRET_PREVIEW = '••••••••'

/** The settings-search anchor and test id of the section. */
export const STRANDS_SETUP_ANCHOR = 'strands-setup'

const CONFIG_QUERY_KEY = ['kirocrewConfig']
const SECRETS_QUERY_KEY = ['secrets']

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
    case 'AWS_PROFILE': return i18nT('pages.developer.strandsSetup.desc_aws_profile')
    case 'ANTHROPIC_API_KEY': return i18nT('pages.developer.strandsSetup.desc_anthropic_api_key')
    case 'OPENAI_API_KEY': return i18nT('pages.developer.strandsSetup.desc_openai_api_key')
    case 'GEMINI_API_KEY': return i18nT('pages.developer.strandsSetup.desc_gemini_api_key')
    case 'OLLAMA_HOST': return i18nT('pages.developer.strandsSetup.desc_ollama_host')
    case 'LITELLM_BASE_URL': return i18nT('pages.developer.strandsSetup.desc_litellm_base_url')
    case 'LITELLM_API_KEY': return i18nT('pages.developer.strandsSetup.desc_litellm_api_key')
    default: return ''
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
 * Save writes the vault FIRST and the reference second, so the config never names
 * a vault entry that does not exist. Remove runs the other way round: the reference
 * leaves the config, then the entry it named leaves the vault. An entry is deleted
 * only when it is this card's own name; a reference a person pointed at a vault
 * entry of their own is unmapped and the entry is left alone.
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
  // Every write on the card runs one after another. Each field writes the WHOLE
  // `agent.strands_env` map it has just read, so two saves left to overlap would
  // each write a map without the other's entry and the later one would win.
  const tail = useRef<Promise<unknown>>(Promise.resolve())
  const serial = useCallback(<T,>(write: () => Promise<T>): Promise<T> => {
    const run = tail.current.then(write, write)
    tail.current = run.catch(() => undefined)
    return run
  }, [])
  const providerMut = useMutation({
    // A model saved for the old provider would follow the switch and be sent to
    // the new one as `<new provider>/<old model>`, a model it does not have. So
    // the model goes back to `auto` first, then the provider is written.
    mutationFn: (id: StrandsProvider['id']) =>
      serial(async () => {
        // Read at write time, not from the render: a model save queued just ahead
        // of this switch has landed by now and must be cleared too.
        if (readStrandsModel(await api.kirocrewConfig())) {
          await api.patchConfig(STRANDS_MODEL_KEY, STRANDS_MODEL_AUTO)
        }
        await api.patchConfig(STRANDS_PROVIDER_KEY, id)
      }),
    onSuccess: () => setModelDraft(null),
    onSettled: () => qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
  })
  const modelMut = useMutation({
    mutationFn: (model: string) =>
      serial(() => api.patchConfig(STRANDS_MODEL_KEY, model || STRANDS_MODEL_AUTO)),
    onSuccess: () => setModelDraft(null),
    onSettled: () => qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
  })
  // The pick on screen while its write is in flight, so the fields follow the click.
  const provider =
    (providerMut.isPending && STRANDS_PROVIDERS.find(p => p.id === providerMut.variables)) ||
    savedProvider
  const title = i18nT('pages.developer.strandsSetup.title')
  const known = configQ.isSuccess && secretsQ.isSuccess
  const names = secretsQ.data?.names ?? []
  const model = modelDraft ?? savedModel
  const modelDirty = modelDraft !== null && modelDraft.trim() !== savedModel

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
      <SettingsSelect
        label={i18nT('pages.developer.strandsSetup.provider_label')}
        value={provider.id}
        options={STRANDS_PROVIDERS.map(p => p.id)}
        optionLabels={STRANDS_PROVIDERS.map(p => strandsProviderName(p.id))}
        onChange={v => {
          if (v !== savedProvider.id) providerMut.mutate(v as StrandsProvider['id'])
        }}
        disabled={!configQ.isSuccess || providerMut.isPending || modelMut.isPending}
        configKey={STRANDS_PROVIDER_KEY}
      />
      <p className="mb-0 text-[12px] leading-relaxed text-muted" data-testid="strands-provider-note">
        {providerNote(provider.id)}
      </p>
      {/* No hand-off: the key and model fields below may hold unsaved drafts. */}
      {providerMut.isError && (
        <ErrorNotice
          message={i18nT('pages.developer.strandsSetup.save_failed')}
          variant="inline"
          onDismiss={() => providerMut.reset()}
        />
      )}
      {provider.fields.map(field => (
        <StrandsField
          // Keyed by provider too: a draft typed for one provider's field must not
          // carry over to a field of the same name under another.
          key={`${provider.id}:${field.env}`}
          field={field}
          env={env}
          vaultNames={names}
          known={known}
          serial={serial}
        />
      ))}
      <SettingsInput
        label={i18nT('pages.developer.strandsSetup.model_label')}
        description={i18nT('pages.developer.strandsSetup.model_desc_default')}
        value={model}
        onChange={setModelDraft}
        disabled={!configQ.isSuccess || modelMut.isPending}
        configKey={STRANDS_MODEL_KEY}
        after={
          modelDirty ? (
            <div className="flex items-center gap-2">
              <Btn disabled={modelMut.isPending} onClick={() => modelMut.mutate(model.trim())}>
                {i18nT('pages.developer.strandsSetup.save')}
              </Btn>
              <Btn disabled={modelMut.isPending} onClick={() => setModelDraft(null)}>
                {i18nT('pages.developer.strandsSetup.cancel')}
              </Btn>
            </div>
          ) : undefined
        }
      />
      {/* No hand-off: the model field holds an unsaved draft the agent would not see. */}
      {modelMut.isError && (
        <ErrorNotice
          message={i18nT('pages.developer.strandsSetup.save_failed')}
          variant="inline"
          onDismiss={() => modelMut.reset()}
        />
      )}
    </section>
  )
}

/** One variable: a write-only field with its own Save, Replace and Remove. */
function StrandsField({
  field,
  env,
  vaultNames,
  known,
  serial,
}: {
  field: StrandsProviderField
  env: Record<string, string>
  vaultNames: string[]
  known: boolean
  /** The card's write queue: see `StrandsSetupCard`. */
  serial: <T>(write: () => Promise<T>) => Promise<T>
}) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState('')
  const [editing, setEditing] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const own = strandsVaultName(field.env)
  const mapped = referencedVaultName(env[field.env])
  // Saved means the map names a vault entry AND the vault holds it. A reference to
  // a missing entry is not a working value, so the field asks for one again.
  const isSet = mapped !== null && vaultNames.includes(mapped)

  const refresh = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
      qc.invalidateQueries({ queryKey: SECRETS_QUERY_KEY }),
    ])

  // The map is re-read at write time rather than taken from the render, so a save
  // in another field between this render and the click is not written over.
  const freshEnv = async () => readStrandsEnv(await api.kirocrewConfig())

  const saveMut = useMutation({
    mutationFn: (value: string) => serial(async () => {
      const before = await freshEnv()
      const ownWasStored = ((await api.secretsList()).names ?? []).includes(own)
      await api.secretsSave(own, value)
      // Replacing a value the map already names: the vault write was the whole
      // change, so there is no reference to write and nothing to fail after it.
      if (before[field.env] === `${SECRET_REF_PREFIX}${own}`) return
      try {
        await api.patchConfig(STRANDS_ENV_KEY, { ...before, [field.env]: `${SECRET_REF_PREFIX}${own}` })
      } catch (err) {
        // Take back a vault entry this save created, so no credential is left in
        // the vault with nothing naming it and no control on screen to remove it.
        // Only once a fresh read CONFIRMS the reference did not land: a PATCH whose
        // response was lost may still have committed, and deleting then would leave
        // the config naming a missing entry. An unreadable config keeps the entry.
        if (!ownWasStored) {
          const landed = await freshEnv().then(
            env => env[field.env] === `${SECRET_REF_PREFIX}${own}`,
            () => true,
          )
          if (!landed) await api.secretsDelete(own).catch(() => undefined)
        }
        throw err
      }
    }),
    onSuccess: () => {
      setDraft('')
      setEditing(false)
    },
    onSettled: refresh,
  })
  const removeMut = useMutation({
    mutationFn: () => serial(async () => {
      const current = await freshEnv()
      const named = referencedVaultName(current[field.env])
      const rest = Object.fromEntries(Object.entries(current).filter(([k]) => k !== field.env))
      await api.patchConfig(STRANDS_ENV_KEY, rest)
      if (named === own) await api.secretsDelete(own)
    }),
    onSettled: () => {
      setConfirmRemove(false)
      return refresh()
    },
  })
  const busy = !known || saveMut.isPending || removeMut.isPending
  const label = field.optional
    ? i18nT('pages.developer.strandsSetup.field_label_optional', { name: field.env })
    : field.env

  return (
    <div data-testid={`strands-field-${field.env}`}>
      <SecretField
        label={label}
        description={fieldDescription(field.env)}
        isSet={isSet && known}
        preview={SECRET_PREVIEW}
        replaceLabel={i18nT('pages.developer.strandsSetup.replace')}
        value={draft}
        onChange={setDraft}
        editing={editing && !removeMut.isPending && !confirmRemove}
        onEditingChange={setEditing}
        cleared={false}
        onClearedChange={next => {
          if (next && !busy) setConfirmRemove(true)
        }}
        permanentRemoval={{
          label: i18nT('pages.developer.strandsSetup.remove'),
          text: i18nT('pages.developer.strandsSetup.remove'),
          confirmation: confirmRemove ? (
            <>
              <span className="text-[13px] text-warn">
                {i18nT('pages.developer.strandsSetup.remove_confirm')}
              </span>
              <Btn danger disabled={busy} onClick={() => removeMut.mutate()}>
                {i18nT('pages.developer.strandsSetup.remove_delete')}
              </Btn>
              <Btn disabled={removeMut.isPending} onClick={() => setConfirmRemove(false)}>
                {i18nT('pages.developer.strandsSetup.cancel')}
              </Btn>
            </>
          ) : undefined,
        }}
      />
      {known && !removeMut.isPending && !confirmRemove && (!isSet || editing) && (
        <Btn disabled={!draft.trim() || busy} onClick={() => saveMut.mutate(draft.trim())}>
          {i18nT('pages.developer.strandsSetup.save')}
        </Btn>
      )}
      {/* No hand-off: the field may hold a typed credential, and opening the agent
          would leave it in an unsent draft. */}
      {(saveMut.isError || removeMut.isError) && (
        <ErrorNotice
          message={
            saveMut.isError
              ? i18nT('pages.developer.strandsSetup.save_failed')
              : i18nT('pages.developer.strandsSetup.remove_failed')
          }
          variant="inline"
          onDismiss={() => {
            saveMut.reset()
            removeMut.reset()
          }}
        />
      )}
    </div>
  )
}
