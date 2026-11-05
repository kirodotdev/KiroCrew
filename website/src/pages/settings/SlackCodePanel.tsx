/**
 * Settings → Slack → Slack Code: the registered Slack Code switches (Agent
 * sessions and Code channels; see `slackCodeSwitches`), the code-channel defaults (folder /
 * agent / model / effort), and the "Update your Slack app" panel.
 *
 * The draft lives in `SlackPanel` (one Save for the whole page); this module
 * owns the shape of the Slack Code slice of it, how it is read from the GET and
 * written to the PUT, and how it renders.
 */
import { Suspense, lazy } from 'react'
import { CheckCircle2, Info } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { Badge } from '../../components/ui'
import type { SlackCapability, SlackConfigData } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { SlackManifestUpdate, useInstallRecheckFailed, useSlackManifestStatus } from './SlackManifestUpdate'
import { SLACK_CODE_SWITCHES } from './slackCodeSwitches'
import type { SlackCodeDraft } from './slackCodeDraft'

export { slackCodeDraftFrom, slackCodePayload, type SlackCodeDraft } from './slackCodeDraft'

const SlackCodeChannelDefaults = lazy(() => import('./SlackCodeChannelDefaults'))

/** Install readings that leave no update steps to do. */
const NO_UPDATE_STEPS = new Set(['up_to_date', 'feature_disabled'])

/** Whether a SAVED toggle can run: it also needs the installed app to hold the
 *  feature's scope, and Slack not to have refused it. Only what the "Update
 *  your Slack app" panel below does not already say: that the app is prepared.
 *  A missing scope, a workspace without the feature, a rejected or refused
 *  token and an unread install are the panel's status, with its detail and
 *  steps, so they are not repeated here. */
function CapabilityLine({ capability, updatePending }: { capability?: SlackCapability; updatePending: boolean }) {
  if (!capability) return null
  // The app can hold the scope while its manifest still lacks the feature: the
  // update panel then says it needs an update, so "prepared" would contradict it.
  if (capability.available && !updatePending) {
    return (
      <div className="flex items-center gap-1.5 text-[12px] text-ok mt-1" data-testid="slack-capability" data-available="true">
        <CheckCircle2 size={12} className="flex-none" /> {i18nT('pages.settings.slackPanel.slackCode.cap_active')}
      </div>
    )
  }
  // A token-type refusal is the update panel's to state, with which token to
  // paste; repeating it here would put two verdicts on the page.
  return null
}

export function SlackCodeSection({ data, draft, onChange, readOnly, manifestText, onSave, saving }: {
  data: SlackConfigData
  draft: SlackCodeDraft
  onChange: (patch: Partial<SlackCodeDraft>) => void
  readOnly: boolean
  /** The manifest the SAVED switches render (GET /api/slack/manifest). */
  manifestText: string | undefined
  /** The page's own save (one save for the whole Slack page), offered in the
   *  update panel while a switch change is unsaved. */
  onSave: () => void
  saving: boolean
}) {
  // Turning Agent sessions on only changes the manifest this page generates, so
  // the switch flips like any other. The permanent step is saving that manifest
  // in Slack, and the warning sits there, at the update steps.
  const setAgentSessions = (on: boolean) => {
    onChange({ switches: { ...draft.switches, agent_sessions: on } })
  }
  // The note's "nothing yet ... in the steps below" holds only while there are
  // steps to do: not once the feature is live, or once a reading asks for no
  // update (no steps render then). A granted scope alone proves nothing (the
  // older assistant view holds it too). Only a reading the update panel below
  // would also trust: it did not fail and was taken for the saved switches.
  // When unsure, the note stays.
  const statusQ = useSlackManifestStatus()
  const [recheckFailed] = useInstallRecheckFailed(statusQ.dataUpdatedAt)
  const savedKey = SLACK_CODE_SWITCHES.filter(f => !!data[f]).join(',')
  const reading = statusQ.data
  const readingCurrent = !!reading && !statusQ.isError && !recheckFailed
    && SLACK_CODE_SWITCHES.filter(f => reading.features?.includes(f)).join(',') === savedKey
  const installRead = readingCurrent ? reading.install ?? undefined : undefined
  // The update panel below lists steps for this reading.
  const updatePending = !!installRead && !NO_UPDATE_STEPS.has(installRead.status)
  const agentSessionsInSlack = data.agent_sessions && (
    !!data.agent_sessions_capability?.available
    || (!!installRead && NO_UPDATE_STEPS.has(installRead.status)))

  return (
    <SettingsSection title={i18nT('pages.settings.slackPanel.slackCode.section')}>
      <SettingsCard index={2}>
        {/* Framed, unlike the page's other borderless cards: it is the one
            section whose switches change the Slack app itself. */}
        <div className="rounded-lg border border-border p-4" data-testid="slack-code-section">
          <p className="text-[12px] text-muted m-0 mb-2">{i18nT('pages.settings.slackPanel.slackCode.intro')}</p>

          {/* ── Switches, one row each, in registry order. The Agent sessions
              row (#21422) is the first, above Code channels. ── */}
          <div className="flex flex-col gap-4" data-testid="slack-code-switches">
            {/* ── Agent sessions ── */}
            <div>
              <SettingsToggle
                label={i18nT('pages.settings.slackPanel.slackCode.agent_sessions')}
                hint={i18nT('pages.settings.slackPanel.slackCode.agent_sessions_desc')}
                checked={draft.switches.agent_sessions}
                onChange={setAgentSessions}
                disabled={readOnly}
                configKey="slack.agent_sessions"
              />
              {/* Muted: the switch is safe. The warn-toned warning is at the
                  manifest update steps, where the permanent step happens. Gone
                  once Slack has the setup: "nothing yet" is then untrue. */}
              {!agentSessionsInSlack && (
                <div className="flex items-start gap-2 text-[12px] text-muted mt-1" data-testid="slack-agent-sessions-note">
                  <Info size={12} className="flex-none mt-0.5" />
                  <span>{i18nT('pages.settings.slackPanel.slackCode.agent_sessions_note')}</span>
                </div>
              )}
              {data.agent_sessions && <CapabilityLine capability={data.agent_sessions_capability} updatePending={updatePending} />}
            </div>
            {/* ── Code channels ── */}
            <div>
              <SettingsToggle
                label={i18nT('pages.settings.slackPanel.slackCode.code_channels')}
                description={i18nT('pages.settings.slackPanel.slackCode.code_channels_desc')}
                checked={draft.switches.code_channels}
                onChange={v => onChange({ switches: { ...draft.switches, code_channels: v } })}
                disabled={readOnly}
                configKey="slack.code_channels"
              />
              {/* A preview tag on a normal Slack setting. The description says
                  what on and off do, so the tag stands alone here. */}
              <div className="flex items-start gap-2 mt-1" data-testid="slack-code-preview-note">
                <Badge variant="aim" className="text-[11px] font-sans px-1.5 py-0 flex-none">{i18nT('pages.settings.slackPanel.slackCode.stage_tag')}</Badge>
              </div>
              {data.code_channels && <CapabilityLine capability={data.code_channels_capability} updatePending={updatePending} />}
            </div>
          </div>

          {draft.switches.code_channels && (
            // Its own chunk: loaded only once the switch is on, so the Settings
            // page does not carry the defaults card for every visitor.
            <Suspense fallback={null}>
              <SlackCodeChannelDefaults data={data} draft={draft} onChange={onChange} readOnly={readOnly} />
            </Suspense>
          )}

          <SlackManifestUpdate data={data} draft={draft} manifestText={manifestText} onSave={readOnly ? undefined : onSave} saving={saving} />
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
