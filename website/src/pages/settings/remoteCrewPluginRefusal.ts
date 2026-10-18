import type { CloudPreflight } from '../../api/client'
import { i18nT } from '../../i18n/t'
import type { ErrorReport } from '../../utils/errorReport'

// AWS's own install page for the plugin: the same page the Remote Crew on EC2
// guide and the gateway's not-installed message (cloud/ssm.py) link. It covers
// every platform, so unlike the install command it can live here.
export const SESSION_MANAGER_PLUGIN_INSTALL_URL =
  'https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html'

// The pre-flight troubleshooting entry of the Remote Crew on EC2 guide: it covers
// both the missing credential helper and a refused plugin.
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

/** The diagnosis in the reader's language, from the kind of refusal the gateway
 *  names. Null for a kind this build has no sentence for: the caller then puts
 *  the safety check's own English reason in the generic sentence. */
export function pluginRefusalSentence(p: CloudPreflight, path: string): string | null {
  switch (p.session_manager_plugin_refused_code) {
    case 'other_owner':
      if (p.session_manager_plugin_refused_owner) {
        return i18nT('pages.settings.remoteCrewPanel.plugin_refused_other_owner', {
          path,
          owner: p.session_manager_plugin_refused_owner,
          uid: String(p.session_manager_plugin_refused_owner_uid ?? ''),
        })
      }
      return typeof p.session_manager_plugin_refused_owner_uid === 'number'
        ? i18nT('pages.settings.remoteCrewPanel.plugin_refused_other_owner_uid', { path, uid: String(p.session_manager_plugin_refused_owner_uid) })
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

export type PluginRefusalCopy = {
  /** The diagnosis: where the plugin was found and why it was refused. */
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
 *  root gateway and the strict policy are not fixed by an ownership change. */
function nextStep(code: string | undefined): string {
  if (code === 'root_gateway') return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next_root_gateway')
  if (code === 'strict_policy') return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next_strict_policy')
  return i18nT('pages.settings.remoteCrewPanel.plugin_refused_next')
}

/** The plugin row's wording for a plugin that is installed but refused, or null
 *  when it was accepted or is simply not installed. */
export function pluginRefusalCopy(p: CloudPreflight): PluginRefusalCopy | null {
  const path = p.session_manager_plugin_refused_path
  if (p.session_manager_plugin || !path) return null
  const raw = p.session_manager_plugin_refused_reason ?? ''
  const translated = pluginRefusalSentence(p, path)
  const failure = translated ?? i18nT('pages.settings.remoteCrewPanel.plugin_refused_found', { path, reason: raw })
  const failureDetail = translated && raw ? raw : undefined
  if (!p.session_manager_plugin_command) {
    // No fix command is known for this refusal, so point at the guide's entry.
    return {
      failure,
      failureDetail,
      steps: {
        text: nextStep(p.session_manager_plugin_refused_code),
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

/** What "Ask the agent" on the refused-plugin notice carries: the sentence on
 *  screen, plus the refusal's code and the safety check's own words, which the
 *  row shows only as a muted line. The pre-flight answered, so this is a
 *  `system` report, not a failed request. */
export function pluginRefusalReport(p: CloudPreflight, copy: PluginRefusalCopy): ErrorReport {
  return {
    id: 'remote-crew-plugin-refused',
    at: Date.now(),
    source: 'system',
    code: p.session_manager_plugin_refused_code || undefined,
    message: copy.failure,
    detail: p.session_manager_plugin_refused_reason || undefined,
    endpoint: '/api/cloud/preflight',
    route: window.location.pathname,
  }
}
