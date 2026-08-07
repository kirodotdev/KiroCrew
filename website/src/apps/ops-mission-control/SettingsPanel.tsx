/**
 * Ops Mission Control — Settings.
 *
 * Without this panel the app cannot be set up at all: providers ship disabled and
 * their credentials live in a keystone store the agent cannot reach, so a human
 * with a browser is the only thing that can turn one on.
 *
 * Two invariants the UI must not break:
 *
 * - **Secrets are write-only.** The API never returns a stored token, so a field
 *   that is already set shows a placeholder and an explicit Replace action rather
 *   than a pre-filled value. Never round-trip a secret through this form.
 * - **Autonomy is a ceiling.** The mode selector sets the app-level maximum; a
 *   per-signal rule can only narrow it. The copy says so, because "act" reads as
 *   a feature and is actually a grant of write access to production.
 *
 * i18n — DONE. Every string in this app's five components and `api.ts` routes through
 * `i18nT` (~310 keys), and the keys are mirrored into all nine non-English catalogs so
 * `catalogParity.test.ts` passes. An earlier revision of this comment argued at length
 * that extraction was a core-i18n change belonging in its own PR; that turned out to be
 * wrong, and the reason is worth keeping because it is the trap:
 *
 * `scripts/i18n-codemod.mjs --merge` is whole-corpus by design (no path scope), so a run
 * also rewrites files this app does not own. The answer is not to defer — it is to revert
 * the files outside this app after the run. Three were dragged in here (ComputerUseLiveView,
 * ChatSidebar, TelemetryPanel) and reverted; their three leaked catalog keys had to be
 * removed by hand too, because `deadKeys.test.ts` counts keys nothing references.
 *
 * What the nine catalogs carry for these keys is the **English fallback**, not a
 * translation. That is deliberate and is the interim state `i18n-translate.mjs` exists to
 * replace: parity checks key sets, placeholders and non-emptiness — not translation
 * quality — and only `destructiveConfirm.test.ts`'s three SchedulePage keys must genuinely
 * differ from English. A real translation pass for these 310 keys is still open.
 *
 * Do NOT hand-edit `en.json` to ADD keys — it is generated. The codemod refuses a
 * non-`--merge` run once the corpus is converted, which is what stops a rebuild from
 * silently wiping the catalog.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Bell,
  BellRing,
  CalendarClock,
  Clock,
  Eye,
  FolderGit2,
  GitBranch,
  Info,
  MessageSquare,
  Radio,
  UserCheck,
  Zap,
  KeyRound,
  Trash2,
  ShieldAlert,
} from 'lucide-react'
import { Badge, Btn, Card, CardTitle, Input, SendBtn, Toggle } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { fmtUnit } from '../../i18n/format'
import SegmentedControl from '../../components/SegmentedControl'
import SimpleSelect from '../../components/SimpleSelect'
import {
  opsApi,
  type AutonomyRule,
  type CompanionInfo,
  type LedgerSyncStatus,
  type NotifyOutStatus,
  type OperatingMode,
  type ProviderInfo,
  type RotationRoster,
  type SlackOutStatus,
  type SweepWindows,
} from './api'

/** Module-level frozen empty so the render-time fallback is referentially stable. */
const EMPTY_COMPANIONS: readonly CompanionInfo[] = Object.freeze([])


const MODE_HELP_KEY = {
  observe: 'apps.opsMissionControl.settingsPanel.mode_help_observe',
  propose: 'apps.opsMissionControl.settingsPanel.mode_help_propose',
  act: 'apps.opsMissionControl.settingsPanel.mode_help_act',
} as const

function ProviderRow({
  provider,
  fencedIdentity,
}: {
  provider: ProviderInfo
  /**
   * A rotation identity this adapter needs but CANNOT declare as a config field, because it is
   * an input to the off-shift refusal and provider config is agent-writable (see
   * `policy_store.OPERATOR_ONLY_KEYS`). Supplied by the panel from the rotation response and
   * written through `PUT /settings`, so the row can still render one editable control for it.
   * Undefined for every adapter that has no such field.
   */
  fencedIdentity?: { label: string; settingsKey: string; value: string; help: string }
}) {
  const queryClient = useQueryClient()
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({})
  const [configDrafts, setConfigDrafts] = useState<Record<string, string>>({})

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'providers'] })
    queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
  }

  const configMutation = useMutation({
    mutationFn: (updates: Record<string, unknown>) =>
      opsApi.putProviderConfig(provider.id, updates),
    onSuccess: invalidate,
  })

  const secretMutation = useMutation({
    mutationFn: ({ field, value }: { field: string; value: string }) =>
      opsApi.putSecret(provider.id, field, value),
    onSuccess: (_data, variables) => {
      // Drop the draft immediately so a token never lingers in component state
      // longer than the request that stored it.
      setSecretDrafts((prev) => ({ ...prev, [variables.field]: '' }))
      invalidate()
    },
  })

  const revokeMutation = useMutation({
    mutationFn: () => opsApi.deleteSecret(provider.id),
    onSuccess: invalidate,
  })

  const [identityDraft, setIdentityDraft] = useState<string | undefined>(undefined)
  const identityMutation = useMutation({
    // `PUT /settings`, the keystone's sole writer — NOT `putProviderConfig`, which writes the
    // agent-writable file this value was moved off.
    mutationFn: (value: string) =>
      opsApi.putSettings({ [fencedIdentity!.settingsKey]: value }),
    onSuccess: () => {
      setIdentityDraft(undefined)
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'rotation'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })
  const commitIdentity = () => {
    if (identityDraft === undefined || identityMutation.isPending) return
    identityMutation.mutate(identityDraft.trim())
  }

  const enabled = Boolean(provider.config?.enabled)
  // Fields other than the enable flag, which has its own toggle.
  const editableFields = provider.config_fields.filter((f) => f !== 'enabled')

  // Not every adapter HAS an enable flag, and painting a toggle for one that does not
  // was a dead control. `_handle_put_provider_config` 400s any key the adapter did not
  // declare, and the rotation adapters declare none: "Schedule file (git)" now declares
  // NOTHING (both of its fields moved onto the keystone floor — see the on-call card),
  // and "Observe only" / "Always on shift" never declared any. So their toggles rejected
  // every click — and because the error line lived INSIDE the block the toggle gates, the
  // rejection was invisible: the operator clicked, nothing moved, and no message appeared.
  //
  // The fix is here and not in the adapter: `schedule_file.configured()` deliberately keys
  // on the schedule FILE existing, so adding an `enabled` field would invent a flag that
  // gates nothing. And it must STAY absent for the rotation sources — the off-shift write
  // gate no longer consults `configured()` at all, precisely because that predicate reads
  // an agent-writable flag, so a new `enabled` here would be a control the security path
  // deliberately ignores.
  const hasEnableFlag = provider.config_fields.includes('enabled')
  const fieldsVisible = enabled || !hasEnableFlag
  const writeError = configMutation.isError || secretMutation.isError

  return (
    <div className="border-t border-border py-3">
      <div className="flex items-center gap-2 mb-1">
        {hasEnableFlag ? (
          <Toggle
            checked={enabled}
            onChange={(v) => configMutation.mutate({ enabled: v })}
            label={i18nT('apps.opsMissionControl.settingsPanel.enable_provider', {
              provider: provider.display_name,
            })}
          />
        ) : null}
        <span className="text-sm font-medium">{provider.display_name}</span>
        <Badge variant={provider.configured ? 'ok' : 'muted'}>
          {provider.configured
            ? i18nT('apps.opsMissionControl.settingsPanel.ready')
            : i18nT('apps.opsMissionControl.settingsPanel.not_set_up')}
        </Badge>
        <span className="text-[12px] text-muted ml-auto">{provider.roles.join(' · ')}</span>
      </div>
      <p className="text-[12px] text-muted mb-2">{provider.detail}</p>

      {/* The fenced identity renders whether or not the enable toggle is on: it is not
          provider config, it is a keystone value this adapter happens to consume, and gating it
          behind `enabled` would hide the field an instance needs to recognise ITSELF — the same
          dead-control bug the comment above describes, one level over. */}
      {fencedIdentity ? (
        <div className={`flex flex-col gap-2 mb-2 ${hasEnableFlag ? 'pl-11' : ''}`}>
          <label
            className="flex items-center gap-2 text-[13px]"
            htmlFor={`omc-${provider.id}-fenced-identity`}
          >
            <span className="w-44 shrink-0 text-muted">{fencedIdentity.label}</span>
            <Input
              id={`omc-${provider.id}-fenced-identity`}
              value={identityDraft ?? fencedIdentity.value}
              placeholder="—"
              onChange={(e) => setIdentityDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitIdentity()
              }}
            />
            <SendBtn
              disabled={
                identityDraft === undefined
                || identityDraft === fencedIdentity.value
                || identityMutation.isPending
              }
              onClick={commitIdentity}
            >
              {i18nT('apps.opsMissionControl.settingsPanel.save')}
            </SendBtn>
          </label>
          <p className="text-[12px] text-muted">{fencedIdentity.help}</p>
        </div>
      ) : null}

      {fieldsVisible && (editableFields.length > 0 || provider.secret_fields.length > 0) ? (
        <div className={`flex flex-col gap-2 ${hasEnableFlag ? 'pl-11' : ''}`}>
          {editableFields.map((field) => {
            const inputId = `omc-${provider.id}-${field}`
            // The input is BOTH nested in the label and bound by htmlFor/id:
            // jsx-a11y/label-has-for requires both forms.
            return (
              <label
                key={field}
                htmlFor={inputId}
                className="flex items-center gap-2 text-[13px]"
              >
                <span className="w-44 shrink-0 text-muted">{field}</span>
                <Input
                  id={inputId}
                  value={configDrafts[field] ?? String(provider.config?.[field] ?? '')}
                  onChange={(e) =>
                    setConfigDrafts((prev) => ({ ...prev, [field]: e.target.value }))
                  }
                  onBlur={() => {
                    const draft = configDrafts[field]
                    if (draft !== undefined && draft !== String(provider.config?.[field] ?? '')) {
                      configMutation.mutate({ [field]: draft })
                    }
                  }}
                  placeholder="—"
                />
              </label>
            )
          })}

          {provider.secret_fields.map((field) => {
            const isSet = Boolean(provider.secrets?.[field])
            const secretId = `omc-${provider.id}-secret-${field}`
            return (
              <label
                key={field}
                htmlFor={secretId}
                className="flex items-center gap-2 text-[13px]"
              >
                <span className="w-44 shrink-0 text-muted flex items-center gap-1">
                  <KeyRound className="lucide-inline" /> {field}
                </span>
                <Input
                  id={secretId}
                  type="password"
                  autoComplete="off"
                  value={secretDrafts[field] ?? ''}
                  onChange={(e) =>
                    setSecretDrafts((prev) => ({ ...prev, [field]: e.target.value }))
                  }
                  // Never pre-fill: the API cannot return a stored secret, and a
                  // placeholder that looked like a value would invite a re-save
                  // of the placeholder itself.
                  placeholder={
                    isSet
                      ? i18nT('apps.opsMissionControl.settingsPanel.stored_enter_a_new_value_to_replace')
                      : i18nT('apps.opsMissionControl.settingsPanel.not_set')
                  }
                />
                <SendBtn
                  disabled={!secretDrafts[field] || secretMutation.isPending}
                  onClick={() =>
                    secretMutation.mutate({ field, value: secretDrafts[field] ?? '' })
                  }
                >
                  {isSet
                    ? i18nT('apps.opsMissionControl.settingsPanel.replace')
                    : i18nT('apps.opsMissionControl.settingsPanel.save')}
                </SendBtn>
              </label>
            )
          })}

          {provider.secret_fields.length > 0 && Object.keys(provider.secrets ?? {}).some(
            (k) => provider.secrets?.[k],
          ) ? (
            <div>
              <Btn danger disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>
                <Trash2 className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.revoke_stored_credentials')}
              </Btn>
              {/* Disclose the retention boundary HERE, next to the only control that
                  changes it. Credentials live in a keystone file at the crew-home root
                  (they must, for the sensitive-path floor), NOT under the app
                  directory — so uninstalling the app cannot remove them, and nothing
                  else tells the user that. Revoking before uninstall is the only way
                  to be sure a token is gone. */}
              <p className="text-[12px] text-muted mt-1.5">
                {i18nT('apps.opsMissionControl.settingsPanel.credential_retention_note')}
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* OUTSIDE the block the enable toggle gates. It used to be inside, which meant the
          one write most likely to be rejected — the toggle itself, on an adapter with no
          `enabled` field — failed with no visible message at all. A rejected write must
          always be able to say so. */}
      {writeError ? (
        <p className={`text-[12px] text-danger ${hasEnableFlag ? 'pl-11' : ''}`}>
          {((configMutation.error ?? secretMutation.error) as Error)?.message}
        </p>
      ) : null}
    </div>
  )
}

/**
 * Slack output channel — the pin board.
 *
 * Deliberately has NO token field. Kiro Crew already holds a Slack bot token for
 * its own gateway and this app reuses that client, so there is no second
 * credential to enter, store, or rotate. The consequence is a real dependency
 * rather than a hidden one: when Kiro Crew's Slack is not connected, this card says
 * so and points at the fix instead of silently doing nothing.
 */
function SlackOutCard({
  status,
  onSave,
  saving,
}: {
  status?: SlackOutStatus
  onSave: (updates: Record<string, unknown>) => void
  saving: boolean
}) {
  // Local draft so the field is editable without a save per keystroke; seeded
  // from the server value and re-seeded when it changes underneath us.
  const [channel, setChannel] = useState(status?.channel ?? '')
  const [touched, setTouched] = useState(false)
  const serverChannel = status?.channel ?? ''
  useEffect(() => {
    if (!touched) setChannel(serverChannel)
  }, [serverChannel, touched])

  const enabled = Boolean(status?.enabled)
  const dirty = touched && channel.trim() !== serverChannel

  return (
    <Card>
      <CardTitle>
        <MessageSquare className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.slack')}
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        {i18nT('apps.opsMissionControl.settingsPanel.mirror_incidents_to_a_channel_as_a_live_board_on')}
      </p>

      <div className="flex items-center gap-2 text-[13px]">
        <Toggle
          checked={enabled}
          onChange={(v) => onSave({ slack_enabled: v })}
          label={i18nT('apps.opsMissionControl.settingsPanel.mirror_incidents_to_slack')}
        />
        <span>{i18nT('apps.opsMissionControl.settingsPanel.mirror_incidents_to_slack')}</span>
        {status ? (
          <Badge variant={status.ready ? 'ok' : enabled ? 'warn' : 'muted'}>
            {status.ready
              ? i18nT('apps.opsMissionControl.settingsPanel.active')
              : enabled
                ? i18nT('apps.opsMissionControl.settingsPanel.needs_setup')
                : i18nT('apps.opsMissionControl.settingsPanel.off')}
          </Badge>
        ) : null}
      </div>

      {enabled ? (
        <div className="mt-3 flex flex-col gap-2">
          {/* Input BOTH nested and bound by htmlFor/id — jsx-a11y/label-has-for
              requires both forms, matching the provider rows above. */}
          <label
            className="flex items-center gap-2 text-[13px]"
            htmlFor="omc-slack-channel"
          >
            <span className="w-44 shrink-0 text-muted">{i18nT('apps.opsMissionControl.settingsPanel.channel_id')}</span>
            <Input
              id="omc-slack-channel"
              value={channel}
              placeholder={i18nT('apps.opsMissionControl.settingsPanel.c0123456789')}
              onChange={(e) => {
                setTouched(true)
                setChannel(e.target.value)
              }}
            />
            <SendBtn
              disabled={!dirty || saving}
              onClick={() => {
                onSave({ slack_channel: channel.trim() })
                setTouched(false)
              }}
            >
              {i18nT('apps.opsMissionControl.settingsPanel.save')}
            </SendBtn>
          </label>
          <p className="text-[12px] text-muted">
            {i18nT('apps.opsMissionControl.settingsPanel.find_it_at_the_bottom_of_the_channel_s_detail_di')}
          </p>
        </div>
      ) : null}

      {/* The backend already distinguishes the three failure modes and names the
          fix for each; rendering its sentence beats re-deriving that here. */}
      {status && !status.ready && enabled ? (
        <p className="text-[13px] text-warn mt-3 flex items-start gap-1.5">
          <AlertTriangle className="lucide-inline" />
          <span>{status.detail}</span>
        </p>
      ) : null}
    </Card>
  )
}

/** Lucide component per channel icon name declared in `app.json`. */
const CHANNEL_ICONS: Record<string, JSX.Element> = {
  UserCheck: <UserCheck className="lucide-inline" />,
  Radio: <Radio className="lucide-inline" />,
  Clock: <Clock className="lucide-inline" />,
}

/**
 * What each declared channel actually fires on.
 *
 * Keyed by the manifest id, so a channel the backend declares and this map does not know
 * still renders (with its name and priority) rather than disappearing — an unexplained
 * channel is better than a hidden one. Deliberately phrased as the EDGE condition,
 * because that is the contract: the backend pushes on a state change and never on a tick,
 * and copy that said "when a source is unhealthy" would read as a recurring alert.
 */
const CHANNEL_WHEN_KEY = {
  'waiting-on-you': 'apps.opsMissionControl.settingsPanel.channel_when_waiting_on_you',
  'source-health': 'apps.opsMissionControl.settingsPanel.channel_when_source_health',
  'incident-released': 'apps.opsMissionControl.settingsPanel.channel_when_incident_released',
} as const

/** `CHANNEL_WHEN_KEY`'s own key type. `ch.id` is an arbitrary runtime string from the
 * manifest, and `tsc -b` refuses to index a literal object with `string` (TS7053) —
 * while widening the map to `Record<string, string>` would erase the literal values that
 * let `check-i18n-keys` verify each key exists. Narrow at the index site to keep both. */
type ChannelWhenId = keyof typeof CHANNEL_WHEN_KEY
const isKnownChannel = (id: string): id is ChannelWhenId => id in CHANNEL_WHEN_KEY

/**
 * Local desktop notifications.
 *
 * This card exists because the app declared the `notification` permission from day one
 * and never produced one, so the only push channel that needs NO credential and no
 * inbound URL was inert — every fact this app computed required an open dashboard tab or
 * a Slack workspace it holds no token for.
 *
 * Deliberately has NO per-channel mute control. Kiro Crew renders that centrally at
 * Settings → Notifications (one row per channel with a mute switch and a priority
 * override), and a second copy here would be two controls that can disagree about the
 * same stored setting. What this card owns instead is the app-level on/off and the
 * DECLARATION of which channels exist — which the central rail cannot show for a freshly
 * installed app, because it lists channels only once they have been registered and
 * registration happens on a channel's first push.
 */
function NotifyOutCard({
  status,
  onSave,
}: {
  status?: NotifyOutStatus
  onSave: (updates: Record<string, unknown>) => void
}) {
  const enabled = Boolean(status?.enabled)
  const channels = status?.channels ?? []

  return (
    <Card>
      <CardTitle>
        <BellRing className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.desktop_notifications')}
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        {i18nT('apps.opsMissionControl.settingsPanel.get_a_notification_when_something_changes_that_n')}
      </p>

      <div className="flex items-center gap-2 text-[13px]">
        <Toggle
          checked={enabled}
          onChange={(v) => onSave({ notify_enabled: v })}
          label={i18nT('apps.opsMissionControl.settingsPanel.notify_me_on_state_changes')}
        />
        <span>{i18nT('apps.opsMissionControl.settingsPanel.notify_me_on_state_changes')}</span>
        {/* Three states, not two, and `bus_available` is what separates the middle one.
            "needs setup" was rendered for every not-ready case — including the one where
            there is no setup to do: the bus lives on the running gateway, so its absence is
            not something a toggle, a field or a credential fixes. Telling an operator to
            set something up when nothing they can reach would help is advice that cannot
            work, and they would go looking for the missing field. Unlike the Slack card,
            where the not-ready half genuinely IS the operator's (connect Kiro Crew's Slack),
            so "needs setup" is honest there. */}
        {status ? (
          <Badge
            variant={status.ready ? 'ok' : !enabled ? 'muted' : status.bus_available ? 'warn' : 'err'}
            title={status.detail}
          >
            {status.ready
              ? i18nT('apps.opsMissionControl.settingsPanel.active')
              : !enabled
                ? i18nT('apps.opsMissionControl.settingsPanel.off')
                : status.bus_available
                  ? i18nT('apps.opsMissionControl.settingsPanel.needs_setup')
                  : i18nT('apps.opsMissionControl.settingsPanel.unavailable_here')}
          </Badge>
        ) : null}
      </div>

      {/* The declaration, not the bus registry — see api.ts. Shown whether or not the
          channel is on, because "which channels could speak to me" is the question an
          operator has BEFORE deciding to enable this. */}
      {channels.length > 0 ? (
        <dl className="mt-3 flex flex-col gap-2">
          {channels.map((ch) => (
            <div key={ch.id} className="flex items-start gap-2 text-[13px]">
              <dt className="flex items-center gap-1.5 w-44 shrink-0">
                {CHANNEL_ICONS[ch.icon] ?? <Bell className="lucide-inline" />}
                <span>{ch.name}</span>
              </dt>
              <dd className="text-muted">
                {isKnownChannel(ch.id)
                  ? i18nT(CHANNEL_WHEN_KEY[ch.id])
                  : i18nT('apps.opsMissionControl.settingsPanel.when_a_state_changes')}
                {ch.default_priority === 'critical' ? (
                  <span className="text-warn"> {i18nT('apps.opsMissionControl.settingsPanel.interrupts_by_default')}</span>
                ) : ch.default_priority === 'passive' ? (
                  <span> {i18nT('apps.opsMissionControl.settingsPanel.quiet_and_expires_on_its_own')}</span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      <p className="text-[12px] text-muted mt-3">
        {i18nT('apps.opsMissionControl.settingsPanel.one_notification_per_state_change_never_per_hear')}
      </p>

      {/* Mute lives centrally. Say where, rather than adding a control that would fight
          the one Kiro Crew already stores. */}
      {enabled ? (
        <p className="text-[12px] text-muted mt-1 flex items-start gap-1.5">
          <Info className="lucide-inline" />
          <span>
            {i18nT('apps.opsMissionControl.settingsPanel.to_silence_one_of_these_without_turning_the_rest')}
          </span>
        </p>
      ) : null}

      {/* The backend distinguishes off from no-bus and names the fix for each; render its
          sentence rather than re-deriving that here. */}
      {status && !status.ready && enabled ? (
        <p className="text-[13px] text-warn mt-3 flex items-start gap-1.5">
          <AlertTriangle className="lucide-inline" />
          <span>{status.detail}</span>
        </p>
      ) : null}
    </Card>
  )
}

/**
 * Hide a `userinfo@` component before a remote URL is painted.
 *
 * `https://user:ghp_xxx@github.com/org/repo.git` → `https://github.com/org/repo.git`.
 *
 * Not theatre, and not a claim that the value was cleaned: `config.json` is served
 * UNAUTHENTICATED (see `providers.write_config`), the write path only length-caps the
 * remote, and `secrets.redact_tokens` has no pattern for a PAT embedded in a URL. So if an
 * operator pastes a token-bearing remote it IS stored in a world-readable file, and this
 * function changes nothing about that. What it does is stop this panel from being a second
 * place the token is displayed — a screenshot or a screen-share of Settings should not leak
 * a credential the operator has already been told not to enter here.
 */
export function displayRemote(url: string): string {
  const trimmed = url.trim()
  // Scheme-relative and scp-style (`git@host:org/repo`) remotes have no userinfo to strip;
  // the `//` requirement is what distinguishes them from a URL that does.
  const marker = trimmed.indexOf('://')
  if (marker < 0) return trimmed
  const rest = trimmed.slice(marker + 3)
  const at = rest.indexOf('@')
  const slash = rest.indexOf('/')
  if (at < 0 || (slash >= 0 && at > slash)) return trimmed
  return `${trimmed.slice(0, marker + 3)}${rest.slice(at + 1)}`
}

/**
 * Shared team memory — the git repo the knowledge ledger syncs through.
 *
 * The backend has accepted these three settings all along and nothing ever sent them, so
 * the app's headline team feature was reachable only by hand-editing `data/config.json`.
 * The owner's report was exactly that: "I do not see where we can specify memory exchange
 * / SOP / on-call schedule repository."
 *
 * Placed with the Slack card because both answer "where does this instance talk to the
 * outside world", and BEFORE the Instance card because that card's nightly-maintenance
 * copy only makes sense to someone who already knows a shared ledger exists.
 */
function SharedMemoryCard({
  status,
  onSave,
  saving,
}: {
  status?: LedgerSyncStatus
  onSave: (updates: Record<string, unknown>) => void
  saving: boolean
}) {
  // Local drafts, seeded from the server and re-seeded while untouched — the same shape
  // as the Slack channel field, so a background /state refresh cannot clobber typing.
  const serverRemote = status?.remote ?? ''
  const serverBranch = status?.branch ?? ''
  const [remote, setRemote] = useState(serverRemote)
  const [branch, setBranch] = useState(serverBranch)
  const [remoteTouched, setRemoteTouched] = useState(false)
  const [branchTouched, setBranchTouched] = useState(false)
  useEffect(() => {
    if (!remoteTouched) setRemote(serverRemote)
  }, [serverRemote, remoteTouched])
  useEffect(() => {
    if (!branchTouched) setBranch(serverBranch)
  }, [serverBranch, branchTouched])

  const enabled = Boolean(status?.enabled)
  const remoteDirty = remoteTouched && remote.trim() !== serverRemote
  // A blank branch is never sent: the backend applies `main` only when the key is absent,
  // so posting "" would persist an empty branch and no default would rescue it.
  const branchDirty = branchTouched && branch.trim() !== '' && branch.trim() !== serverBranch

  const saveRemote = () => {
    if (!remoteDirty || saving) return
    onSave({ ledger_sync_remote: remote.trim() })
    setRemoteTouched(false)
  }
  const saveBranch = () => {
    if (!branchDirty || saving) return
    onSave({ ledger_sync_branch: branch.trim() })
    setBranchTouched(false)
  }

  // A branch problem only matters once the repo exists AND sync is on: `branch_matches` is
  // true for an uninitialized repo by design, so this is the "sync is live and the local
  // repo drifted" case. `detached` narrows it to the half the operator has to fix by hand.
  const branchDrifted = Boolean(status?.ready && status.initialized && !status.branch_matches)

  // Driven ONLY by fields the backend reports. Worst state first, because a schedule
  // conflict makes every push fail and must not be outranked by "syncing".
  //
  // A drifted branch ranks BELOW both conflicts and above "syncing": publishing genuinely
  // still works (the refspecs are explicit), so calling it an error would overstate it —
  // but "syncing" alone is what hid it for the whole life of the feature.
  const badge = status?.schedule_conflict
    ? { variant: 'err' as const, label: i18nT('apps.opsMissionControl.settingsPanel.schedule_conflict') }
    : status?.conflict
      ? { variant: 'warn' as const, label: i18nT('apps.opsMissionControl.settingsPanel.ledger_conflict') }
      : branchDrifted
        ? {
            variant: 'warn' as const,
            label: status?.detached
              ? i18nT('apps.opsMissionControl.settingsPanel.detached_head')
              : i18nT('apps.opsMissionControl.settingsPanel.wrong_local_branch'),
          }
        : status?.ready && status.initialized
          ? { variant: 'ok' as const, label: i18nT('apps.opsMissionControl.settingsPanel.syncing') }
          : status?.ready
            ? { variant: 'ok' as const, label: i18nT('apps.opsMissionControl.settingsPanel.ready') }
            : enabled
              ? { variant: 'warn' as const, label: i18nT('apps.opsMissionControl.settingsPanel.needs_setup') }
              : { variant: 'muted' as const, label: i18nT('apps.opsMissionControl.settingsPanel.off') }

  const troubled = Boolean(
    status && (!status.ready || status.conflict || status.schedule_conflict || branchDrifted),
  )

  return (
    <Card>
      <CardTitle>
        <FolderGit2 className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.shared_team_memory')}
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        {i18nT('apps.opsMissionControl.settingsPanel.ledger_intro', { file: 'ledger.jsonl' })}
      </p>
      {/* The CADENCE, stated because it is not the one the design intended and an operator
          plans around it. `POST /ledger/hygiene` is the only caller of the git transport
          (`grep sync_safely backend/` → routes.py twice, dispatch.py zero), and that route
          runs on the daily `primary`-tier cron — so an instance that is not primary has no
          code path that pulls at all.

          This paragraph replaced "pulled before every match and pushed after every lesson",
          which is what `ledger_sync`'s module docstring still aspires to and what
          `sync_safely`'s own docstring wrongly claims ("the dispatch cycle and the daily
          hygiene pass call this"). Believing it costs a team correctness, not just latency:
          `rotation.yaml` travels in the same repo, so a non-primary instance keeps arming
          off a schedule it may never fetch again — which is the double-claim strict gating
          exists to prevent. Saying the real cadence is the honest half of the fix; moving
          the pull onto an always-tier cron is the other half and is backend work. */}
      <p className="text-[12px] text-muted mb-3 flex items-start gap-1.5">
        <Info className="lucide-inline" />
        <span>
          {i18nT('apps.opsMissionControl.settingsPanel.exchange_happens_on_the_nightly_maintenance_pass')} <span className="font-mono">{i18nT('apps.opsMissionControl.settingsPanel.rotation_yaml')}</span> {i18nT('apps.opsMissionControl.settingsPanel.it_last_saw')}
        </span>
      </p>

      <div className="flex items-center gap-2 text-[13px]">
        <Toggle
          checked={enabled}
          onChange={(v) => onSave({ ledger_sync_enabled: v })}
          label={i18nT('apps.opsMissionControl.settingsPanel.share_the_knowledge_ledger_with_my_team')}
        />
        <span>{i18nT('apps.opsMissionControl.settingsPanel.share_the_knowledge_ledger_with_my_team')}</span>
        {status ? <Badge variant={badge.variant}>{badge.label}</Badge> : null}
      </div>

      <div className="mt-3 flex flex-col gap-2">
        {/* Input BOTH nested and bound by htmlFor/id — jsx-a11y/label-has-for wants both,
            matching every other field in this file. */}
        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-sync-remote">
          <span className="w-44 shrink-0 text-muted">{i18nT('apps.opsMissionControl.settingsPanel.repository')}</span>
          <Input
            id="omc-sync-remote"
            value={remote}
            placeholder={i18nT('apps.opsMissionControl.settingsPanel.git_github_com_your_org_ops_memory_git')}
            onChange={(e) => {
              setRemoteTouched(true)
              setRemote(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveRemote()
            }}
          />
          {/* An explicit Save, not a commit on blur. Tabbing out of a half-pasted URL
              would repoint the whole team's repo, and the backend's branch/length
              refusals need a moment the operator can attribute to their own click. */}
          <SendBtn disabled={!remoteDirty || saving} onClick={saveRemote}>
            {i18nT('apps.opsMissionControl.settingsPanel.save')}
          </SendBtn>
        </label>

        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-sync-branch">
          <span className="w-44 shrink-0 text-muted flex items-center gap-1">
            <GitBranch className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.branch')}
          </span>
          <Input
            id="omc-sync-branch"
            value={branch}
            placeholder={i18nT('apps.opsMissionControl.settingsPanel.main')}
            onChange={(e) => {
              setBranchTouched(true)
              setBranch(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') saveBranch()
            }}
          />
          <SendBtn disabled={!branchDirty || saving} onClick={saveBranch}>
            {i18nT('apps.opsMissionControl.settingsPanel.save')}
          </SendBtn>
        </label>

        {/* The honest boundary. There is genuinely no credential to enter — sync shells out
            to git, which uses whatever the operator already has — and saying so is the only
            thing that stops someone reaching for a token-bearing HTTPS URL, since this
            config file is served without auth. */}
        <p className="text-[12px] text-muted">
          {i18nT('apps.opsMissionControl.settingsPanel.no_credential_note', {
            gitCmd: 'git',
            sshRemote: 'git@github.com:org/repo.git',
            defaultBranch: 'main',
          })}
        </p>
      </div>

      {status ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px] mt-3">
          <dt className="text-muted">{i18nT('apps.opsMissionControl.settingsPanel.remote')}</dt>
          <dd className="font-mono truncate">{displayRemote(status.remote) || '—'}</dd>
          <dt className="text-muted">{i18nT('apps.opsMissionControl.settingsPanel.branch')}</dt>
          <dd className="font-mono">{status.branch || 'main'}</dd>
          {/* The local branch is shown ONLY when it disagrees, and labelled as a separate
              fact rather than folded into the row above. Two rows that usually say the same
              thing invite exactly the conflation that caused the bug; one row that appears
              only on disagreement makes the disagreement the point. */}
          {branchDrifted ? (
            <>
              <dt className="text-warn">{i18nT('apps.opsMissionControl.settingsPanel.this_repo_is_on')}</dt>
              <dd className="font-mono text-warn">
                {status.detached
                  ? i18nT('apps.opsMissionControl.settingsPanel.no_branch_detached_head')
                  : status.local_branch || '—'}
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}

      {/* The backend already distinguishes off / no remote / not yet created / conflicted
          and names the fix for each, so its sentence is rendered verbatim — the same
          reasoning as the Slack card above. */}
      {troubled && status ? (
        <p
          className={`text-[13px] mt-3 flex items-start gap-1.5 ${
            status.schedule_conflict ? 'text-danger' : 'text-warn'
          }`}
        >
          <AlertTriangle className="lucide-inline" />
          <span>{status.detail}</span>
        </p>
      ) : null}
      {status?.schedule_conflict ? (
        <p className="text-[12px] text-muted mt-1">
          {i18nT('apps.opsMissionControl.settingsPanel.pushes_stay_refused_until')} <span className="font-mono">{i18nT('apps.opsMissionControl.settingsPanel.rotation_yaml')}</span> {i18nT('apps.opsMissionControl.settingsPanel.is_resolved_by_hand_in_the_repo_publishing_a_sch')}
        </p>
      ) : null}
      {/* Says what the drift costs, and does NOT overstate it: the exchange keeps working,
          because sync names the branch explicitly on every fetch, merge and push. What
          breaks is the operator's own git — which is the thing they need when a conflicted
          schedule sends them into this directory to fix it by hand. */}
      {branchDrifted && status ? (
        <p className="text-[12px] text-muted mt-1">
          {i18nT('apps.opsMissionControl.settingsPanel.your_team_s_ledger_is_still_being_exchanged_sync')}{' '}
          <span className="font-mono">{status.branch || 'main'}</span> {i18nT('apps.opsMissionControl.settingsPanel.explicitly_every_time_what_does_not_work_is')} <span className="font-mono">{i18nT('apps.opsMissionControl.settingsPanel.git_pull_or_push')}</span> {i18nT('apps.opsMissionControl.settingsPanel.run_by_hand_in_the_ledger_directory_because_this')}{' '}
          {status.detached
            ? i18nT('apps.opsMissionControl.settingsPanel.detached_head_left_alone_finish_the_merge_first')
            : i18nT('apps.opsMissionControl.settingsPanel.next_sync_moves_it_across_unless_a_branch_exists')}
        </p>
      ) : null}
    </Card>
  )
}

/**
 * On-call schedule — the second file in that same repo.
 *
 * Exists because the format was documented ONLY in a Python docstring, the module spec and
 * one SOP, so an operator standing in Settings had no way to learn that the schedule is a
 * file at all, let alone which file. It is deliberately not a path they choose: every
 * teammate must read the same one, so `schedule_file.schedule_path()` fixes it beside
 * `ledger.jsonl`.
 *
 * Renders `github_login` here rather than only in the generic Providers row above because
 * this is where someone goes looking for it, and it answers the roster warnings directly
 * beneath. Both controls post the same key through the same mutation, so they cannot
 * disagree on the server.
 */
function OnCallScheduleCard({
  provider,
  roster,
  syncReady,
}: {
  provider?: ProviderInfo
  roster?: RotationRoster
  syncReady: boolean
}) {
  const queryClient = useQueryClient()
  // From the ROSTER, not from provider config. The login moved onto the keystone floor
  // (`policy_store.OPERATOR_ONLY_KEYS`) because it is an input to the authorization decision —
  // provider config is agent-writable and served unauthenticated, so a login stored there could
  // be forged to defeat the off-shift refusal. `roster.me` is the server's resolved answer and
  // is already fetched here, so nothing new is needed to display it.
  const serverLogin = String(roster?.me ?? '')
  const [login, setLogin] = useState(serverLogin)
  const [touched, setTouched] = useState(false)
  useEffect(() => {
    if (!touched) setLogin(serverLogin)
  }, [serverLogin, touched])

  const loginMutation = useMutation({
    // Through `PUT /settings`, the authenticated route that is the keystone's sole writer.
    mutationFn: (value: string) => opsApi.putSettings({ schedule_github_login: value }),
    onSuccess: () => {
      setTouched(false)
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'providers'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'rotation'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })

  const strictMutation = useMutation({
    mutationFn: (value: boolean) => opsApi.putSettings({ schedule_strict_gating: value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'rotation'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })

  const dirty = touched && login.trim() !== serverLogin
  const commit = () => {
    if (!dirty || loginMutation.isPending) return
    loginMutation.mutate(login.trim())
  }

  return (
    <Card>
      <CardTitle>
        <CalendarClock className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.on_call_schedule')}
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        {i18nT('apps.opsMissionControl.settingsPanel.schedule_location_note', {
          schedule: 'rotation.yaml',
          ledger: 'ledger.jsonl',
        })}
      </p>

      {!syncReady ? (
        <p className="text-[12px] text-warn mb-3 flex items-start gap-1.5">
          <AlertTriangle className="lucide-inline" />
          <span>
            {i18nT('apps.opsMissionControl.settingsPanel.sharing_is_not_set_up_above_so_the_schedule_is_w')}
          </span>
        </p>
      ) : null}

      <p className="text-[12px] text-muted flex items-start gap-1.5">
        <Info className="lucide-inline" />
        <span>{i18nT('apps.opsMissionControl.settingsPanel.expected_shape_label')}</span>
      </p>
      <pre className="font-mono text-[12px] bg-bg-elevated border border-border rounded-md p-2.5 mt-1 overflow-x-auto">
        {`leader: octocat                 # optional; runs nightly ledger hygiene alone
timezone: America/Los_Angeles   # optional; UTC when absent
shifts:
  - from: 2026-08-01
    to: 2026-08-08              # a date-only 'to' means THROUGH that whole day
    who: octocat                # a GitHub login
  - from: 2026-08-08T09:00
    to: 2026-08-15T09:00
    who: [octocat, hubot]       # a list is allowed — co-primary`}
      </pre>

      {/* `schedule-file` is registered unconditionally (registry.build_default_registry), so
          an absent provider here means the catalog has not arrived yet, not that the adapter
          is missing — hence a loading line rather than an explanation of a fault. */}
      {!provider ? <p className="text-[12px] text-muted mt-3">{i18nT('apps.opsMissionControl.settingsPanel.loading')}</p> : null}

      {provider ? (
        <div className="mt-3 flex flex-col gap-2">
          <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-schedule-login">
            <span className="w-44 shrink-0 text-muted">{i18nT('apps.opsMissionControl.settingsPanel.your_github_login')}</span>
            <Input
              id="omc-schedule-login"
              value={login}
              placeholder={i18nT('apps.opsMissionControl.settingsPanel.resolved_from_the_gh_cli_when_blank')}
              onChange={(e) => {
                setTouched(true)
                setLogin(e.target.value)
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit()
              }}
            />
            <SendBtn disabled={!dirty || loginMutation.isPending} onClick={commit}>
              {i18nT('apps.opsMissionControl.settingsPanel.save')}
            </SendBtn>
          </label>
          <p className="text-[12px] text-muted">
            {i18nT('apps.opsMissionControl.settingsPanel.github_login_help', {
              whoKey: 'who:',
              ghCli: 'gh',
            })}
          </p>
          {loginMutation.isError ? (
            <p className="text-[12px] text-danger">
              {(loginMutation.error as Error).message}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* The two setup mistakes the app ALREADY detects and, until now, could only report
          on the Board — which is the wrong place, because the Board answers "who has the
          pager" and this answers "why is my setup wrong". Under strict gating both leave
          this instance permanently idle while looking configured. */}
      {roster ? (
        <div className="mt-3 flex flex-col gap-1.5">
          {!roster.me ? (
            <p className="text-[13px] text-warn flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                {i18nT('apps.opsMissionControl.settingsPanel.no_github_login_resolved_for_this_instance_so_it')}{' '}
                <span className="font-mono">{i18nT('apps.opsMissionControl.settingsPanel.gh')}</span> {i18nT('apps.opsMissionControl.settingsPanel.cli')}
              </span>
            </p>
          ) : null}
          {/* Conditioned on `strict_gating`, because the consequence inverts with it and
              only one half is a fault. Strict on: an unnamed instance is disarmed and idle.
              Strict off: `schedule_file._indeterminate` returns `on_shift=True`, so it arms
              anyway — and so does every other unnamed instance, which is the duplicate-claim
              shape the shared schedule exists to prevent. Both are worth saying; neither is
              the other. */}
          {roster.me && !roster.me_on_roster && roster.strict_gating ? (
            <p className="text-[13px] text-warn flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                <span className="font-mono">{roster.me}</span> {i18nT('apps.opsMissionControl.settingsPanel.is_not_named_in_any_shift_so_under_strict_gating')}{' '}
                <span className="font-mono">{i18nT('apps.opsMissionControl.settingsPanel.who')}</span> {i18nT('apps.opsMissionControl.settingsPanel.list_or_correct_the_login_above')}
              </span>
            </p>
          ) : null}
          {roster.me && !roster.me_on_roster && !roster.strict_gating ? (
            <p className="text-[13px] text-muted flex items-start gap-1.5">
              <Info className="lucide-inline" />
              <span>
                <span className="font-mono">{roster.me}</span> {i18nT('apps.opsMissionControl.settingsPanel.is_not_named_in_any_shift_and_strict_gating_is_o')}{' '}
                <span className="font-mono">{i18nT('apps.opsMissionControl.settingsPanel.who')}</span> {i18nT('apps.opsMissionControl.settingsPanel.list_if_the_rotation_is_meant_to_decide_who_resp')}
              </span>
            </p>
          ) : null}
          {roster.error ? (
            <p className="text-[13px] text-danger flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>{roster.error}</span>
            </p>
          ) : null}

          {/* Strict gating moved onto the keystone floor alongside the login, for the same
              reason: turning it off restores fail-open gating, so an unreadable schedule
              reports "on shift" and the off-shift refusal stops firing. Fencing it removed the
              old provider-config path, so the control lives here — the authenticated PUT. */}
          <div className="flex items-center gap-2 text-[13px] pt-1">
            <Toggle
              checked={roster.strict_gating}
              onChange={(v) => strictMutation.mutate(v)}
              label={i18nT('apps.opsMissionControl.settingsPanel.only_the_on_call_instance_picks_up_work')}
            />
            <span>{i18nT('apps.opsMissionControl.settingsPanel.only_the_on_call_instance_picks_up_work')}</span>
          </div>
          <p className="text-[12px] text-muted flex items-start gap-1.5">
            <Info className="lucide-inline" />
            <span>
              {roster.strict_gating
                ? i18nT('apps.opsMissionControl.settingsPanel.strict_on_explanation')
                : i18nT('apps.opsMissionControl.settingsPanel.strict_off_explanation')}
            </span>
          </p>
        </div>
      ) : null}
    </Card>
  )
}

/**
 * Seconds as something an operator reads at 3am without doing arithmetic.
 *
 * Exported for the unit test: the whole point of this card is that the stored unit
 * (seconds) is not the unit a human reasons about, and 43200 silently misread as minutes
 * is exactly the confusion the card exists to remove.
 */
export function humanizeSecs(secs: number): string {
  if (!Number.isFinite(secs) || secs <= 0) return '—'
  // `fmtUnit` renders the unit in the active language and localizes the digits; a
  // hand-glued `${n} min` translates neither and puts the separator in the wrong
  // place outside English. `maximumFractionDigits: 1` keeps the previous
  // one-decimal precision for a non-integer value.
  if (secs < 60) return fmtUnit(secs, 'second')
  if (secs < 3600) return fmtUnit(secs / 60, 'minute', { maximumFractionDigits: 1 })
  return fmtUnit(secs / 3600, 'hour', { maximumFractionDigits: 1 })
}

/**
 * Heartbeat pacing — the claim ceiling and the two release windows.
 *
 * `PUT /settings` has accepted all three for as long as they existed and no read path
 * returned any of them, so an operator who changed how long a dead investigation pins a
 * signal got no confirmation and no way to look the value up again. Worse for the
 * untouched case: the defaults governing every install were invisible, so "how long
 * before this gets picked up again" had no answer short of reading the source.
 *
 * Last in the panel deliberately. These are tuning knobs with correct defaults — an
 * operator setting the app up needs providers and autonomy, not this — and putting them
 * above the setup cards would imply they need attention.
 */
function HeartbeatCard({
  sweep,
  onSave,
  saving,
}: {
  sweep?: SweepWindows
  onSave: (updates: Record<string, unknown>) => void
  saving: boolean
}) {
  // Drafts in MINUTES, because the backend's seconds are a storage unit and nobody tunes a
  // 12-hour window by typing 43200. Re-seeded from the server while untouched, the same
  // shape as every other field here, so a background /state refresh cannot clobber typing.
  const serverStale = sweep?.stale_after_secs ?? 0
  const serverNeedsHuman = sweep?.needs_human_stale_after_secs ?? 0
  const [stale, setStale] = useState('')
  const [needsHuman, setNeedsHuman] = useState('')
  const [staleTouched, setStaleTouched] = useState(false)
  const [needsHumanTouched, setNeedsHumanTouched] = useState(false)
  useEffect(() => {
    if (!staleTouched) setStale(serverStale ? String(Math.round(serverStale / 60)) : '')
  }, [serverStale, staleTouched])
  useEffect(() => {
    if (!needsHumanTouched) {
      setNeedsHuman(serverNeedsHuman ? String(Math.round(serverNeedsHuman / 60)) : '')
    }
  }, [serverNeedsHuman, needsHumanTouched])

  // The backend refuses anything non-integer or <= 0, so the button is disabled rather
  // than letting the operator earn a 400 they have to interpret.
  const parseMins = (raw: string): number | null => {
    const mins = Number(raw.trim())
    if (!raw.trim() || !Number.isInteger(mins) || mins <= 0) return null
    return mins * 60
  }
  const staleSecs = parseMins(stale)
  const needsHumanSecs = parseMins(needsHuman)
  const staleDirty = staleTouched && staleSecs !== null && staleSecs !== serverStale
  const needsHumanDirty =
    needsHumanTouched && needsHumanSecs !== null && needsHumanSecs !== serverNeedsHuman

  const save = (key: string, secs: number | null, clear: () => void) => {
    if (secs === null || saving) return
    onSave({ [key]: secs })
    clear()
  }

  if (!sweep) {
    // Never substitute the defaults here. This gateway did not report them, and printing
    // 2 h against an instance that might be running 30 m would be a confident lie.
    return (
      <Card>
        <CardTitle>
          <Clock className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.heartbeat_pacing')}
        </CardTitle>
        <p className="text-[13px] text-muted">
          {i18nT('apps.opsMissionControl.settingsPanel.not_reported_by_this_gateway_so_the_values_in_fo')}
        </p>
      </Card>
    )
  }

  return (
    <Card>
      <CardTitle>
        <Clock className="lucide-inline" /> {i18nT('apps.opsMissionControl.settingsPanel.heartbeat_pacing')}
      </CardTitle>
      <p className="text-[13px] text-muted mb-3">
        {i18nT('apps.opsMissionControl.settingsPanel.how_much_the_heartbeat_picks_up_at_once_and_how')}
      </p>

      <div className="flex flex-col gap-2">
        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-stale-after">
          <span className="w-56 shrink-0 text-muted">{i18nT('apps.opsMissionControl.settingsPanel.release_an_investigation_after')}</span>
          <Input
            id="omc-stale-after"
            value={stale}
            inputMode="numeric"
            placeholder="120"
            onChange={(e) => {
              setStaleTouched(true)
              setStale(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                save('stale_after_secs', staleSecs, () => setStaleTouched(false))
              }
            }}
          />
          <span className="text-muted shrink-0">{i18nT('apps.opsMissionControl.settingsPanel.min')}</span>
          <SendBtn
            disabled={!staleDirty || saving}
            onClick={() => save('stale_after_secs', staleSecs, () => setStaleTouched(false))}
          >
            {i18nT('apps.opsMissionControl.settingsPanel.save')}
          </SendBtn>
        </label>

        <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-needs-human-after">
          <span className="w-56 shrink-0 text-muted">{i18nT('apps.opsMissionControl.settingsPanel.release_a_question_after')}</span>
          <Input
            id="omc-needs-human-after"
            value={needsHuman}
            inputMode="numeric"
            placeholder="720"
            onChange={(e) => {
              setNeedsHumanTouched(true)
              setNeedsHuman(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                save('needs_human_stale_after_secs', needsHumanSecs, () =>
                  setNeedsHumanTouched(false),
                )
              }
            }}
          />
          <span className="text-muted shrink-0">{i18nT('apps.opsMissionControl.settingsPanel.min')}</span>
          <SendBtn
            disabled={!needsHumanDirty || saving}
            onClick={() =>
              save('needs_human_stale_after_secs', needsHumanSecs, () =>
                setNeedsHumanTouched(false),
              )
            }
          >
            {i18nT('apps.opsMissionControl.settingsPanel.save')}
          </SendBtn>
        </label>
      </div>

      <p className="text-[12px] text-muted mt-2">
        {i18nT('apps.opsMissionControl.settingsPanel.an_incident_waiting_on_you_gets_the_longer_windo')}{' '}
        {sweep.needs_human_derived ? (
          <>
            {i18nT('apps.opsMissionControl.settingsPanel.needs_human_derived', { value: humanizeSecs(serverNeedsHuman) })}
          </>
        ) : (
          <>
            {i18nT('apps.opsMissionControl.settingsPanel.needs_human_pinned', { value: humanizeSecs(serverNeedsHuman) })}
          </>
        )}
      </p>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px] mt-3">
        <dt className="text-muted">{i18nT('apps.opsMissionControl.settingsPanel.in_force_now')}</dt>
        <dd className="font-mono">
          {humanizeSecs(serverStale)} / {humanizeSecs(serverNeedsHuman)}
        </dd>
        <dt className="text-muted">{i18nT('apps.opsMissionControl.settingsPanel.new_claims_per_heartbeat')}</dt>
        <dd className="font-mono">{sweep.max_claims_per_cycle}</dd>
      </dl>
    </Card>
  )
}

/**
 * Author the per-signal grants that `act` mode needs.
 *
 * This whole component is the fix for a dead end: mode could be set to `act`, the copy said
 * grants came from "patterns you have explicitly allowlisted with a rule", and there was
 * NOWHERE to write one — `policy_store.set_rules` had no caller and `/rotation` returned only
 * a count. So `act` was unreachable, and an operator following the manual (which said to edit
 * `data/config.json`) got silent Propose behavior instead, because the keystone store ignores
 * that file once the policy file exists.
 *
 * A rule needs a resource glob or a label match; the backend refuses a blanket act-grant with
 * a 400 and this form requires the glob for the same reason, so the refusal is visible before
 * a round trip rather than as a server error.
 */
function ActRulesCard({
  rules,
  sources,
  onSave,
  saving,
  error,
}: {
  rules: AutonomyRule[]
  sources: ProviderInfo[]
  onSave: (rules: AutonomyRule[]) => void
  saving: boolean
  error: string
}) {
  const [source, setSource] = useState('')
  const [glob, setGlob] = useState('')

  // Only providers that actually emit signals can be the subject of a grant, and only
  // configured ones: a rule naming an unconfigured provider can never match, so offering it
  // would be an invitation to author something inert.
  const eligible = sources.filter((p) => p.configured && p.roles.includes('signal'))
  const canAdd = source !== '' && glob.trim() !== '' && !saving

  return (
    <Card>
      <CardTitle>{i18nT('apps.opsMissionControl.settingsPanel.act_rules')}</CardTitle>
      <p className="text-[13px] text-muted mb-3">
        {i18nT('apps.opsMissionControl.settingsPanel.act_needs_both_the_mode_above_and_a_rule_that_ma')}
      </p>

      {rules.length === 0 ? (
        <p className="text-[13px] text-muted">
          {i18nT('apps.opsMissionControl.settingsPanel.no_rules_yet_add_one_below_to_grant_authority_fo')}
        </p>
      ) : (
        <ul className="flex flex-col gap-1.5 mb-3">
          {rules.map((rule, index) => (
            <li
              key={`${rule.source}-${rule.resource_glob ?? ''}-${index}`}
              className="flex items-center gap-2 text-[13px]"
            >
              <Badge variant="warn">{rule.mode}</Badge>
              <span className="font-mono">{rule.source}</span>
              <span className="font-mono text-muted">{rule.resource_glob ?? ''}</span>
              <span className="text-[12px] text-muted">
                {rule.actions && rule.actions.length > 0
                  ? rule.actions.join(', ')
                  : i18nT('apps.opsMissionControl.settingsPanel.all_actions')}
              </span>
              <Btn
                disabled={saving}
                onClick={() => onSave(rules.filter((_, i) => i !== index))}
                aria-label={i18nT('apps.opsMissionControl.settingsPanel.revoke_rule')}
              >
                <Trash2 className="lucide-inline" />
              </Btn>
            </li>
          ))}
        </ul>
      )}

      {eligible.length === 0 ? (
        <p className="text-[12px] text-muted mt-2">
          {i18nT('apps.opsMissionControl.settingsPanel.connect_a_signal_source_first_a_rule_naming_an_u')}
        </p>
      ) : (
        <div className="flex flex-col gap-2 mt-3">
          {/* A div, not a label: SimpleSelect renders a button, so `htmlFor`/`id` no
              longer associate and the visible heading's own key becomes the aria-label
              instead. "Choose a source" stays SELECTABLE (re-picking it clears `source`
              and re-disables Grant), which is what `clearLabel` is for. */}
          <div className="flex items-center gap-2 text-[13px]">
            <span className="w-44 shrink-0 text-muted">
              {i18nT('apps.opsMissionControl.settingsPanel.signal_source')}
            </span>
            <SimpleSelect
              options={eligible.map((p) => p.id)}
              optionLabels={eligible.map((p) => p.display_name)}
              value={source}
              onChange={setSource}
              clearLabel={i18nT('apps.opsMissionControl.settingsPanel.choose_a_source')}
              aria-label={i18nT('apps.opsMissionControl.settingsPanel.signal_source')}
            />
          </div>
          {/* Input BOTH nested and bound by htmlFor/id, matching the Slack channel row —
              jsx-a11y/label-has-for wants both forms and still warns because `Input` is a
              wrapper it cannot see through. Same warning as the eight existing rows here. */}
          <label className="flex items-center gap-2 text-[13px]" htmlFor="omc-rule-glob">
            <span className="w-44 shrink-0 text-muted">
              {i18nT('apps.opsMissionControl.settingsPanel.resource_pattern')}
            </span>
            <Input
              id="omc-rule-glob"
              value={glob}
              placeholder={i18nT('apps.opsMissionControl.settingsPanel.prod_star')}
              onChange={(e) => setGlob(e.target.value)}
            />
            <SendBtn
              disabled={!canAdd}
              onClick={() => {
                onSave([
                  ...rules,
                  { source, mode: 'act', resource_glob: glob.trim() },
                ])
                setSource('')
                setGlob('')
              }}
            >
              {i18nT('apps.opsMissionControl.settingsPanel.grant')}
            </SendBtn>
          </label>
          <p className="text-[12px] text-muted">
            {i18nT('apps.opsMissionControl.settingsPanel.a_pattern_is_required_there_is_no_wildcard_grant')}
          </p>
        </div>
      )}

      {error ? <p className="text-[12px] text-danger mt-2">{error}</p> : null}
    </Card>
  )
}

export default function SettingsPanel() {
  const queryClient = useQueryClient()

  const providersQuery = useQuery({
    queryKey: ['ops-mission-control', 'providers'],
    queryFn: () => opsApi.providers(),
  })

  const rotationQuery = useQuery({
    queryKey: ['ops-mission-control', 'rotation'],
    queryFn: () => opsApi.rotation(),
  })

  // Slack status rides on /state (it depends on live gateway state, not config
  // alone), so this reuses the board's query rather than adding an endpoint.
  const stateQuery = useQuery({
    queryKey: ['ops-mission-control', 'state'],
    queryFn: () => opsApi.state(),
  })

  const settingsMutation = useMutation({
    mutationFn: (updates: Record<string, unknown>) => opsApi.putSettings(updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'rotation'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })

  const providers = providersQuery.data?.providers ?? []
  const companions = stateQuery.data?.companions ?? EMPTY_COMPANIONS
  const mode: OperatingMode = rotationQuery.data?.mode ?? 'observe'

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardTitle>{i18nT('apps.opsMissionControl.settingsPanel.autonomy')}</CardTitle>
        <p className="text-[13px] text-muted mb-3">
          {i18nT('apps.opsMissionControl.settingsPanel.the_maximum_this_instance_may_do_a_per_signal_ru')}
        </p>
        {/* collapse={false}: inside a Card, `> * { z-index: 1 }` traps the
            collapsed dropdown overlay beneath the rows below it. */}
        {/* Literal keys inline rather than a `.map()` over a key table: `check-i18n-keys`
            cannot follow a key through a closure parameter, and a site it cannot resolve is
            one it cannot verify — which exempts it from every downstream check. */}
        <SegmentedControl
          segments={[
            {
              key: 'observe',
              label: i18nT('apps.opsMissionControl.settingsPanel.mode_observe_label'),
              icon: <Eye className="lucide-inline" />,
            },
            {
              key: 'propose',
              label: i18nT('apps.opsMissionControl.settingsPanel.mode_propose_label'),
              icon: <MessageSquare className="lucide-inline" />,
            },
            {
              key: 'act',
              label: i18nT('apps.opsMissionControl.settingsPanel.mode_act_label'),
              icon: <Zap className="lucide-inline" />,
            },
          ]}
          value={mode}
          onChange={(value) => settingsMutation.mutate({ mode: value })}
          layoutId="omc-mode"
          collapse={false}
        />
        <p className="text-[13px] text-muted mt-3">{i18nT(MODE_HELP_KEY[mode])}</p>
        {mode === 'act' ? (
          <p className="text-[13px] text-warn mt-2 flex items-start gap-1.5">
            <ShieldAlert className="lucide-inline" />
            <span>
              {i18nT('apps.opsMissionControl.settingsPanel.act_only_takes_effect_for_signals_matched_by_a_r')}
            </span>
          </p>
        ) : null}
        {/* An un-actionable empty-state line lived here and was a dead end: it named the gap
            without offering any way to close it. The rules card below IS the way, so the
            statement moved there, next to the form that answers it. */}
        {settingsMutation.isError ? (
          <p className="text-[12px] text-danger mt-2">
            {(settingsMutation.error as Error).message}
          </p>
        ) : null}
      </Card>

      {/* Rendered whenever the gateway reports the rules (not gated on `mode === 'act'`):
          an operator must be able to prepare and review grants BEFORE flipping the mode,
          and must be able to see what is still granted after flipping it back. */}
      {rotationQuery.data?.rules_detail !== undefined ? (
        <ActRulesCard
          rules={rotationQuery.data.rules_detail}
          sources={providers}
          onSave={(next) => settingsMutation.mutate({ autonomy_rules: next })}
          saving={settingsMutation.isPending}
          error={
            settingsMutation.isError ? (settingsMutation.error as Error).message : ''
          }
        />
      ) : null}

      <Card>
        <CardTitle>{i18nT('apps.opsMissionControl.settingsPanel.providers')}</CardTitle>
        <p className="text-[13px] text-muted">
          {i18nT('apps.opsMissionControl.settingsPanel.turn_on_the_systems_you_want_watched_aws_uses_yo')}
        </p>
        {providersQuery.isLoading ? (
          <p className="text-sm text-muted mt-2">{i18nT('apps.opsMissionControl.settingsPanel.loading')}</p>
        ) : (
          providers.map((p) => (
            <ProviderRow
              key={p.id}
              provider={p}
              fencedIdentity={
                p.id === 'pagerduty'
                  ? {
                    label: i18nT('apps.opsMissionControl.settingsPanel.your_pagerduty_user_id'),
                    settingsKey: 'pagerduty_user_id',
                    value: rotationQuery.data?.identities?.pagerduty_user_id ?? '',
                    help: i18nT('apps.opsMissionControl.settingsPanel.pagerduty_user_id_help'),
                  }
                  : undefined
              }
            />
          ))
        )}

        {/* Shown only when a companion IS installed. A public install has none and
            should not be told about an extension point it is not using. When one is
            installed but its adapters are absent above, that gap is the signal that
            it was rejected at admission — which is why this is reported at all. */}
        {companions.length > 0 ? (
          <div className="border-t border-border pt-3 mt-3">
            <p className="text-[12px] text-muted">
              {/* Pluralized by the CATALOG, not by an English fragment passed in as a
                  param: `noun: 'adapter package'` rendered English mid-sentence in all nine
                  non-English locales, and no catalog value can repair an interpolated
                  fragment. Both whole sentences are now translatable. */}
              {i18nT('apps.opsMissionControl.settingsPanel.companions_installed', {
                count: companions.length,
                names: companions.map((c) => c.name).join(', '),
              })}
            </p>
          </div>
        ) : null}
      </Card>

      <SlackOutCard
        status={stateQuery.data?.slack}
        onSave={(updates) => settingsMutation.mutate(updates)}
        saving={settingsMutation.isPending}
      />

      {/* Immediately after Slack, because they are the two output channels and the
          difference between them is the point: this one needs no workspace and no token,
          so it works on an install where the Slack card cannot. */}
      <NotifyOutCard
        status={stateQuery.data?.notify}
        onSave={(updates) => settingsMutation.mutate(updates)}
      />

      {/* Sync status rides on /state for the same reason Slack's does — it reflects live
          repo state (is there a .git yet, does a tracked file hold conflict markers), not
          config alone — so this reuses the board's query rather than adding an endpoint. */}
      <SharedMemoryCard
        status={stateQuery.data?.ledger_sync}
        onSave={(updates) => settingsMutation.mutate(updates)}
        saving={settingsMutation.isPending}
      />

      {/* Immediately after, because the schedule lives INSIDE that repo and only reads
          correctly as a consequence of it. */}
      <OnCallScheduleCard
        provider={providers.find((p) => p.id === 'schedule-file')}
        roster={rotationQuery.data?.roster}
        syncReady={Boolean(stateQuery.data?.ledger_sync?.ready)}
      />

      <Card>
        <CardTitle>{i18nT('apps.opsMissionControl.settingsPanel.instance')}</CardTitle>
        {/* Not a <label>: Toggle renders a role="switch" div, not a form
            control, so a label has nothing to associate with. The switch
            carries its own accessible name via `label`. */}
        <div className="flex items-center gap-2 text-[13px]">
          <Toggle
            checked={Boolean(rotationQuery.data?.primary)}
            onChange={(v) => settingsMutation.mutate({ primary_instance: v })}
            label={i18nT('apps.opsMissionControl.settingsPanel.run_nightly_ledger_maintenance_on_this_instance')}
          />
          <span>{i18nT('apps.opsMissionControl.settingsPanel.run_nightly_ledger_maintenance_on_this_instance')}</span>
        </div>
        <p className="text-[12px] text-muted mt-2">
          {i18nT('apps.opsMissionControl.settingsPanel.leave_this_on_if_you_are_the_only_one_running_op')}
        </p>
      </Card>

      {/* Last: correct out of the box, so it must not compete with the cards an operator
          actually has to touch to get the app watching anything. */}
      <HeartbeatCard
        sweep={rotationQuery.data?.sweep}
        onSave={(updates) => settingsMutation.mutate(updates)}
        saving={settingsMutation.isPending}
      />
    </div>
  )
}
