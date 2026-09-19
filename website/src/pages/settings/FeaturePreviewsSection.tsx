import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight } from 'lucide-react'

import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { FeaturePreviewIntroButton, type FeaturePreviewIntro } from '../../components/FeaturePreviewIntroDialog'
import { usePreviewFlag } from '../../hooks/usePreviewFlag'
import { PREVIEW_CREW, PREVIEW_INSTANCE_SESSIONS, PREVIEW_REMOTE_CREW_CHAT, PREVIEW_WEBHOOKS, setPreviewFlag } from '../../utils/previewFlags'
import { DECISIONS_LIVE_POINT, readDecisions } from './decisionsPreview'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/**
 * Settings > Developer > Feature Previews — opt in to surfaces that ship in the
 * bundle but are not released yet (see `utils/previewFlags.ts`).
 *
 * Formerly its own tab on the standalone Developer page (`/developer`). It moved
 * here because the switch that HOLDS an unreleased feature belongs next to the
 * switch that REVEALS the developer tooling (Developer Mode, one section up):
 * both are consent gates, and a reader looking for "how do I turn the unfinished
 * thing on" looks in Settings, not on an internals page they first have to
 * unlock. `DeveloperPage.tsx` redirects the old
 * `/developer?tab=feature-previews` link here.
 *
 * The USER-FACING copy says "features" and "pages", never "surfaces": `Surface`
 * is the registry's internal term and means nothing to the operator reading the
 * toggle. The component and catalog keys keep the code vocabulary on purpose —
 * they name the mechanism, not the copy. The catalog keys also keep their
 * historical `pages.developer.featurePreviewsTab.*` namespace: renaming seven
 * keys across fourteen catalogs buys no user-visible change, and the section
 * itself still describes the developer-tooling gate it started as.
 *
 * ONE CARD PER FEATURE, and everything that belongs to a feature lives inside
 * its card: the headline, the sentence explaining what state it is in, its
 * toggle, and any ingress that only appears once it is on. A reader scanning the
 * section can then take a card as the whole story of one preview, rather than
 * pairing a row against an ingress rendered somewhere below it.
 *
 * One explicit card per preview flag rather than a loop over a table: the copy
 * has to be a static `i18nT('literal')` call for `check-i18n-keys.mjs` to
 * resolve it, and a table of key strings indexed per card is exactly the dynamic
 * pattern that gate cannot follow. A preview flag is also meant to be
 * short-lived, so the cost of a card is paid once and then deleted with it.
 *
 * Under `pages/settings/` ON PURPOSE, reversing the old tab's stance:
 * `gen-settings-registry.mjs` scans this directory, so these toggles ARE
 * indexed into Settings search (`PANEL_TAB_MAP` maps this file to `developer`).
 * The old tab kept itself out of the index so that searching "webhooks" would
 * not advertise a hidden page. In Settings the calculus flips: a control the
 * user can see on a Settings pane but cannot find through Settings search is
 * the exact coverage gap the settings-coverage gate exists to close, and what
 * the search hit reaches is the opt-in switch — labelled as a preview — not the
 * page it holds. The PAGE stays un-advertised: `getAdvertisedSurfaces()` and
 * the Search Everywhere Pages provider still filter it until the flag is on.
 *
 * No `configKey` on the four `previewFlags.ts` toggles, deliberately: that prop
 * is what makes a `<SettingRef>` chip deep-link here and what feeds
 * `settingsRegistry.gen.ts`, and a per-device localStorage flag has no config
 * path to name at all. Search deep-links still reach every toggle through its
 * registry id + `data-setting-label`, which need no configKey.
 *
 * The Decisions toggle below carries no `configKey` either, for a different
 * reason: it writes no config path. Its value is the KEYSTONE
 * `decisions_consent.json`, reached through `/api/decisions/consent`, because
 * `config.json` is writable by an auto-approved agent shell and consent to send
 * message text off the machine must not be (see `decisionsPreview.ts`). A
 * `configKey` naming a config path nothing reads would be the drift the
 * `test_settingref_schema_fixture.py` guard exists to catch, so its absence here
 * is asserted by `decisionsPreview.test.ts`.
 *
 * Each card may also carry a "See what it looks like" button (`FeaturePreviewIntroButton`)
 * opening a dialog with a REAL capture of the surface the flag reveals, a
 * sentence on what it does, where it appears once on, and the same switch again.
 * The intro is defined right here next to its card — the card IS the preview's
 * definition — as a render-time builder rather than a table of catalog keys,
 * for the same `check-i18n-keys.mjs` reason the cards are not a loop: the gate
 * resolves a literal `i18nT('…')`, not a key read out of a nested object. A
 * preview whose surface has not been captured yet (below: "Chat on a crew",
 * whose menu entry only exists with a live tunnel to a second machine, which an
 * isolated capture instance cannot honestly stage) simply has no builder and so
 * no button — never a dialog with an empty frame.
 */

/** Public paths of the captures; the files ride `public/`, never a JS chunk. */
const MEDIA_BASE = '/app-assets/feature-previews'

/** Webhooks: the `/webhooks` page itself, the flag's only door. */
function webhooksIntro(): FeaturePreviewIntro {
  return {
    summary: i18nT('pages.developer.featurePreviewsTab.intro.webhooks_summary'),
    whereToFind: i18nT('pages.developer.featurePreviewsTab.intro.webhooks_where'),
    media: [
      {
        kind: 'image',
        light: `${MEDIA_BASE}/webhooks-page-light.png`,
        dark: `${MEDIA_BASE}/webhooks-page-dark.png`,
        caption: i18nT('pages.developer.featurePreviewsTab.intro.webhooks_media_page'),
      },
    ],
  }
}

/** Crew Members: the `/members` page, the flag's only door. */
function crewIntro(): FeaturePreviewIntro {
  return {
    summary: i18nT('pages.developer.featurePreviewsTab.intro.crew_summary'),
    whereToFind: i18nT('pages.developer.featurePreviewsTab.intro.crew_where'),
    media: [
      {
        kind: 'image',
        light: `${MEDIA_BASE}/crew-members-light.png`,
        dark: `${MEDIA_BASE}/crew-members-dark.png`,
        caption: i18nT('pages.developer.featurePreviewsTab.intro.crew_media_members'),
      },
    ],
  }
}

/** ids of the notes this card points its switch at via `aria-describedby`. */
const DECISIONS_BACKEND_NOTE_ID = 'decisions-preview-backend-note'
const DECISIONS_EGRESS_NOTE_ID = 'decisions-preview-egress-note'

/**
 * Decisions (Jev): the one card here whose switch is BACKEND state.
 *
 * The other four previews are `previewFlags.ts` keys — per-device localStorage,
 * because what they hold is a page this browser either draws or does not. This
 * one holds a gate that runs in the GATEWAY, which cannot read this browser's
 * localStorage — and it is consent to send message text to a paid external
 * service, so it is not a `config.json` value either: that file is writable by
 * an auto-approved agent shell. The switch reads and writes the keystone
 * `decisions_consent.json` through the owner-only `/api/decisions/consent` pair,
 * the same shape as the AWS consent surface. The sampling share still comes from
 * `config.json` (`['kirocrewConfig']`, shared with every other config reader),
 * because narrowing what consent allows grants nothing on its own.
 *
 * What the switch does: for the share of sessions `decisions.bucket` admits, Jev's
 * answer at `skills.select` decides which skill a message loads. A failure, a
 * timeout or a refusal falls back to the word-matching rule the build ships with,
 * which is why the copy promises a fallback rather than an outage.
 *
 * `decisions.preview` is NOT read as a fallback for the field — see
 * `decisionsPreview.ts`. A gateway carrying only that field is reported as
 * unsupported, with the same disabled switch and reason a pre-`decisions`
 * gateway gets.
 *
 * No `configKey`: the switch writes no config path (see the section's doc
 * comment above).
 *
 * The point row is READ-ONLY on purpose. It names the one thing Jev decides in
 * plain words, with the backend's identifier beside it for a reader matching the
 * card against the decision log. It carries no on/off state of its own — the
 * switch above is that state, and printing it twice read as two controls. The
 * sampling share is a setting an operator moves in `config.json`
 * (`decisions.bucket`), so the sampling line names that path instead of offering
 * a second control. It is printed whether the switch is on or off, phrased as
 * "while this is on": a reader deciding whether to consent needs to know that
 * the shipped default is every session, before flipping the switch, not after.
 *
 * NO "See what it looks like" button, and that is the missing-capture rule the
 * "Chat on a crew" card below states, not an omission: this preview draws no
 * surface at all. What it changes is which skills load, and the receipts are
 * lines in a log file.
 */
function DecisionsPreviewCard() {
  const qc = useQueryClient()
  const configQ = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const consentQ = useQuery({
    queryKey: ['decisionsConsent'],
    queryFn: () => api.getDecisionsConsent(),
    // A 404 is the answer "this gateway predates the keystone", not a transient
    // failure worth retrying: the card renders it as the update notice.
    retry: false,
  })
  const view = readDecisions(consentQ.data, configQ.data)
  const mut = useMutation({
    // The address on screen travels with the click, so consent binds to what the
    // owner reviewed; the gateway refuses (409) if config moved it meanwhile and
    // the refetch below then shows the new address.
    mutationFn: (value: boolean) => api.saveDecisionsConsent(value, view.configuredEndpoint),
    // Refetch rather than trusting the value just sent: the server owns the
    // effective verdict.
    //
    // RETURNED, not just started: react-query holds a mutation pending only while
    // `onSettled` has an unresolved promise outstanding. Dropping the return let
    // `isPending` clear the instant the PUT resolved, which re-enabled the
    // switch for as long as the refetch took — while `checked` still read the
    // pre-flip value. The flip looked like it had not taken, and a second click
    // wrote the same value again.
    onSettled: () => qc.invalidateQueries({ queryKey: ['decisionsConsent'] }),
  })
  // "Old gateway" and "could not read the settings" are different facts and must
  // not share a sentence: the first is a state the user fixes by updating, the
  // second by retrying. An older gateway answers the consent GET with 404, which
  // surfaces here as an error carrying that status; any other failure is the
  // read problem.
  const backendMissing = consentQ.isError && isNotFoundError(consentQ.error)
  const readFailed = configQ.isError || (consentQ.isError && !backendMissing)
  const loading = configQ.isLoading || consentQ.isLoading
  const describedBy =
    [backendMissing ? DECISIONS_BACKEND_NOTE_ID : '', DECISIONS_EGRESS_NOTE_ID]
      .filter(Boolean)
      .join(' ')

  return (
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.featurePreviewsTab.decisions')}
        description={i18nT('pages.developer.featurePreviewsTab.decisions_desc')}
        checked={view.enabled}
        onChange={v => mut.mutate(v)}
        // A keystone that has not been read, or could not be, is no basis for
        // offering a write against the value it holds.
        disabled={loading || readFailed || !view.supported || mut.isPending}
        describedBy={describedBy}
      />
      {/* The egress fact carries body weight, not muted fine print: it is what a
          reader is actually consenting to, and it stays outside the row so a
          disabled switch does not dim its own explanation. The one exception is
          a gateway that cannot run this at all: then nothing can leave the
          machine, and a full-weight warning beside a faded card read as a
          mistake, so it fades with the rest -- at the SAME opacity the disabled
          row uses, or a muted colour still reads darker than a row at 40%. */}
      <p
        id={DECISIONS_EGRESS_NOTE_ID}
        className={backendMissing ? 'text-[12px] text-muted opacity-40' : 'text-[12px] text-text'}
      >
        {i18nT('pages.developer.featurePreviewsTab.decisions_egress')}
      </p>
      {/* WHERE the messages go, as a fact beside the switch: consent is given for
          an address, and the gate holds the config to that address afterwards.
          Mono and untranslated -- it is a URL the reader may want to compare
          against their own provider settings. */}
      {view.supported && view.configuredEndpoint && (
        <p className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_sent_to')}{' '}
          <span className="font-mono break-all">{view.configuredEndpoint}</span>
        </p>
      )}
      {/* The redirected-config state: consent stands for one address, config.json
          now names another, so nothing is sent. A WARNING, not a paragraph: it is
          the one state where the switch reads "on" and the truth is "off", so it
          has to be readable as "something is wrong" without reading it -- the
          colour and icon the bot-channel panel uses for its own not-working
          states. role="alert" so a screen reader hears it when it appears. */}
      {view.endpointMoved && (
        <p role="alert" className="text-[12px] text-warn m-0 flex items-start gap-1.5">
          <AlertTriangle size={13} className="flex-none mt-0.5" aria-hidden="true" />
          <span>{i18nT('pages.developer.featurePreviewsTab.decisions_endpoint_moved')}</span>
        </p>
      )}
      {backendMissing && (
        <p id={DECISIONS_BACKEND_NOTE_ID} className="text-[12px] text-muted">
          {i18nT('pages.developer.featurePreviewsTab.decisions_backend_required')}
        </p>
      )}
      {/* Nothing on this card is a draft, so the hand-off to the agent is on. */}
      {readFailed && (
        <ErrorNotice
          variant="inline"
          className="mt-1"
          askAgent
          message={i18nT('pages.developer.featurePreviewsTab.decisions_config_unavailable')}
        />
      )}
      {mut.isError && (
        <ErrorNotice
          variant="inline"
          className="mt-1"
          askAgent
          message={i18nT('pages.developer.featurePreviewsTab.decisions_save_failed')}
        />
      )}
      {/* Only rendered against a gateway that carries the field: on an older one
          there is no point wired at all, so a row would describe a check that
          does not exist. */}
      {view.supported && (
        <div className="pt-1">
          <div className="text-[12px] text-muted mb-1">
            {i18nT('pages.developer.featurePreviewsTab.decisions_points')}
          </div>
          {/* One row: the plain-words name of what Jev decides, then the backend's
              identifier for it. The identifier stays mono and untranslated — it is
              the string a reader greps the decision log for, and the muted prefix
              says so, since the token alone reads as a second, unexplained name.
              No state column: the switch above already says whether this runs. */}
          <div className="flex items-center justify-between gap-3 text-[12px]">
            <span className="text-text">
              {i18nT('pages.developer.featurePreviewsTab.decisions_point_skills_select')}
            </span>
            <span className="text-muted">
              {i18nT('pages.developer.featurePreviewsTab.decisions_point_logged_as')}{' '}
              <span className="font-mono" title={DECISIONS_LIVE_POINT}>
                {DECISIONS_LIVE_POINT}
              </span>
            </span>
          </div>
          {/* The share is printed in both switch states, as "while this is on":
              the shipped default is 100, and a reader must see "all of your
              sessions" BEFORE consenting, not discover it afterwards. Only a
              config with no printable bucket (see `decisionsPreview.ts`) says
              nothing. */}
          {view.bucket !== null && (
            <p className="text-[12px] text-muted mt-1">
              {view.bucket === 100
                ? i18nT('pages.developer.featurePreviewsTab.decisions_point_sampled_all')
                : i18nT('pages.developer.featurePreviewsTab.decisions_point_sampled', {
                    // Through the format seam: Latin digits and a `%` are wrong
                    // for bn, and a bare `${n}%` would follow the browser's locale.
                    percent: fmtPercent(view.bucket / 100),
                  })}
            </p>
          )}
        </div>
      )}
    </SettingsCard>
  )
}

/**
 * `data-setting-key` anchor on the section wrapper, for
 * `?highlight=key:<this>` — the redirect target of the old Developer-page tab
 * (`DeveloperPage.tsx`). Not a config path and not a registry entry: it only
 * exists so useSettingHighlight's direct DOM lookup can ring the whole section.
 * Neither the settings extractor (which reads `configKey` props on Settings*
 * primitives) nor the SettingRef call-site guard (which scans `<SettingRef>`)
 * sees a bare `data-setting-key` attribute, so it cannot leak into search or
 * pose as a schema path.
 */
export const FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR = 'feature-previews-section'
export function FeaturePreviewsSection() {
  const navigate = useNavigate()
  const webhooks = usePreviewFlag(PREVIEW_WEBHOOKS)
  const crew = usePreviewFlag(PREVIEW_CREW)
  const remoteCrewChat = usePreviewFlag(PREVIEW_REMOTE_CREW_CHAT)
  const instanceSessions = usePreviewFlag(PREVIEW_INSTANCE_SESSIONS)

  return (
    // The wrapper exists for the legacy redirect: `?highlight=key:<anchor>`
    // rings whatever element carries that `data-setting-key`, so this rings
    // the WHOLE section — header, caveat and all three cards — rather than one
    // card. A reader arriving from an old Feature Previews bookmark asked a
    // section-sized question ("where did the tab go?"), and a single ringed
    // row answers a different one ("is this row selected?"). The wrapper takes
    // over the between-sections `mt-4` because SettingsSection's own
    // `first:mt-0` now sees its header as the first child of this div.
    <div data-setting-key={FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR} className="mt-4">
    <SettingsSection title={i18nT('pages.settings.developerPanel.feature_previews')}>
      {/* The "unpolished on purpose" caveat sits under the section header rather
          than repeated per card: it is true of every card here, and the old tab
          carried it once as the page description for the same reason. */}
      <p className="text-[13px] text-muted mb-2">
        {i18nT('pages.settings.developerPanel.feature_previews_desc')}
      </p>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.webhooks')}
          description={i18nT('pages.developer.featurePreviewsTab.inbound_webhook_tokens_registered_contexts_and_r')}
          checked={webhooks}
          onChange={v => setPreviewFlag(PREVIEW_WEBHOOKS, v)}
        />
        {/* One action row under the toggle: "See what it looks like" always, the ingress
            link only once the flag is on. Same row so the card keeps one
            footer whichever state it is in, rather than a link appearing on a
            new line and pushing the next card down. */}
        <div className="flex flex-wrap items-center gap-x-4 pt-1">
          <FeaturePreviewIntroButton
            title={i18nT('pages.developer.featurePreviewsTab.webhooks')}
            intro={webhooksIntro()}
            checked={webhooks}
            onChange={v => setPreviewFlag(PREVIEW_WEBHOOKS, v)}
          />
          {webhooks && (
            <button
              type="button"
              onClick={() => navigate('/webhooks')}
              className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 hover:underline"
            >
              {i18nT('pages.developer.featurePreviewsTab.open_webhooks')}
              {/* An in-app arrow, NOT `ExternalLink`: this navigates in the same
                  tab. Elsewhere in the dashboard the external-link glyph is
                  reserved for pop-outs and off-site URLs, so using it here would
                  promise a new window that never opens. */}
              <ArrowRight size={13} className="lucide-inline" />
            </button>
          )}
        </div>
      </SettingsCard>
      {/* One card, one flag, one door: the Crew Members page (`/members`) and its
          rail item. Crew Mode — the second door this card used to name — retired
          in favour of that page; the sidebar create menu keeps a "Crew Members"
          entry that opens the page, or lands HERE with this card ringed while the
          flag is still off (`ChatSidebar.openCrewMembers`).

          NO ingress button here, deliberately, unlike the webhooks card above. That
          one needs its link because `/webhooks` is `hiddenFromNav` and the card is
          its ONLY door. Crew Members is not: flipping this switch puts the row back
          on the rail in the same tick (`usePreviewFlagRevision`), so a link here
          would be a second spelling of a door the user can already see — and one
          that costs a catalog key in twelve languages permanently. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.crew_members')}
          description={i18nT('pages.developer.featurePreviewsTab.crew_members_desc')}
          checked={crew}
          onChange={v => setPreviewFlag(PREVIEW_CREW, v)}
        />
        {/* "See what it looks like" is not an ingress: it shows the page instead of
            opening it, which is what a reader deciding whether to flip the switch
            needs BEFORE flipping it. */}
        <div className="pt-1">
          <FeaturePreviewIntroButton
            title={i18nT('pages.developer.featurePreviewsTab.crew_members')}
            intro={crewIntro()}
            checked={crew}
            onChange={v => setPreviewFlag(PREVIEW_CREW, v)}
          />
        </div>
      </SettingsCard>
      {/* A SEPARATE card from Crew Members above, because the word names two
          unrelated things: that flag holds the Crew Members page, this one holds
          a chat dispatched to another MACHINE over the instances tunnel. One card
          each keeps a reader from flipping the wrong switch.

          NO ingress button, for the same reason as the crew card: turning it on
          puts the create-menu entry back in the same tick, and that menu is
          already in front of the user.

          NO "See what it looks like" yet either, and that is the missing-capture rule, not
          an omission: the entry it adds only renders while a tunnel to a second
          machine is live (`warmCrews.length > 0` in ChatSidebar), and the
          isolated instance the captures come from has no honest way to stage
          one. A dialog with a staged or drawn frame would break the promise the
          other two dialogs make — that what you see is what will appear. Add a
          builder here the day a real two-machine capture exists. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.chat_on_a_crew')}
          description={i18nT('pages.developer.featurePreviewsTab.chat_on_a_crew_desc')}
          checked={remoteCrewChat}
          onChange={v => setPreviewFlag(PREVIEW_REMOTE_CREW_CHAT, v)}
        />
      </SettingsCard>
      {/* Adjacent to the card above and still SEPARATE from it, because the two
          point opposite ways across the same tunnel: that flag DISPATCHES a chat
          to another machine, this one LISTS the sessions that machine already
          owns. Sharing a card would imply flipping one gets the other.

          NO ingress button, and for a different reason than the crew cards: they
          omit it because their door is already on screen, whereas this preview
          has no page of its own at all — it changes the Sessions list every user
          is already looking at, so the toggle IS the whole affordance. */}
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.developer.featurePreviewsTab.remote_instance_sessions')}
          description={i18nT('pages.developer.featurePreviewsTab.merge_a_connected_remote_instances_live_sessions')}
          checked={instanceSessions}
          onChange={v => setPreviewFlag(PREVIEW_INSTANCE_SESSIONS, v)}
        />
      </SettingsCard>
      {/* LAST, and the only card here whose switch is not a per-device flag: it
          writes `decisions.enabled` in `config.json`. Its own doc comment carries
          why, and why its one point row is read-only. */}
      <DecisionsPreviewCard />
    </SettingsSection>
    </div>
  )
}
