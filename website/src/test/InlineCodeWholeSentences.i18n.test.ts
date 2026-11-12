/**
 * Dashboard sentences that wrap inline `<code>`, `<b>` or `<strong>` as one message (#33069).
 *
 * Each line used to be catalog pieces placed around the marked-up element, so
 * Japanese, Korean, Hindi and Bengali read in English word order. Each is now one
 * catalog message that carries the element as `<Trans>` markup, so every language
 * places it.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']

/** Each new message, its source file, and the elements it must carry. */
const MESSAGES: Array<[string, string, string[]]> = [
  ['apps.issueRadar.connectPanel.reads_through_cli_session', 'apps/issue-radar/ConnectPanel.tsx', ['<cli/>', '<cli/>', '<retry/>']],
  ['apps.issueRadar.views.settings.generalSettings.authenticated_through_cli', 'apps/issue-radar/views/settings/GeneralSettings.tsx', ['<cli/>']],
  ['apps.issueRadar.views.settings.generalSettings.not_signed_in_run_login', 'apps/issue-radar/views/settings/GeneralSettings.tsx', ['<cli/>', '<login/>']],
  ['apps.issueRadar.views.settings.repoSettings.checks_once_a_minute_with_cli', 'apps/issue-radar/views/settings/RepoSettings.tsx', ['<cli/>']],
  ['apps.issueRadar.views.settings.repoSettings.investigate_runs_in_path', 'apps/issue-radar/views/settings/RepoSettings.tsx', ['<path/>']],
  ['components.mcpCustomServerModal.paste_config_hint', 'components/McpCustomServerModal.tsx', ['<servers/>', '<map/>', '<command/>', '<args/>', '<env/>', '<url/>', '<headers/>']],
  ['components.reportProblemModal.saved_to_path', 'components/ReportProblemModal.tsx', ['<path/>']],
  ['pages.artifactDeployPage.configure_profile_on_gateway_host', 'pages/ArtifactDeployPage.tsx', ['<b>', '</b>', '<aws/>']],
  ['pages.artifactDeployPage.sso_needs_cli_v2', 'pages/ArtifactDeployPage.tsx', ['<b>', '</b>', '<error/>', '<path/>']],
  ['pages.artifactDeployPage.apply_iam_policy_yourself', 'pages/ArtifactDeployPage.tsx', ['<action/>', '<iam/>']],
  ['pages.artifactDeployPage.only_profile_name_is_stored', 'pages/ArtifactDeployPage.tsx', ['<b>', '</b>', '<aws/>', '<profile/>']],
  ['pages.artifactDeployPage.bucket_created_private', 'pages/ArtifactDeployPage.tsx', ['<ownership/>', '<b>', '</b>', '<arn/>']],
  ['pages.artifactDeployPage.content_scanned_before_upload', 'pages/ArtifactDeployPage.tsx', ['<aws/>', '<ssh/>']],
  ['pages.artifactDeployPage.writes_only_region_via_command', 'pages/ArtifactDeployPage.tsx', ['<cmd/>']],
  ['pages.projectsPage.yaml_workflows_hard_dag', 'pages/ProjectsPage.tsx', ['<field/>']],
  ['pages.overview.promptsTab.invoke_prompts_in_chat', 'pages/overview/PromptsTab.tsx', ['<sop/>', '<get/>']],
  ['pages.settings.instancesPanel.disabled_restart_to_tear_down_tunnels', 'pages/settings/RemoteCrewPanel.tsx', ['<cmd/>']],
  ['pages.settings.securityPanel.strictest_boundary_on_host_surface', 'pages/settings/SecurityPanelGovernance.tsx', ['<strong>', '</strong>', '<file/>']],
  ['pages.settings.securityPanel.no_policy_author_ceiling', 'pages/settings/SecurityPanelGovernance.tsx', ['<policy/>', '<profiles/>']],
  ['pages.settings.teamsPanel.register_azure_bot_intro', 'pages/settings/TeamsPanel.tsx', ['<channel/>', '<path/>']],
  ['pages.settings.teamsPanel.messaging_endpoint_url', 'pages/settings/TeamsPanel.tsx', ['<url/>']],
  ['pages.settings.teamsPanel.public_host_required_detail', 'pages/settings/TeamsPanel.tsx', ['<localhost/>', '<host/>']],
  ['pages.settings.teamsPanel.side_load_app_with_bot_id', 'pages/settings/TeamsPanel.tsx', ['<field/>']],
  ['pages.settings.instancesPanel.commonly_path_use_absolute', 'pages/settings/InstanceFormFields.tsx', ['<path/>', '<tilde/>']],
]

const t = (lng: string, key: string) => i18next.getFixedT(lng)(key, { provider: 'GitHub' }) as string
const count = (s: string, part: string) => s.split(part).length - 1

describe('sentences around inline code are whole messages', () => {
  it('keeps the English text', () => {
    expect(t('en', 'components.reportProblemModal.saved_to_path')).toBe('Saved to <path/>')
    expect(t('en', 'pages.projectsPage.yaml_workflows_hard_dag')).toBe(
      'YAML workflows bypass the LLM decomposer — <field/> is enforced as a hard DAG constraint.',
    )
    expect(t('en', 'apps.issueRadar.views.settings.generalSettings.not_signed_in_run_login')).toBe(
      'Not signed in — the <cli/> CLI has no active session. Run <login/> in your terminal.',
    )
  })

  it('carries every element exactly as often as English in every shipped language', () => {
    for (const [key, , parts] of MESSAGES) {
      for (const lng of LANGS) {
        const value = t(lng, key)
        expect(value, `${lng} ${key}`).not.toBe(key)
        for (const part of new Set(parts)) {
          expect(count(value, part), `${lng} ${key} ${part}`).toBe(count(t('en', key), part))
        }
      }
    }
  })

  it('lets Japanese put the verb after the inline element', () => {
    expect(t('ja', 'apps.issueRadar.views.settings.generalSettings.not_signed_in_run_login')).toContain('<login/> を実行してください')
    expect(t('ko', 'components.reportProblemModal.saved_to_path')).toContain('<path/>에 저장')
  })

  it('renders each message through one Trans call in its file', () => {
    for (const [key, file] of MESSAGES) {
      const src = fs.readFileSync(path.join(SRC, file), 'utf8')
      expect(src, `${file} ${key}`).toContain(`i18nKey="${key}"`)
    }
  })
})
