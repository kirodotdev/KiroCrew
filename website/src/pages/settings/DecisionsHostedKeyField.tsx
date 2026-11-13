import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { DECISIONS_API_KEY_SECRET } from './decisionsPreview'

/** Masked stand-in for a stored credential; the value itself is never sent back. */
const SECRET_PREVIEW = '••••••••'

/**
 * The hosted provider's API key, inside that provider's own entry of the decision
 * model picker.
 *
 * It is not a shared setting: only the hosted provider (and a hand-written address
 * that proxies to it) is ever sent this key, so it lives where that provider is
 * chosen, and the shared block of the card holds only what every decision model
 * shares.
 *
 * The key goes into the dashboard SECRETS VAULT, as the one entry
 * `TYPESAFE_API_KEY`, and never into `config.json`: `provider.api_key` honours
 * `secret://TYPESAFE_API_KEY` and nothing else, so no key is ever written to
 * `config.json` and none is ever read back. The field shows set or unset, never a
 * value.
 *
 * A failed read of the vault index is never presented as "no key set": that would
 * offer Save over a credential the reader has just been told is absent. The
 * controls hold and the notice says the read failed.
 */
export function DecisionsHostedKeyField({ disabled }: { disabled: boolean }) {
  const qc = useQueryClient()
  // Names only: the vault never exposes a value.
  const secretsQ = useQuery<{ names?: string[] }>({
    queryKey: ['secrets'],
    queryFn: () => api.secretsList(),
  })
  const [draft, setDraft] = useState('')
  const [editing, setEditing] = useState(false)
  // Whether the trash has been pressed and the reader has not yet confirmed. Removing
  // a vault entry cannot be undone, so the question goes BEFORE the DELETE.
  const [confirmRemove, setConfirmRemove] = useState(false)
  // Arming swaps the focused Remove key for the confirm row and Keep it swaps it
  // back; Keep it takes focus on arm (the safe choice), Remove key on Keep it.
  const removeRef = useRef<HTMLButtonElement>(null)
  const cancelRemoveRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (confirmRemove) cancelRemoveRef.current?.focus()
  }, [confirmRemove])
  const cancelRemove = () => {
    setConfirmRemove(false)
    requestAnimationFrame(() => removeRef.current?.focus())
  }
  const saveMut = useMutation({
    mutationFn: (value: string) => api.secretsSave(DECISIONS_API_KEY_SECRET, value),
    onSuccess: () => {
      setDraft('')
      setEditing(false)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['secrets'] }),
  })
  const deleteMut = useMutation({
    mutationFn: () => api.secretsDelete(DECISIONS_API_KEY_SECRET),
    onSettled: () => {
      setConfirmRemove(false)
      return qc.invalidateQueries({ queryKey: ['secrets'] })
    },
  })

  const isSet = (secretsQ.data?.names ?? []).includes(DECISIONS_API_KEY_SECRET)
  // Every credential control is gated on a KNOWN index: without a successful read
  // the field cannot tell "no key" from "could not ask".
  const known = secretsQ.isSuccess
  // A removal in flight holds the controls too: a DELETE and a POST outstanding
  // together can land the delete last and erase the key just saved.
  const busy = !known || disabled || saveMut.isPending || deleteMut.isPending

  return (
    <div
      className="flex flex-col gap-1 rounded-md border border-border bg-bg-accent px-2.5 py-1.5"
      data-testid="decisions-hosted-key"
    >
      {/* The rotate icon names the verb and not the noun, so this one passes its own
          label. Removal ASKS FIRST: `cleared` stays false and the trash opens a
          confirmation instead of writing, because a vault entry is not deferred and
          not recoverable, so an Undo strip would offer something already done. */}
      <SecretField
        label={i18nT('pages.developer.featurePreviewsTab.decisions_api_key_label')}
        description={i18nT('pages.developer.featurePreviewsTab.decisions_api_key_desc')}
        isSet={isSet && known}
        preview={SECRET_PREVIEW}
        replaceLabel={i18nT('pages.developer.featurePreviewsTab.decisions_api_key_replace')}
        value={draft}
        onChange={setDraft}
        editing={editing && !deleteMut.isPending && !confirmRemove}
        onEditingChange={setEditing}
        cleared={false}
        onClearedChange={next => {
          if (next && !busy) setConfirmRemove(true)
        }}
        permanentRemoval={{
          label: i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove'),
          text: i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove'),
          buttonRef: removeRef,
          confirmation: confirmRemove ? (
            <>
              <span className="text-[13px] text-warn">
                {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove_confirm')}
              </span>
              <Btn danger disabled={busy} onClick={() => deleteMut.mutate()}>
                {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_delete')}
              </Btn>
              <Btn ref={cancelRemoveRef} disabled={deleteMut.isPending} onClick={cancelRemove}>
                {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_remove_cancel')}
              </Btn>
            </>
          ) : undefined,
        }}
      />
      {known && !deleteMut.isPending && !confirmRemove && (!isSet || editing) && (
        <div>
          {/* Held while EITHER write is outstanding, not just the save. */}
          <Btn disabled={!draft || busy} onClick={() => saveMut.mutate(draft)}>
            {i18nT('pages.developer.featurePreviewsTab.decisions_api_key_save')}
          </Btn>
        </div>
      )}
      {/* A failed READ: the hand-off only while no key is being typed, since asking
          the agent unmounts this card and the draft with it. */}
      {secretsQ.isError && (
        <ErrorNotice
          variant="inline"
          askAgent={!draft}
          message={i18nT('settings.secrets.load_error', {
            error: (secretsQ.error as Error).message,
          })}
        />
      )}
      {/* No hand-off: a save failure is exactly when the draft holds a credential the
          vault has not taken yet, and asking the agent unmounts this card. */}
      {(saveMut.isError || deleteMut.isError) && (
        <ErrorNotice
          variant="inline"
          message={i18nT('pages.developer.featurePreviewsTab.decisions_save_failed')}
        />
      )}
    </div>
  )
}
