/**
 * Settings -> Remote Crew -> Add Crew -> Set up a new Crew: the two cards of the
 * built-in EC2 launcher, the AWS prerequisites checklist (with the account inputs
 * it probes) and the launch form. Presentational only: `RemoteCrewPanel.tsx`
 * owns every value and callback, so the form keeps its state while the Add Crew
 * modal closes and reopens.
 */
import type { UseQueryResult } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { AlertTriangle, Check, CheckCircle, ChevronDown, Copy, ExternalLink, RefreshCw, Rocket } from 'lucide-react'
import { ApiError, type CloudIdentity, type CloudPreflight } from '../../api/client'
import { Card, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { parseErrorCode, parseErrorField, reportForError, type ErrorReport } from '../../utils/errorReport'
import { i18nT } from '../../i18n/t'
import {
  awsCliRefusalCopy, awsCliRefusalReport, credentialHelperMissingSentence, envCredentialsSentence, pluginMissingSteps, pluginRefusalCopy, pluginRefusalReport,
} from './remoteCrewPluginRefusal'
import { PRICE_REGION, PRICING_CALCULATOR_URL, SIZE_TIERS, X86_TIERS, type SizeTier } from './remoteCrewPanelModel'
import { PrereqRow, SizeCard } from './RemoteCrewPanelLaunch'

/** The AWS prerequisites card: profile and Region inputs, then the checklist they produce. */
export function PrereqCard({
  profile, setProfile, region, setRegion, regionOk, runCheck, checkedProfile, checkedRegion, checkStale,
  preflightQuery, errMsg, copyPolicy, copyCommand, copied, policyNotice, copyErr,
}: {
  profile: string
  setProfile: (v: string) => void
  region: string
  setRegion: (v: string) => void
  regionOk: boolean
  runCheck: () => void
  checkedProfile: string
  checkedRegion: string
  checkStale: boolean
  preflightQuery: UseQueryResult<CloudPreflight>
  errMsg: (e: unknown, fallback: string) => string
  copyPolicy: () => void
  copyCommand: (command: string) => Promise<void>
  copied: 'command' | 'policy' | null
  policyNotice: string
  copyErr: { target: 'command' | 'policy'; message: string } | null
}) {
  const preflight = preflightQuery.data
  // The gateway could not run the `aws` CLI at all: this host has no sandbox
  // for it. Keyed on the code, so the sentence is the reader's language, and the
  // no-result line below must not blame credentials nobody read.
  const preflightNoSandbox = preflightQuery.error instanceof ApiError
    && parseErrorCode(preflightQuery.error.body) === 'sandbox_unavailable'
  // Only `no_backend` means this host has no sandbox: `transient` clears on a
  // retry, and `foreign_sandbox` is an outer sandbox around a host that has one.
  const preflightSandboxKind = preflightNoSandbox && preflightQuery.error instanceof ApiError
    ? parseErrorField(preflightQuery.error.body, 'kind')
    : undefined
  // Found but refused is not "not installed": the install command would put the
  // same refused file back, so the plugin row says why it was refused instead.
  const pluginRefusal = preflight ? pluginRefusalCopy(preflight) : null
  const pluginMissing = preflight ? pluginMissingSteps(preflight) : null
  // The same for an `aws` CLI the gateway found and refused: the credentials row
  // says why, instead of the CLI's "not found" error and its install link.
  const cliRefusal = preflight ? awsCliRefusalCopy(preflight) : null
  // The service calls run with the profile's credentials, so when those did not
  // resolve none of them ran: the rows say so instead of blaming a permission.
  // A call that ran and failed is an error, so it goes through `failure`.
  // The three rows share one sentence, so the hand-off names the check and what it ran against (profile "" = default).
  const serviceFailureReport = (flag: string): ErrorReport => ({
    id: `remote-crew-${flag}`,
    at: Date.now(),
    source: 'system',
    message: i18nT('pages.settings.remoteCrewPanel.service_missing'),
    detail: JSON.stringify({ [flag]: false, profile: checkedProfile, region: checkedRegion }),
    endpoint: '/api/cloud/preflight',
    route: window.location.pathname,
  })
  const serviceRow = (reachable: boolean, flag: string) => !preflight?.reachable
    ? { ok: null, detail: i18nT('pages.settings.remoteCrewPanel.service_not_checked') }
    : reachable
      ? { ok: true, detail: i18nT('pages.settings.remoteCrewPanel.service_ok') }
      : { ok: false, detail: '', failure: i18nT('pages.settings.remoteCrewPanel.service_missing'), failureReport: serviceFailureReport(flag) }
  // Copy policy JSON only on a CloudFormation call that ran and failed.
  const cloudformationRow = serviceRow(!!preflight?.cloudformation_reachable, 'cloudformation_reachable')
  // AWS prerequisites — the account inputs live HERE, above the rows they
  // produce. The check runs against this profile/region, so showing the
  // verdict first and the inputs in a later card inverted cause and effect:
  // a red "credentials expired" row gave no hint that it had probed a
  // different profile than the one the reader had in mind. These same two
  // values are also what the launch below uses.
  return (
    <Card>
      <div className="flex items-center gap-2 mb-3 text-text font-medium">
        <CheckCircle className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.before_you_start')}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
        <label htmlFor="cloud-profile" className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.instancesPanel.aws_profile')}
          <input
            id="cloud-profile"
            aria-label={i18nT('pages.settings.instancesPanel.aws_profile')}
            className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-hidden focus-ring"
            value={profile}
            onChange={e => setProfile(e.target.value)}
            onBlur={runCheck}
            placeholder={i18nT('pages.settings.instancesPanel.default_credential_chain')}
          />
        </label>
        <label htmlFor="cloud-region" className="flex flex-col gap-1 text-[13px] text-muted">
          {i18nT('pages.settings.remoteCrewPanel.region')}
          <input
            id="cloud-region"
            aria-label={i18nT('pages.settings.remoteCrewPanel.region')}
            className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-hidden focus-ring aria-invalid:border-danger"
            value={region}
            onChange={e => setRegion(e.target.value)}
            onBlur={runCheck}
            placeholder={i18nT('pages.settings.remoteCrewPanel.region_placeholder')}
            required
            aria-invalid={!regionOk}
            aria-describedby={regionOk ? undefined : 'cloud-region-error'}
          />
          {!regionOk && (
            <span id="cloud-region-error" className="text-[12px] text-danger">
              {i18nT('pages.settings.remoteCrewPanel.region_required')}
            </span>
          )}
        </label>
      </div>
      <p className="mb-3 text-[12px] text-muted">
        {checkedProfile
          ? checkedRegion
            ? i18nT('pages.settings.remoteCrewPanel.checking_against', { profile: checkedProfile, region: checkedRegion })
            : i18nT('pages.settings.remoteCrewPanel.checking_against_no_region', { profile: checkedProfile })
          : checkedRegion
            ? i18nT('pages.settings.remoteCrewPanel.checking_against_default', { region: checkedRegion })
            : i18nT('pages.settings.remoteCrewPanel.checking_against_default_no_region')}
        {checkStale ? <span className="block text-warn-text">{i18nT('pages.settings.remoteCrewPanel.results_stale')}</span> : null}
      </p>
      {/* Rendered independently of `preflight`: a failed RE-check keeps the
          previous result in cache, and the rows below would otherwise paint
          that stale verdict as if the check had just passed. askAgent ON:
          the launch form on this tab keeps every entry the reader has not
          launched with (see the diagnosis notice above), so the navigation
          destroys nothing, and a preflight that cannot even run is exactly
          the credential/CLI problem the agent can look into. */}
      {preflightQuery.isError && (
        <ErrorNotice
          className="mb-2"
          message={preflightNoSandbox
            ? preflightSandboxKind === 'transient'
              ? i18nT('pages.settings.remoteCrewPanel.aws_check_sandbox_busy')
              : preflightSandboxKind === 'foreign_sandbox'
                ? i18nT('pages.settings.remoteCrewPanel.aws_check_outer_sandbox')
                : i18nT('pages.settings.remoteCrewPanel.aws_check_no_sandbox')
            : errMsg(preflightQuery.error, i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
          report={preflightNoSandbox ? reportForError(preflightQuery.error) : undefined}
          askAgent
        />
      )}
      {preflightQuery.isLoading ? (
        <div className="flex items-center gap-2 text-muted text-sm py-2">
          <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.remoteCrewPanel.checking')}
        </div>
      ) : preflight ? (
        <ul className="m-0 p-0 list-none">
          <PrereqRow
            ok={preflight.reachable && !!preflight.account}
            stale={checkStale}
            title={i18nT('pages.settings.remoteCrewPanel.prereq_credentials')}
            detail={preflight.reachable && preflight.account
              ? checkedProfile
                ? i18nT('pages.settings.remoteCrewPanel.credentials_ok', { profile: checkedProfile, account: preflight.account })
                : i18nT('pages.settings.remoteCrewPanel.credentials_ok_default', { account: preflight.account })
              : ''}
            failure={preflight.reachable && preflight.account
              ? undefined
              : cliRefusal
                ? cliRefusal.failure
              : preflight.credential_helper_missing
                // The helper's name is the actionable part, so the sentence
                // leads; the CLI's own error sits under it (failureDetail)
                // for anything the sentence misses.
                ? credentialHelperMissingSentence(preflight.credential_helper_missing)
              : preflight.env_credentials
                ? envCredentialsSentence()
                : (preflight.detail || i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
            failureDetail={preflight.reachable && preflight.account
              ? undefined
              : cliRefusal
                ? cliRefusal.failureDetail
                : ((preflight.credential_helper_missing || preflight.env_credentials) && preflight.detail) || undefined}
            failureReport={cliRefusal ? awsCliRefusalReport(preflight, cliRefusal) : undefined}
            steps={cliRefusal?.steps}
            onRecheck={runCheck}
            rechecking={preflightQuery.isFetching}
          />
          <PrereqRow {...serviceRow(preflight.ec2_reachable, 'ec2_reachable')} stale={checkStale} title={i18nT('pages.settings.remoteCrewPanel.prereq_ec2')} />
          <PrereqRow {...cloudformationRow} stale={checkStale} title={i18nT('pages.settings.remoteCrewPanel.prereq_cloudformation')} extraAction={cloudformationRow.ok !== false ? undefined : <><Btn onClick={copyPolicy}>{copied === 'policy' ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />} {copied === 'policy' ? i18nT('pages.settings.remoteCrewPanel.copied') : i18nT('pages.settings.remoteCrewPanel.copy_policy_json')}</Btn>{policyNotice ? <span role="status" className="text-[12px] text-muted">{policyNotice}</span> : null}</>} error={copyErr?.target === 'policy' ? copyErr.message : undefined} />
          <PrereqRow {...serviceRow(preflight.ssm_reachable, 'ssm_reachable')} stale={checkStale} title={i18nT('pages.settings.remoteCrewPanel.prereq_ssm')} />
          <PrereqRow
            ok={preflight.session_manager_plugin}
            stale={checkStale}
            title={i18nT('pages.settings.remoteCrewPanel.plugin')}
            detail={preflight.session_manager_plugin
              ? i18nT('pages.settings.remoteCrewPanel.plugin_ok')
              : i18nT('pages.settings.remoteCrewPanel.plugin_missing')}
            failure={pluginRefusal?.failure}
            failureDetail={pluginRefusal?.failureDetail}
            failureReport={pluginRefusal ? pluginRefusalReport(preflight, pluginRefusal) : undefined}
            steps={pluginRefusal?.steps ?? pluginMissing ?? undefined}
            commandNote={pluginRefusal?.commandNote}
            command={preflight.session_manager_plugin ? undefined : (preflight.session_manager_plugin_command || undefined)}
            onCopyCommand={
              preflight.session_manager_plugin || !preflight.session_manager_plugin_command
                ? undefined
                : () => { void copyCommand(preflight.session_manager_plugin_command as string) }
            }
            copied={copied === 'command'}
            error={copyErr?.target === 'command' ? copyErr.message : undefined}
            onRecheck={preflight.session_manager_plugin ? undefined : runCheck}
            rechecking={preflightQuery.isFetching}
          />
        </ul>
      ) : (
        <div className="text-[13px] text-muted py-1">
          {/* The failure itself renders above, independent of this branch;
              this is the no-result state with its Re-check. A check that
              failed to run read no credentials, so it must not blame them. */}
          {preflightQuery.isError
            ? i18nT('pages.settings.remoteCrewPanel.check_not_run')
            : i18nT('pages.settings.remoteCrewPanel.credentials_bad')}
          <div className="mt-2"><Btn onClick={runCheck} disabled={preflightQuery.isFetching}><RefreshCw className={`lucide-inline${preflightQuery.isFetching ? ' animate-spin' : ''}`} /> {preflightQuery.isFetching ? i18nT('pages.settings.remoteCrewPanel.checking') : i18nT('pages.settings.remoteCrewPanel.re_check')}</Btn></div>
        </div>
      )}
      <p className="mt-3 text-[12px] text-muted flex items-start gap-1.5">
        <CheckCircle size={13} className="mt-0.5 shrink-0 text-ok" /> {i18nT('pages.settings.remoteCrewPanel.profile_name_only')}
      </p>
    </Card>
  )
}

const SUBNET_ERROR_ID = 'remote-crew-subnet-error'

/** The launch form: size, subnet, the Kiro identity the crew signs in as, billing note and Launch. */
export function LaunchFormCard({
  region, sizeKey, setSizeKey, showMoreSizes, setShowMoreSizes, subnetId, setSubnetId, subnetErr,
  identityQuery, identityChosen, identityUnknownCause, identityRadioShown, identityMode,
  identityStartUrl, identityStartUrlOk, identityRegion, identityRegionOk, onIdentity,
  launchBuiltin, launchReady, alreadyLaunched, launching, setupRunning, launchFailed,
  launchFailedBeforeProvision, launchFailedCrewListed, identityResolving, identityUnknown,
  identityOk, regionOk, blockingOk, blockingCheck,
}: {
  region: string
  sizeKey: SizeTier['key']
  setSizeKey: (k: SizeTier['key']) => void
  showMoreSizes: boolean
  setShowMoreSizes: (f: (v: boolean) => boolean) => void
  subnetId: string
  setSubnetId: (v: string) => void
  /** A launch refused for this subnet, shown under the field. */
  subnetErr: string | null
  identityQuery: UseQueryResult<CloudIdentity>
  identityChosen: boolean
  identityUnknownCause: 'no_answer' | 'no_portal' | null
  identityRadioShown: boolean
  identityMode: 'builder_id' | 'identity_center'
  identityStartUrl: string
  identityStartUrlOk: boolean
  identityRegion: string
  identityRegionOk: boolean
  /** The reader's own sign-in choice: marks it chosen and sets the field. */
  onIdentity: (patch: { mode?: 'builder_id' | 'identity_center'; startUrl?: string; region?: string }) => void
  launchBuiltin: () => void
  launchReady: boolean
  alreadyLaunched: boolean
  launching: boolean
  setupRunning: boolean
  launchFailed: boolean
  launchFailedBeforeProvision: boolean
  launchFailedCrewListed: boolean
  identityResolving: boolean
  identityUnknown: boolean
  identityOk: boolean
  regionOk: boolean
  blockingOk: boolean
  blockingCheck: string | undefined
}) {
  // Launch form — profile/region are set in the prerequisites card above,
  // which is the same account this launches into.
  return (
    <Card>
      <div className="text-text font-medium mb-3">{i18nT('pages.settings.remoteCrewPanel.new_cloud_crew')}</div>

      <div>
        <div className="text-[13px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.size')}</div>
        {region.trim() !== PRICE_REGION && (
          <p className="text-[12px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.price_region_note', { priceRegion: PRICE_REGION })}</p>
        )}
        <div className="space-y-2.5">
          {SIZE_TIERS.map(tier => (
            <SizeCard key={tier.key} tier={tier} on={sizeKey === tier.key} onPick={setSizeKey} />
          ))}
        </div>

        <button
          type="button"
          onClick={() => setShowMoreSizes(v => !v)}
          aria-expanded={showMoreSizes}
          className="mt-2.5 w-full flex items-center gap-2 text-[12px] text-muted px-3 py-2 rounded-md border border-dashed border-border-strong hover:text-text"
        >
          <ChevronDown size={14} aria-hidden="true" className={`transition-transform ${showMoreSizes ? 'rotate-180' : ''}`} /> {i18nT('pages.settings.remoteCrewPanel.more_sizes')}
        </button>
        {showMoreSizes && (
          <>
            <p className="mt-2 text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.more_sizes_hint')}</p>
            <div className="mt-2 space-y-2.5">
              {X86_TIERS.map(tier => (
                <SizeCard key={tier.key} tier={tier} on={sizeKey === tier.key} onPick={setSizeKey} />
              ))}
            </div>
          </>
        )}
      </div>

      <label className="mt-4 block text-[13px] text-muted">
        {i18nT('pages.settings.remoteCrewPanel.subnet')}
        <input
          type="text"
          value={subnetId}
          aria-label={i18nT('pages.settings.remoteCrewPanel.subnet')}
          aria-invalid={subnetErr ? true : undefined}
          aria-describedby={subnetErr ? SUBNET_ERROR_ID : undefined}
          onChange={e => setSubnetId(e.target.value)}
          placeholder="subnet-0123456789abcdef0"
          spellCheck={false}
          className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
        />
        <span className="block mt-1 text-[12px]">{i18nT('pages.settings.remoteCrewPanel.subnet_hint')}</span>
      </label>
      {/* askAgent ON: the unlaunched subnet draft lives in the store and survives the hand-off.
          The row wraps so the hand-off takes the line under the message in the narrow modal. */}
      {subnetErr && <ErrorNotice id={SUBNET_ERROR_ID} variant="inline" className="mt-1 flex-wrap" messageClassName="grow basis-[calc(100%-2rem)]" message={subnetErr} askAgent />}

      <div className="mt-4">
        <div className="text-[13px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.identity')}</div>
        {identityQuery.isError ? (
          <>
            {/* askAgent ON: the launch form keeps every entry the reader has
                not launched with (crewForms.launch), so the hand-off
                loses nothing, and a sign-in the gateway could not read is one
                the agent can look into. The failure is non-blocking: the user
                can still pick and type the target below. Once they have
                picked, the notice says the selected option is theirs rather
                than asking them to choose. */}
            <ErrorNotice
              variant="inline"
              className="mb-2"
              message={i18nT(identityChosen
                ? 'pages.settings.remoteCrewPanel.identity_unknown_chosen'
                : 'pages.settings.remoteCrewPanel.identity_lookup_failed')}
              askAgent
            />
          </>
        ) : identityUnknownCause !== null ? (
          <>
            {/* askAgent ON, as above. The server suggests nothing, so no radio
                is checked until the user picks, and Launch waits for that pick.
                The notice names the cause the server reported: whoami did not
                answer, or an Identity Center sign-in was read without its
                portal address; after a pick it says the choice is theirs.
                `partial`, not red: the form still works, only the preselect
                is missing. A signed-out kiro-cli no longer lands here. */}
            <ErrorNotice
              variant="inline"
              impact="partial"
              className="mb-2"
              message={i18nT(
                identityChosen
                  ? 'pages.settings.remoteCrewPanel.identity_unknown_chosen'
                  : identityUnknownCause === 'no_portal'
                    ? 'pages.settings.remoteCrewPanel.identity_unknown_no_portal'
                    : 'pages.settings.remoteCrewPanel.identity_unknown_no_answer',
              )}
              askAgent
            />
          </>
        ) : null}
        <div className="space-y-2">
          <label className="flex items-start gap-2 text-[13px] text-text cursor-pointer">
            <input
              type="radio"
              name="kiro-identity"
              className="mt-0.5"
              checked={identityRadioShown && identityMode === 'builder_id'}
              aria-label={i18nT('pages.settings.remoteCrewPanel.identity_builder_id')}
              onChange={() => { onIdentity({ mode: 'builder_id' }) }}
              // Re-selecting the already-checked default fires no change
              // event, but it IS the user's explicit choice, and that
              // choice ends the wait for the inherited identity.
              onClick={() => { onIdentity({ mode: 'builder_id' }) }}
            />
            <span>
              {i18nT('pages.settings.remoteCrewPanel.identity_builder_id')}
              <span className="block text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.identity_builder_id_hint')}</span>
            </span>
          </label>
          <label className="flex items-start gap-2 text-[13px] text-text cursor-pointer">
            <input
              type="radio"
              name="kiro-identity"
              className="mt-0.5"
              checked={identityRadioShown && identityMode === 'identity_center'}
              aria-label={i18nT('pages.settings.remoteCrewPanel.identity_center')}
              onChange={() => { onIdentity({ mode: 'identity_center' }) }}
            />
            <span>
              {i18nT('pages.settings.remoteCrewPanel.identity_center')}
              <span className="block text-[12px] text-muted">
                {identityQuery.data?.discovery !== 'unknown' && identityQuery.data?.identity?.account_type === 'IamIdentityCenter'
                  ? i18nT('pages.settings.remoteCrewPanel.identity_center_inherited')
                  : i18nT('pages.settings.remoteCrewPanel.identity_center_hint')}
              </span>
            </span>
          </label>
          {identityMode === 'identity_center' && (
            <div className="ml-6 space-y-2">
              <label className="block text-[12px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.identity_start_url')}
                <input
                  type="url"
                  value={identityStartUrl}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.identity_start_url')}
                  onChange={e => { onIdentity({ startUrl: e.target.value }) }}
                  placeholder="https://example.awsapps.com/start"
                  spellCheck={false}
                  aria-invalid={identityStartUrl !== '' && !identityStartUrlOk}
                  className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
                />
              </label>
              <label className="block text-[12px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.identity_region')}
                <input
                  type="text"
                  value={identityRegion}
                  aria-label={i18nT('pages.settings.remoteCrewPanel.identity_region')}
                  onChange={e => { onIdentity({ region: e.target.value }) }}
                  placeholder={i18nT('pages.settings.remoteCrewPanel.region_placeholder')}
                  spellCheck={false}
                  required
                  aria-invalid={!identityRegionOk}
                  aria-describedby={identityRegionOk ? undefined : 'identity-region-error'}
                  className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent aria-invalid:border-danger"
                />
                {!identityRegionOk && (
                  <span id="identity-region-error" className="block mt-1 text-danger">
                    {i18nT('pages.settings.remoteCrewPanel.region_required')}
                  </span>
                )}
                <span className="block mt-1">{i18nT('pages.settings.remoteCrewPanel.identity_region_hint')}</span>
              </label>
            </div>
          )}
        </div>
      </div>

      <div className="mt-4 flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5">
        <AlertTriangle size={15} className="mt-0.5 shrink-0 text-warn" />
        <div className="text-[12px] text-text">
          <Trans i18nKey="pages.settings.remoteCrewPanel.billing_pricing_calculator" components={{ link: (
            <a className="text-accent font-medium hover:underline inline-flex items-center gap-1" href={PRICING_CALCULATOR_URL} target="_blank" rel="noreferrer">
              {i18nT('pages.settings.remoteCrewPanel.pricing_calculator')} <ExternalLink size={12} />
            </a>
          ) }} />
        </div>
      </div>

      <div className="mt-4 flex items-center gap-3 flex-wrap">
        <Btn primary onClick={launchBuiltin} disabled={!launchReady || alreadyLaunched}>
          <Rocket className="lucide-inline" /> {launching ? i18nT('pages.settings.remoteCrewPanel.launching') : i18nT('pages.settings.remoteCrewPanel.launch')}
        </Btn>
        {/* After a launch, Launch stays off for the same settings; this is
            the explicit way to start a second crew with them. It sends the
            launch itself, as its label says. */}
        {alreadyLaunched && !launching && !setupRunning && (
          <Btn onClick={launchBuiltin} disabled={!launchReady}>{i18nT('pages.settings.remoteCrewPanel.launch_another')}</Btn>
        )}
        {launchFailed && !launching ? (
          /* No hand-off: the subnet and sign-in choice this retry will send
             live only in this form's state (the store copy was dropped when
             the gateway accepted the launch), so navigating away loses them. */
          <ErrorNotice
            variant="inline"
            message={launchFailedBeforeProvision
              ? i18nT('pages.settings.remoteCrewPanel.launch_failed_retry')
              : launchFailedCrewListed
                ? i18nT('pages.settings.remoteCrewPanel.launch_failed_crew_listed')
                : i18nT('pages.settings.remoteCrewPanel.launch_failed_check_crews')}
          />
        ) : (
        <span className="text-[12px] text-muted">
          {setupRunning && !launching
            ? i18nT('pages.settings.remoteCrewPanel.launch_wait_for_setup')
            : alreadyLaunched && !launching
            ? i18nT('pages.settings.remoteCrewPanel.launched_same_settings')
            : identityResolving
            ? identityUnknown
              ? i18nT('pages.settings.remoteCrewPanel.identity_choose')
              : i18nT('pages.settings.remoteCrewPanel.identity_resolving')
            : !identityOk
              ? i18nT('pages.settings.remoteCrewPanel.identity_incomplete')
              : !regionOk
                ? i18nT('pages.settings.remoteCrewPanel.region_required')
                : blockingOk
                  ? i18nT('pages.settings.remoteCrewPanel.ready_in_6')
                  : blockingCheck
                    ? i18nT('pages.settings.remoteCrewPanel.finish_prereq_named', { check: blockingCheck })
                    : i18nT('pages.settings.remoteCrewPanel.finish_prereqs')}
        </span>
        )}
      </div>
    </Card>
  )
}
