import type { CloudPreflight } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ErrorReport } from '../../utils/errorReport'

// AWS's own install page for the plugin: the same page the Remote Crew on EC2
// guide and the gateway's not-installed message (cloud/ssm.py) link. It covers
// every platform, so unlike the install command it can live here.
export const SESSION_MANAGER_PLUGIN_INSTALL_URL =
  'https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html'

// The pre-flight troubleshooting entry of the Remote Crew on EC2 guide: it covers
// the missing credential helper and a refused plugin or AWS CLI.
export const REMOTE_CREW_PREFLIGHT_GUIDE_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-crew-on-ec2.md#set-up-pre-flight-credential-helper-not-found-or-plugin-refused'

// Typed by the user, so never a catalog value: the AWS config file's usual
// place and the command that prints a program's full path.
export const AWS_CONFIG_FILE = '~/.aws/config'
export const whichCommand = (name: string) => `which ${name}`

/** The credentials row's sentence when the AWS CLI could not find the
 *  credential_process helper by its bare name: the edit to make first, then why. */
export function credentialHelperMissingSentence(name: string): string {
  return i18nT('pages.settings.remoteCrewPanel.credentials_helper_missing', {
    name,
    file: AWS_CONFIG_FILE,
    command: whichCommand(name),
  })
}

/** A binary the gateway found and refused, read from one preflight's fields. */
type ToolRefusal = {
  path: string
  reason: string
  code?: string
  owner?: string
  ownerUid?: number | null
  /** The command that clears the refusal, or "" when none is known. */
  command: string
}

function pluginRefusalOf(p: CloudPreflight): ToolRefusal {
  return {
    path: p.session_manager_plugin_refused_path ?? '',
    reason: p.session_manager_plugin_refused_reason ?? '',
    code: p.session_manager_plugin_refused_code,
    owner: p.session_manager_plugin_refused_owner,
    ownerUid: p.session_manager_plugin_refused_owner_uid,
    command: p.session_manager_plugin_command ?? '',
  }
}

/** The CLI's refusal carries no command: none is known to clear it. */
function awsCliRefusalOf(p: CloudPreflight): ToolRefusal {
  return {
    path: p.aws_cli_refused_path ?? '',
    reason: p.aws_cli_refused_reason ?? '',
    code: p.aws_cli_refused_code,
    owner: p.aws_cli_refused_owner,
    ownerUid: p.aws_cli_refused_owner_uid,
    command: '',
  }
}

/** The diagnosis in the reader's language, from the kind of refusal the gateway
 *  names. Null for a kind this build has no sentence for: the caller then puts
 *  the safety check's own English reason in the generic sentence. */
function refusalSentence(r: ToolRefusal): string | null {
  const path = r.path
  switch (r.code) {
    case 'other_owner':
      if (r.owner) {
        return i18nT('pages.settings.remoteCrewPanel.plugin_refused_other_owner', {
          path,
          owner: r.owner,
          uid: String(r.ownerUid ?? ''),
        })
      }
      return typeof r.ownerUid === 'number'
        ? i18nT('pages.settings.remoteCrewPanel.plugin_refused_other_owner_uid', { path, uid: String(r.ownerUid) })
        : null
    case 'world_writable':
      return i18nT('pages.settings.remoteCrewPanel.plugin_refused_world_writable', { path })
    case 'root_gateway':
      return i18nT('pages.settings.remoteCrewPanel.plugin_refused_root_gateway', { path })
    case 'strict_policy':
      return i18nT('pages.settings.remoteCrewPanel.plugin_refused_strict_policy', { path })
    default:
      return null
  }
}

/** The plugin row's diagnosis for a plugin refused at *path*; see refusalSentence. */
export function pluginRefusalSentence(p: CloudPreflight, path: string): string | null {
  return refusalSentence({ ...pluginRefusalOf(p), path })
}

export type PluginRefusalCopy = {
  /** The diagnosis: where the binary was found and why it was refused. */
  failure: string
  /** The safety check's own English reason, under a translated one, as the
   *  notice's smaller second line. */
  failureDetail?: string
  /** What to do about it, as plain text under the diagnosis. */
  steps: { text: string; link?: { href: string; label: string } }
  /** Under the ownership command: where it runs and what it changes. */
  commandNote?: string
}

/** The next step when no command is known, chosen by the refusal's kind: a
 *  root gateway and the strict policy are not fixed by an ownership change, and
 *  another account's file must not be made trusted, so its step is AWS's copy. */
function nextStep(code: string | undefined): string {
  if (code === 'other_owner') return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next_other_owner')
  if (code === 'root_gateway') return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next_root_gateway')
  if (code === 'strict_policy') return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next_strict_policy')
  return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next')
}

function refusalCopy(r: ToolRefusal): PluginRefusalCopy {
  const translated = refusalSentence(r)
  const failure = translated ?? i18nT('pages.settings.remoteCrewPanel.plugin_refused_found', { path: r.path, reason: r.reason })
  const failureDetail = translated && r.reason ? r.reason : undefined
  if (!r.command) {
    // No fix command is known for this refusal, so point at the guide's entry.
    return {
      failure,
      failureDetail,
      steps: {
        text: nextStep(r.code),
        link: { href: REMOTE_CREW_PREFLIGHT_GUIDE_URL, label: i18nT('pages.settings.remoteCrewPanel.plugin_refused_guide') },
      },
    }
  }
  // Root-owning the folder makes whatever file is in it trusted, so the
  // reinstall-from-AWS caution comes before the command, with AWS's page.
  return {
    failure,
    failureDetail,
    steps: {
      text: i18nT('pages.settings.remoteCrewPanel.plugin_refused_fix_steps'),
      link: { href: SESSION_MANAGER_PLUGIN_INSTALL_URL, label: i18nT('pages.settings.remoteCrewPanel.plugin_install_page') },
    },
    commandNote: i18nT('pages.settings.remoteCrewPanel.plugin_ownership_command_note'),
  }
}

/** The plugin row's wording for a plugin that is installed but refused, or null
 *  when it was accepted or is simply not installed. */
export function pluginRefusalCopy(p: CloudPreflight): PluginRefusalCopy | null {
  if (p.session_manager_plugin || !p.session_manager_plugin_refused_path) return null
  return refusalCopy(pluginRefusalOf(p))
}

/** The credentials row's wording when the gateway found an `aws` CLI and refused
 *  it, or null when credentials resolved or no CLI was refused. Reporting that as
 *  the CLI's "not found" error would send the reader to reinstall the same file. */
export function awsCliRefusalCopy(p: CloudPreflight): PluginRefusalCopy | null {
  if ((p.reachable && p.account) || !p.aws_cli_refused_path) return null
  const copy = refusalCopy(awsCliRefusalOf(p))
  return { ...copy, failure: i18nT('pages.settings.remoteCrewPanel.aws_cli_refused', { diagnosis: copy.failure }) }
}

/** What "Ask the agent" on a refused-binary notice carries: the sentence on
 *  screen, plus the refusal's code and the safety check's own words, which the
 *  row shows only as a muted line. The pre-flight answered, so this is a
 *  `system` report, not a failed request. */
function refusalReport(id: string, r: ToolRefusal, copy: PluginRefusalCopy): ErrorReport {
  return {
    id,
    at: Date.now(),
    source: 'system',
    code: r.code || undefined,
    message: copy.failure,
    detail: r.reason || undefined,
    endpoint: '/api/cloud/preflight',
    route: window.location.pathname,
  }
}

export function pluginRefusalReport(p: CloudPreflight, copy: PluginRefusalCopy): ErrorReport {
  return refusalReport('remote-crew-plugin-refused', pluginRefusalOf(p), copy)
}

export function awsCliRefusalReport(p: CloudPreflight, copy: PluginRefusalCopy): ErrorReport {
  return refusalReport('remote-crew-aws-cli-refused', awsCliRefusalOf(p), copy)
}
