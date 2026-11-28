import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SecretField } from '../../components/SecretField'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { referencedVaultName, SECRET_REF_PREFIX } from './agentEnvMap'

/** Masked stand-in for a stored value; the vault never hands one back. */
const SECRET_PREVIEW = '••••••••'

export const CONFIG_QUERY_KEY = ['kirocrewConfig']
export const SECRETS_QUERY_KEY = ['secrets']
/** A field's Save or Remove. A card that would unmount its fields (the Strands
 *  provider picker) waits while one runs, so the field's outcome is not dropped. */
export const FIELD_WRITE_KEY = ['vaultEnvFieldWrite']

/**
 * A failed write's notice: the card's own lead, then the gateway's reason when
 * it sent one (a key `config.local.json` pins, a refused value), so the user
 * learns why the next Save would fail too. With no reason, the lead alone.
 */
export function failureNotice(error: Error | null, lead: string): { title?: string; message: string } {
  return error?.message ? { title: lead, message: error.message } : { message: lead }
}

/** Where one field writes, and the words it shows. */
export interface VaultEnvFieldTarget {
  /** The env map's config path, e.g. `agent.strands_env`. */
  configKey: string
  /** The environment-variable name the field maps. */
  env: string
  /** The one fixed vault entry the field saves to, so a second Save replaces it. */
  vaultName: string
  label: string
  description: string
  /** Shown when the map names a vault entry that is gone. */
  missingText: string
  /** The env map as saved, read from a config document. */
  readEnv: (cfg: unknown) => Record<string, string>
  /** `<prefix>-field-<env>` and `<prefix>-dangling-<env>`. */
  testIdPrefix: string
}

/**
 * One variable of a harness env map, as Settings > Agent Harness offers it: a
 * write-only field with its own Save, Replace and Remove.
 *
 * Save writes the vault FIRST and the reference second, so the config never names
 * a vault entry that does not exist; a failed reference write leaves the entry in
 * the vault, where Settings > Secrets lists it and the next Save overwrites it.
 * Each write sends only its own key (`{NAME: ref}` to set, `{NAME: null}` to
 * remove); the gateway merges it into the saved map under its config lock.
 * Remove only unmaps the variable: the field never deletes a vault entry.
 */
export function VaultEnvField({
  target,
  env,
  vaultNames,
  known,
  serial,
}: {
  target: VaultEnvFieldTarget
  /** The env map as the card last read it. */
  env: Record<string, string>
  vaultNames: string[]
  known: boolean
  /** The card's write queue: every write on the card runs one after another, so a
   *  save's vault write and its reference never interleave with another field's. */
  serial: <T>(write: () => Promise<T>) => Promise<T>
}) {
  const qc = useQueryClient()
  // A save or remove unmounts the pressed button once the card's queries catch
  // up: a saved key turns into the masked view with Replace, a removed one into
  // an empty input. Focus waits for that control and moves to it, so it does
  // not stay on the page. Armed only when focus was in this field at the press.
  const fieldRef = useRef<HTMLDivElement>(null)
  const armed = useRef(false)
  const [refocus, setRefocus] = useState<'none' | 'replace' | 'input' | 'remove'>('none')
  const aim = (target: 'replace' | 'input' | 'remove') => {
    if (armed.current) setRefocus(target)
    armed.current = false
  }
  const disarm = () => {
    armed.current = false
    setRefocus('none')
  }
  const [draft, setDraft] = useState('')
  // The latest draft, for the save callback, which runs after the click's render.
  const draftRef = useRef('')
  draftRef.current = draft
  const [editing, setEditing] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState(false)
  // Arming swaps the focused Remove for the confirm row and Cancel swaps it
  // back, so each would drop focus to the page. Same handoff as Settings >
  // Secrets: Cancel takes focus on arm (the safe choice), and Remove on Cancel.
  const removeButtonRef = useRef<HTMLButtonElement>(null)
  const cancelRemoveRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (confirmRemove) cancelRemoveRef.current?.focus()
  }, [confirmRemove])
  const cancelRemove = () => {
    setConfirmRemove(false)
    requestAnimationFrame(() => removeButtonRef.current?.focus())
  }
  const own = target.vaultName
  const mapped = referencedVaultName(env[target.env])
  // Saved means the map names a vault entry AND the vault holds it, found as the
  // gateway finds it: the whole name, else the name before a field reference's `#`.
  const isSet = mapped !== null && [mapped, mapped.split('#')[0]].some(name => vaultNames.includes(name))
  // Mapped, but the vault entry is gone (deleted under Settings > Secrets): the
  // session refuses to start on it, so the field asks for a value AND offers to
  // unmap it.
  const dangling = mapped !== null && known && !isSet

  const refresh = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY }),
      qc.invalidateQueries({ queryKey: SECRETS_QUERY_KEY }),
    ])

  // The map as in force now, read at write time rather than taken from the render.
  // Each write below sends only its own key (the gateway merges it into the saved
  // map under its config lock), so this read decides nothing about other keys.
  const freshEnv = async () => target.readEnv(await api.kirocrewConfig())

  const saveMut = useMutation({
    mutationKey: FIELD_WRITE_KEY,
    mutationFn: (value: string) => serial(async () => {
      const before = await freshEnv()
      await api.secretsSave(own, value)
      // Replacing a value the map already names: the vault write was the whole
      // change, so there is no reference to write.
      if (before[target.env] === `${SECRET_REF_PREFIX}${own}`) return
      // A failed reference write leaves the vault entry in place on purpose. It is
      // listed under Settings > Secrets, and the next Save here overwrites it; a
      // rollback that deleted it could take out an entry another tab had just
      // mapped, and the config would then name a missing value.
      await api.patchConfig(target.configKey, { [target.env]: `${SECRET_REF_PREFIX}${own}` })
    }),
    // Clears only the value it saved: a correction typed while the write ran is a
    // newer draft, still unsaved, and stays in the field with its own Save.
    onSuccess: (_result, saved) => {
      // Keep the field open on a newer draft: once saved, the field would
      // otherwise flip to its masked view and hide that unsaved text.
      if (draftRef.current.trim() !== saved) {
        disarm()
        return setEditing(true)
      }
      setDraft('')
      setEditing(false)
      aim('replace')
    },
    onError: disarm,
    onSettled: refresh,
  })
  const removeMut = useMutation({
    mutationKey: FIELD_WRITE_KEY,
    mutationFn: () => serial(async () => {
      // Unmaps this variable only. The vault entry stays: something outside
      // this harness (an MCP server, a cron grant) may name it, and deleting a
      // vault value is Settings > Secrets' job, where every reader is in view.
      await api.patchConfig(target.configKey, { [target.env]: null })
    }),
    // The confirmation closes on success only: a refused remove keeps the
    // focused confirm Remove in place to retry, beside the failure notice.
    onSuccess: () => {
      setConfirmRemove(false)
      aim('input')
    },
    // A refused dangling Remove was hidden while it ran; focus goes back to it.
    onError: () => aim('remove'),
    onSettled: refresh,
  })
  const writing = saveMut.isPending || removeMut.isPending
  const busy = !known || writing
  // A save or remove swaps this field's controls when it settles (Save leaves
  // for the masked view, the confirm row and the dangling Remove go), so the
  // pressed button can unmount. When it does and focus has fallen to the page,
  // it moves to the field's first live control instead. A control the user
  // moved to meanwhile keeps it.
  // The latest write owns the handoff: one still pending from an earlier
  // press is dropped.
  const startWrite = (write: () => void) => {
    setRefocus('none')
    armed.current = Boolean(fieldRef.current?.contains(document.activeElement))
    write()
  }
  const showsReplace = isSet && known && !editing
  useEffect(() => {
    if (refocus === 'none' || busy) return
    if (refocus === 'replace' ? !showsReplace : refocus === 'input' ? isSet : false) return
    setRefocus('none')
    // Only focus that fell to the page moves: a control the user picked
    // meanwhile keeps it.
    const active = document.activeElement
    if (active && active !== document.body) return
    const root = fieldRef.current
    const buttons = [...(root?.querySelectorAll<HTMLButtonElement>('button') ?? [])]
    const target = refocus === 'replace'
      ? buttons.find(b => b.textContent?.includes(i18nT('pages.developer.strandsSetup.replace')))
      : refocus === 'remove'
        ? buttons.findLast(b => b.textContent?.trim() === i18nT('pages.developer.strandsSetup.remove'))
        : root?.querySelector<HTMLInputElement>('input:not([disabled])')
    target?.focus()
  }, [refocus, busy, showsReplace, isSet])
  return (
    <div ref={fieldRef} data-testid={`${target.testIdPrefix}-field-${target.env}`}>
      <SecretField
        label={target.label}
        description={target.description}
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
          buttonRef: removeButtonRef,
          confirmation: confirmRemove ? (
            <>
              <span className="text-[13px] text-warn">
                {i18nT('pages.developer.strandsSetup.remove_confirm')}
              </span>
              {/* `aria-disabled` plus the guard, NOT `disabled`, so the pressed
                  Delete keeps focus while the remove runs. */}
              <Btn
                danger
                aria-disabled={busy || undefined}
                className="aria-disabled:opacity-30 aria-disabled:cursor-default"
                onClick={() => { if (!busy) startWrite(() => removeMut.mutate()) }}
              >
                {i18nT('pages.developer.strandsSetup.remove_delete')}
              </Btn>
              <Btn ref={cancelRemoveRef} disabled={removeMut.isPending} onClick={cancelRemove}>
                {i18nT('pages.developer.strandsSetup.cancel')}
              </Btn>
            </>
          ) : undefined,
        }}
      />
      {dangling && !removeMut.isPending && (
        <div className="flex flex-wrap items-center gap-2" data-testid={`${target.testIdPrefix}-dangling-${target.env}`}>
          <span className="text-[12px] text-warn">{target.missingText}</span>
          <Btn onClick={() => startWrite(() => removeMut.mutate())}>{i18nT('pages.developer.strandsSetup.remove')}</Btn>
        </div>
      )}
      {known && !removeMut.isPending && !confirmRemove && (!isSet || editing) && (
        // An empty draft stays natively disabled: nothing was pressed. A write in
        // flight uses `aria-disabled` plus the guard, so the pressed Save keeps
        // focus while it runs and after a refusal.
        <Btn
          // Stays natively enabled while a write runs: a successful save clears
          // the draft before the masked view replaces this button.
          disabled={!draft.trim() && !writing}
          aria-disabled={writing || undefined}
          className="aria-disabled:opacity-30 aria-disabled:cursor-default"
          onClick={() => { if (!writing) startWrite(() => saveMut.mutate(draft.trim())) }}
        >
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
