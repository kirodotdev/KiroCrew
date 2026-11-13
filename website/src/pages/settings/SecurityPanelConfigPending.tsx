/**
 * Settings -> Security: settings files changed outside the dashboard.
 *
 * On a host where the sandbox seals the settings files, an edit made outside
 * the dashboard (an editor, a script, an agent's shell) does not take effect on
 * its own: the gateway keeps serving the last version it saved and reports the
 * edit here as pending. The owner reviews the changed keys and either applies
 * the edit or discards it (the file is overwritten with the saved version). A
 * deleted file is pending too, and can only be discarded, which puts it back.
 *
 * A change that loosens confinement (the sandbox, deny rules, auto-approved
 * tools, computer use, the agent environment) carries a plain-words line saying
 * what it does (`securityHints`), so the owner can tell a risky edit from a
 * cosmetic one before applying it. When any change carries such a line, or the
 * list is cut short, Apply is a plain button that asks first and repeats those
 * lines (and that not every change is listed); Discard, the safe
 * choice, is never styled as the dangerous one.
 *
 * Rendered in the Security sub-nav's `banner` slot, so the bell note's link
 * (`/settings/security?highlight=security.config-pending`) lands on it whichever
 * section is open and at any width. Nothing renders while no file is pending,
 * nor for a viewer who is not the owner (the read is refused, and the bell note
 * the gateway raised stays until the edit is resolved).
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ShieldAlert } from 'lucide-react'
import { api, type ConfigPendingChange, type ConfigPendingData, type ConfigPendingLeaf } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import { Btn } from '../../components/ui'
import { useConfirm } from '../../components/ConfirmDialog'
import ErrorNotice from '../../components/ErrorNotice'
import { isOwnerOnlyRefusal } from '../../components/sessionEnvironmentHooks'
import { i18nT } from '../../i18n/t'
import { reportForError } from '../../utils/errorReport'
import { CONFIG_PENDING_QUERY_KEY, CONFIG_PENDING_SETTING_ID, useConfigPending } from './configPendingQuery'

/** Show a value the gateway allowed (a number, a short string, a flat list) as JSON,
 *  so a string and a number with the same text stay distinguishable. */
const shown = (value: unknown): string => JSON.stringify(value) ?? String(value)

/** One sentence per change; static keys so the i18n gates can see each one. */
export function describeChange(c: ConfigPendingChange): string {
  const key = c.path.join('.')
  const hasBefore = 'before' in c
  const hasAfter = 'after' in c
  switch (c.change) {
    case 'added':
      if (c.redacted) return i18nT('pages.settings.securityPanel.configPending.added_hidden', { key })
      return hasAfter
        ? i18nT('pages.settings.securityPanel.configPending.added_value', { key, after: shown(c.after) })
        : i18nT('pages.settings.securityPanel.configPending.added', { key })
    case 'removed':
      if (c.redacted) return i18nT('pages.settings.securityPanel.configPending.removed_hidden', { key })
      return hasBefore
        ? i18nT('pages.settings.securityPanel.configPending.removed_value', { key, before: shown(c.before) })
        : i18nT('pages.settings.securityPanel.configPending.removed', { key })
    default:
      if (c.redacted) return i18nT('pages.settings.securityPanel.configPending.changed_hidden', { key })
      return hasBefore && hasAfter
        ? i18nT('pages.settings.securityPanel.configPending.changed_value', { key, before: shown(c.before), after: shown(c.after) })
        : i18nT('pages.settings.securityPanel.configPending.changed', { key })
  }
}

type FileKind = 'config' | 'denied' | 'computerUse'

/** Which sealed settings file *leaf* (the gateway's file name) is. */
function fileKind(leaf: string): FileKind | null {
  if (leaf === 'config.json' || leaf === 'config.local.json') return 'config'
  if (leaf === 'denied_commands.json') return 'denied'
  if (leaf === 'computer_use.json') return 'computerUse'
  return null
}
const kept = (c: ConfigPendingChange) => c.change !== 'removed'

/** The keys whose change can loosen confinement, per file, and what the change
 *  does in plain words. A display hint only: it decides nothing the gateway
 *  enforces, and a key not listed here keeps the plain row. The gateway lists
 *  changes to these same keys first (`_SECURITY_KEYS` in
 *  `config/loosening_provenance.py`), so a capped list never drops one. */
const SECURITY_RULES: ReadonlyArray<{
  file: FileKind
  key: string
  hint: (c: ConfigPendingChange) => string | null
}> = [
  {
    file: 'config',
    key: 'agent.sandbox',
    // A removed key falls back to whatever the other file says, which can be `off`.
    hint: c => kept(c) && c.after === 'off'
      ? i18nT('pages.settings.securityPanel.configPending.hint_sandbox_off')
      : i18nT('pages.settings.securityPanel.configPending.hint_sandbox_changed'),
  },
  {
    file: 'config',
    key: 'agent.sandbox_allow_unsandboxed_exec',
    hint: c => c.after === true ? i18nT('pages.settings.securityPanel.configPending.hint_unsandboxed_exec') : null,
  },
  {
    file: 'config',
    key: 'agent.apps_allow_third_party',
    hint: c => c.after === true ? i18nT('pages.settings.securityPanel.configPending.hint_third_party_apps') : null,
  },
  {
    file: 'config',
    key: 'hooks.auto_approve_tools',
    hint: c => kept(c) ? i18nT('pages.settings.securityPanel.configPending.hint_auto_approve') : null,
  },
  // The standing approve-every-tool grant, in each spelling the gateway reads. Any
  // change to it (removing a `false` included) can change which tools ask first.
  {
    file: 'config',
    key: 'agent.dangerously_skip_permissions',
    hint: () => i18nT('pages.settings.securityPanel.configPending.hint_auto_approve'),
  },
  {
    file: 'config',
    key: 'agent.dangerouslySkipPermissions',
    hint: () => i18nT('pages.settings.securityPanel.configPending.hint_auto_approve'),
  },
  {
    file: 'config',
    key: 'agent.yolo',
    hint: () => i18nT('pages.settings.securityPanel.configPending.hint_auto_approve'),
  },
  {
    file: 'config',
    key: 'agent.env',
    hint: c => kept(c) ? i18nT('pages.settings.securityPanel.configPending.hint_agent_env') : null,
  },
  {
    file: 'denied',
    key: 'disable_all',
    hint: c => c.after === true ? i18nT('pages.settings.securityPanel.configPending.hint_deny_all_off') : null,
  },
  {
    file: 'denied',
    key: 'disabled_ids',
    hint: c => kept(c) ? i18nT('pages.settings.securityPanel.configPending.hint_deny_rules') : null,
  },
  {
    file: 'denied',
    key: 'user_added',
    hint: () => i18nT('pages.settings.securityPanel.configPending.hint_deny_rules'),
  },
  {
    file: 'computerUse',
    key: 'enabled',
    hint: c => c.after === true ? i18nT('pages.settings.securityPanel.configPending.hint_computer_use_on') : null,
  },
]

/** The plain-words security lines for one change of *leaf*: the rule's own line
 *  for that key or a key under it, and a general one when a whole section that
 *  holds such a key was added, replaced or removed (its value is not shown). */
export function securityHints(leaf: string, c: ConfigPendingChange): string[] {
  const key = c.path.join('.')
  const file = fileKind(leaf)
  const hints = new Set<string>()
  for (const rule of SECURITY_RULES) {
    if (rule.file !== file) continue
    if (key === rule.key || key.startsWith(rule.key + '.')) {
      const hint = rule.hint(c)
      if (hint) hints.add(hint)
    } else if (rule.key.startsWith(key + '.')) {
      hints.add(i18nT('pages.settings.securityPanel.configPending.hint_section'))
    }
  }
  return [...hints]
}

type Op = 'apply' | 'discard'

function ConfigPendingCard({ item }: { item: ConfigPendingLeaf }) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const file = item.leaf
  // The version whose Apply the gateway refused: Apply stays off for it until the
  // file changes (a new signature), since the same content is refused again.
  const [refusedSignature, setRefusedSignature] = useState<string | null>(null)
  // The signature travels with the call, read when the button was pressed: the
  // card can re-render with a newer version while a confirm is open, and the
  // owner confirmed the version they saw.
  const run = useMutation({
    mutationFn: ({ op, signature }: { op: Op; signature: string }) => op === 'apply'
      ? api.applyConfigPending(item.leaf, signature)
      : api.discardConfigPending(item.leaf, signature),
    onSuccess: (data: ConfigPendingData) => qc.setQueryData(CONFIG_PENDING_QUERY_KEY, data),
    onError: (error, { op, signature }) => {
      if (op === 'apply' && apiErrorCode(error) === 'write_refused') setRefusedSignature(signature)
    },
    // Re-read on every settlement: a 409 `pending_changed` means the file moved
    // since this diff was drawn, and a write whose response was lost may have landed.
    onSettled: () => void qc.invalidateQueries({ queryKey: CONFIG_PENDING_QUERY_KEY }),
  })
  const code = run.isError ? apiErrorCode(run.error) : ''
  // Kept across the re-read that follows: after a 409 `pending_changed` the card
  // shows the newer version, and this notice says why the list moved.
  const changedAgain = code === 'pending_changed'
  // A 409 `write_refused` for THIS version: the file's own save rules refuse the
  // edit, and the gateway's plain reason says which value. The file stays pending.
  const refusedHere = refusedSignature === item.signature
  const refused = code === 'write_refused'
  const errorText = run.error instanceof Error ? run.error.message : String(run.error)
  const report = run.isError ? reportForError(run.error) : undefined
  const removed = item.removed === true
  const invalidId = `config-pending-invalid-${file}`
  const refusedId = `config-pending-refused-${file}`
  const applyBlockedBy = removed || !item.disk_valid ? invalidId : refused && refusedHere ? refusedId : undefined
  // Every security line the listed changes carry. Any line makes Apply a neutral
  // button that asks first and restates what the edit does; with none, Apply
  // stays the card's primary action.
  const editHints = [...new Set(item.changes.flatMap(c => securityHints(item.leaf, c)))]
  const sandboxOff = i18nT('pages.settings.securityPanel.configPending.hint_sandbox_off')

  // A truncated list may leave out a change the owner should weigh, so Apply asks
  // first and says not every change is listed.
  const asksFirst = editHints.length > 0 || item.truncated

  const apply = async () => {
    const signature = item.signature
    if (asksFirst) {
      const ok = await confirm({
        title: i18nT('pages.settings.securityPanel.configPending.apply_title', { file }),
        body: (
          <>
            {editHints.length > 0 && (
              <>
                <p className="m-0">{i18nT('pages.settings.securityPanel.configPending.apply_body')}</p>
                <ul data-testid="config-pending-apply-hints" className="m-0 mt-1.5 pl-5 list-disc space-y-0.5">
                  {editHints.map(hint => <li key={hint}>{hint}</li>)}
                </ul>
              </>
            )}
            {item.truncated && (
              <p data-testid="config-pending-apply-truncated" className="m-0 mt-1.5">
                {i18nT('pages.settings.securityPanel.configPending.truncated')}
              </p>
            )}
          </>
        ),
        confirmLabel: editHints.length === 1 && editHints[0] === sandboxOff
          ? i18nT('pages.settings.securityPanel.configPending.apply_turn_off_sandbox')
          : i18nT('pages.settings.securityPanel.configPending.apply_edit'),
      })
      if (!ok) return
    }
    run.mutate({ op: 'apply', signature })
  }

  const discard = async () => {
    const signature = item.signature
    const ok = await confirm({
      title: i18nT('pages.settings.securityPanel.configPending.discard_title', { file }),
      body: i18nT('pages.settings.securityPanel.configPending.discard_body', { file }),
      confirmLabel: i18nT('pages.settings.securityPanel.configPending.discard'),
    })
    if (ok) run.mutate({ op: 'discard', signature })
  }

  return (
    <div
      data-setting-id={CONFIG_PENDING_SETTING_ID}
      data-setting-label={i18nT('pages.settings.securityPanel.configPending.nav_dot')}
      data-testid="config-pending-card"
      className="flex flex-col gap-2 border border-warn/40 bg-warn-subtle rounded-md px-3 py-2.5 mb-3 min-w-0"
    >
      <div className="flex items-start gap-2 min-w-0">
        <AlertTriangle size={14} className="shrink-0 mt-0.5 text-warn-text" aria-hidden="true" />
        <div className="min-w-0">
          <h3 className="m-0 text-[13px] font-medium text-text break-words">
            {i18nT('pages.settings.securityPanel.configPending.title', { file })}
          </h3>
          <p className="m-0 mt-0.5 text-[12px] text-muted leading-relaxed">
            {i18nT('pages.settings.securityPanel.configPending.intro')}
          </p>
        </div>
      </div>
      {removed ? (
        <p id={invalidId} className="m-0 text-[12px] text-warn-text">
          {i18nT('pages.settings.securityPanel.configPending.removed_file')}
        </p>
      ) : item.disk_valid ? (
        item.changes.length > 0 ? (
          <ul className="m-0 pl-5 list-disc text-[12px] text-text space-y-0.5 min-w-0">
            {item.changes.map((c, i) => {
              const hints = securityHints(item.leaf, c)
              return (
                // The gateway cuts a long path, so two rows can show the same one.
                <li key={`${i}\u0000${c.path.join('\u0000')}`} className="break-words">
                  {describeChange(c)}
                  {hints.map(hint => (
                    <span
                      key={hint}
                      data-testid="config-pending-security-hint"
                      className="flex items-start gap-1 mt-0.5 font-medium text-danger-text"
                    >
                      <ShieldAlert size={12} className="shrink-0 mt-0.5" aria-hidden="true" />
                      {hint}
                    </span>
                  ))}
                </li>
              )
            })}
          </ul>
        ) : (
          <p className="m-0 text-[12px] text-muted">{i18nT('pages.settings.securityPanel.configPending.no_key_changes')}</p>
        )
      ) : (
        <p id={invalidId} className="m-0 text-[12px] text-warn-text">
          {i18nT('pages.settings.securityPanel.configPending.invalid_json')}
        </p>
      )}
      {item.truncated && (
        <p className="m-0 text-[12px] text-muted">{i18nT('pages.settings.securityPanel.configPending.truncated')}</p>
      )}
      {changedAgain ? (
        <ErrorNotice
          askAgent
          message={i18nT('pages.settings.securityPanel.configPending.pending_changed')}
          report={report}
        />
      ) : refused ? (
        // A refusal for an older version is moot: the list now shows the newer one.
        refusedHere && (
          <ErrorNotice
            id={refusedId}
            askAgent
            title={i18nT('pages.settings.securityPanel.configPending.apply_refused_action')}
            message={errorText}
            messagePlacement="below"
            report={report}
          />
        )
      ) : run.isError && (
        <ErrorNotice askAgent message={errorText} report={report} />
      )}
      <div className="flex flex-wrap gap-2">
        <Btn
          primary={!asksFirst}
          disabled={applyBlockedBy !== undefined || refusedHere || run.isPending}
          aria-describedby={applyBlockedBy}
          aria-label={i18nT('pages.settings.securityPanel.configPending.apply_file', { file })}
          onClick={() => void apply()}
        >
          {i18nT('pages.settings.securityPanel.configPending.apply')}
        </Btn>
        {/* Neutral, not danger: putting back the saved version is the safe choice. */}
        <Btn
          disabled={run.isPending}
          aria-label={i18nT('pages.settings.securityPanel.configPending.discard_file', { file })}
          onClick={() => void discard()}
        >
          {i18nT('pages.settings.securityPanel.configPending.discard')}
        </Btn>
      </div>
      {confirmDialog}
    </div>
  )
}

export function ConfigPendingCards() {
  // `data` survives a failed refetch, so a pending edit already shown stays shown.
  const { data, error, refetch, isFetching } = useConfigPending()
  // A refused or failed read is shown, above any cards an earlier read left, except
  // the owner-only refusal every other viewer gets by design. A transport failure is
  // shown too: the socket can stay connected while the fetch fails, and then no
  // offline state would say the list may be stale.
  const readFailed = error != null && !isOwnerOnlyRefusal(error)
  const pending = data?.count && Array.isArray(data.pending) ? data.pending : []
  if (!readFailed && pending.length === 0) return null
  return (
    <>
      {readFailed && (
        <ErrorNotice
          askAgent
          className="mb-3"
          message={i18nT('pages.settings.securityPanel.configPending.read_failed')}
          report={reportForError(error)}
          testId="config-pending-read-error"
          footer={(
            <Btn onClick={() => void refetch()} disabled={isFetching} data-testid="config-pending-read-retry">
              {i18nT('pages.settings.chatPanel.retry')}
            </Btn>
          )}
        />
      )}
      {pending.map(item => <ConfigPendingCard key={item.leaf} item={item} />)}
    </>
  )
}
