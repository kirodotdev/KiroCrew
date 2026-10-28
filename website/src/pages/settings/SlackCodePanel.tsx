/**
 * Settings → Slack → Slack Code: the registered Slack Code switches (Agent
 * sessions; see `slackCodeSwitches`) and the "Update your Slack app" panel.
 *
 * The draft lives in `SlackPanel` (one Save for the whole page); this module
 * owns the shape of the Slack Code slice of it, how it is read from the GET and
 * written to the PUT, and how it renders.
 */
import { CheckCircle2, Info } from 'lucide-react'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { type SlackCapability, type SlackCodeSwitchKey, type SlackConfigData, type SlackConfigSave } from '../../api/client'
import { i18nT } from '../../i18n/t'
import { SlackManifestUpdate, useInstallRecheckFailed, useSlackManifestStatus } from './SlackManifestUpdate'
import { SLACK_CODE_SWITCHES } from './slackCodeSwitches'

/** Install readings that leave no update steps to do. */
const NO_UPDATE_STEPS = new Set(['up_to_date', 'feature_disabled'])

/** The Slack Code slice of the Slack settings draft. */
export interface SlackCodeDraft {
  /** Every registered switch, as the draft has it. */
  switches: Record<SlackCodeSwitchKey, boolean>
}

export function slackCodeDraftFrom(c: SlackConfigData): SlackCodeDraft {
  return {
    switches: Object.fromEntries(SLACK_CODE_SWITCHES.map(k => [k, !!c[k]])) as Record<SlackCodeSwitchKey, boolean>,
  }
}

/** The PUT fields for the Slack Code slice: only the switches that differ from
 *  *saved*, so a save of an unrelated Slack field writes none of them. */
export function slackCodePayload(d: SlackCodeDraft, saved: SlackCodeDraft): Partial<SlackConfigSave> {
  const out: Partial<SlackConfigSave> = {}
  for (const k of SLACK_CODE_SWITCHES) {
    if (d.switches[k] !== saved.switches[k]) out[k] = d.switches[k]
  }
  return out
}

/** Whether a SAVED toggle can run: it also needs the installed app to hold the
 *  feature's scope, and Slack not to have refused it. Only what the "Update
 *  your Slack app" panel below does not already say: that the app is prepared.
 *  A missing scope, a workspace without the feature, a rejected or refused
 *  token and an unread install are the panel's status, with its detail and
 *  steps, so they are not repeated here. */
function CapabilityLine({ capability }: { capability?: SlackCapability }) {
  if (!capability) return null
  if (capability.available) {
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

          {/* ── Switches, one row each, in registry order. ── */}
          <div className="flex flex-col gap-4" data-testid="slack-code-switches">
            {/* ── Agent sessions ── */}
            <div>
              <SettingsToggle
                label={i18nT('pages.settings.slackPanel.slackCode.agent_sessions')}
                description={i18nT('pages.settings.slackPanel.slackCode.agent_sessions_desc')}
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
              {data.agent_sessions && <CapabilityLine capability={data.agent_sessions_capability} />}
            </div>
          </div>

          <SlackManifestUpdate data={data} draft={draft} manifestText={manifestText} onSave={readOnly ? undefined : onSave} saving={saving} />
        </div>
      </SettingsCard>
    </SettingsSection>
  )
}
