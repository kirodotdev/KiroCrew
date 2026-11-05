import { useCallback, useRef, useState } from 'react'
import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
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
  strandsModelRequired,
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
/** A field's Save or Remove. The provider picker waits while one runs: switching
 *  would unmount the field and drop its outcome, a failure included. */
const FIELD_WRITE_KEY = ['strandsFieldWrite']

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

/**
 * A failed write's notice: the card's own lead, then the gateway's reason when
 * it sent one (a key `config.local.json` pins, a refused value), so the user
 * learns why the next Save would fail too. With no reason, the lead alone.
 */
function failureNotice(error: Error | null, lead: string): { title?: string; message: string } {
  return error?.message ? { title: lead, message: error.message } : { message: lead }
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
      <SettingsSelect
        label={i18nT('pages.developer.strandsSetup.provider_label')}
        value={provider.id}
        options={STRANDS_PROVIDERS.map(p => p.id)}
        optionLabels={STRANDS_PROVIDERS.map(p => strandsProviderName(p.id))}
        onChange={v => {
          if (v !== savedProvider.id) providerMut.mutate(v as StrandsProvider['id'])
        }}
        disabled={!configQ.isSuccess || providerMut.isPending || modelMut.isPending || fieldWriting}
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
              <Btn
                disabled={modelMut.isPending || providerMut.isPending}
                onClick={() => modelMut.mutate(model.trim())}
              >
                {i18nT('pages.developer.strandsSetup.save')}
              </Btn>
              <Btn disabled={modelMut.isPending} onClick={() => setModelDraft(null)}>
                {i18nT('pages.developer.strandsSetup.cancel')}
              </Btn>
            </div>
          ) : undefined
        }
      />
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
  // The latest draft, for the save callback, which runs after the click's render.
  const draftRef = useRef('')
  draftRef.current = draft
  const [editing, setEditing] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const own = strandsVaultName(field.env)
  const mapped = referencedVaultName(env[field.env])
  // Saved means the map names a vault entry AND the vault holds it. A reference to
  // a missing entry is not a working value, so the field asks for one again.
  const isSet = mapped !== null && vaultNames.includes(mapped)
  // Mapped, but the vault entry is gone (deleted under Settings > Secrets): the
  // session refuses to start on it, so the field asks for a value AND offers to
  // unmap it.
  const dangling = mapped !== null && known && !vaultNames.includes(mapped)

  const refresh = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
      qc.invalidateQueries({ queryKey: SECRETS_QUERY_KEY }),
    ])

  // The map as in force now, read at write time rather than taken from the render.
  // Each write below sends only its own key (the gateway merges it into the saved
  // map under its config lock), so this read decides nothing about other keys.
  const freshEnv = async () => readStrandsEnv(await api.kirocrewConfig())

  const saveMut = useMutation({
    mutationKey: FIELD_WRITE_KEY,
    mutationFn: (value: string) => serial(async () => {
      const before = await freshEnv()
      await api.secretsSave(own, value)
      // Replacing a value the map already names: the vault write was the whole
      // change, so there is no reference to write.
      if (before[field.env] === `${SECRET_REF_PREFIX}${own}`) return
      // A failed reference write leaves the vault entry in place on purpose. It is
      // listed under Settings > Secrets, and the next Save here overwrites it; a
      // rollback that deleted it could take out an entry another tab had just
      // mapped, and the config would then name a missing value.
      await api.patchConfig(STRANDS_ENV_KEY, { [field.env]: `${SECRET_REF_PREFIX}${own}` })
    }),
    // Clears only the value it saved: a correction typed while the write ran is a
    // newer draft, still unsaved, and stays in the field with its own Save.
    onSuccess: (_result, saved) => {
      // Keep the field open on a newer draft: once saved, the field would
      // otherwise flip to its masked view and hide that unsaved text.
      if (draftRef.current.trim() !== saved) return setEditing(true)
      setDraft('')
      setEditing(false)
    },
    onSettled: refresh,
  })
  const removeMut = useMutation({
    mutationKey: FIELD_WRITE_KEY,
    mutationFn: () => serial(async () => {
      // Unmaps this variable only. The vault entry stays: something outside
      // Strands (an MCP server, a cron grant) may name it, and deleting a vault
      // value is Settings > Secrets' job, where every reader is in view.
      await api.patchConfig(STRANDS_ENV_KEY, { [field.env]: null })
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
      {dangling && !removeMut.isPending && (
        <div className="flex flex-wrap items-center gap-2" data-testid={`strands-dangling-${field.env}`}>
          <span className="text-[12px] text-warn">{i18nT('pages.developer.strandsSetup.value_missing')}</span>
          <Btn onClick={() => removeMut.mutate()}>{i18nT('pages.developer.strandsSetup.remove')}</Btn>
        </div>
      )}
      {known && !removeMut.isPending && !confirmRemove && (!isSet || editing) && (
        <Btn disabled={!draft.trim() || busy} onClick={() => saveMut.mutate(draft.trim())}>
          {i18nT('pages.developer.strandsSetup.save')}
        </Btn>
      )}
      {/* No hand-off: the field may hold a typed credential, and opening the agent
          would leave it in an unsent draft. */}
      {(saveMut.isError || removeMut.isError) && (
        <ErrorNotice
          {...(saveMut.isError
            ? failureNotice(saveMut.error, i18nT('pages.developer.strandsSetup.save_failed'))
            : failureNotice(removeMut.error, i18nT('pages.developer.strandsSetup.remove_failed')))}
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
