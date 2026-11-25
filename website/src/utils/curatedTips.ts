// The curated feature tips (`src/kiro_crew/data/tips_curated.json`) in the
// dashboard's language.
//
// The server sends each curated tip's English, which every reader without a
// catalog (an edition's tip pool, the API) keeps using. These maps name the
// same English, and its translations, under catalog keys by tip id; the
// curated-tip test pins the English of the two copies byte for byte.

import { i18nT } from '../i18n/t'

const CURATED_TIP_TITLE_KEY = {
  'split-view': 'components.tipCard.curated.split_view_title',
  'command-palette': 'components.tipCard.curated.command_palette_title',
  'focus-mode': 'components.tipCard.curated.focus_mode_title',
  'steer-or-queue': 'components.tipCard.curated.steer_or_queue_title',
  'interface-cli-mode': 'components.tipCard.curated.interface_cli_mode_title',
  'warm-pool': 'components.tipCard.curated.warm_pool_title',
  'mcp-gateway': 'components.tipCard.curated.mcp_gateway_title',
  'delegate-independent-work': 'components.tipCard.curated.delegate_independent_work_title',
  'local-telemetry': 'components.tipCard.curated.local_telemetry_title',
  'zero-token-cron': 'components.tipCard.curated.zero_token_cron_title',
  'dev-fleet': 'components.tipCard.curated.dev_fleet_title',
  'app-store': 'components.tipCard.curated.app_store_title',
  'system-monitor': 'components.tipCard.curated.system_monitor_title',
  'web-terminal': 'components.tipCard.curated.web_terminal_title',
  'artifacts-library': 'components.tipCard.curated.artifacts_library_title',
  'language-picker': 'components.tipCard.curated.language_picker_title',
  'auto-skills': 'components.tipCard.curated.auto_skills_title',
  'role-models': 'components.tipCard.curated.role_models_title',
  'agent-browsing': 'components.tipCard.curated.agent_browsing_title',
  'computer-use': 'components.tipCard.curated.computer_use_title',
  'remote-crew': 'components.tipCard.curated.remote_crew_title',
  'secrets-vault': 'components.tipCard.curated.secrets_vault_title',
  'schedule-repeated-work': 'components.tipCard.curated.schedule_repeated_work_title',
  'monitor-change': 'components.tipCard.curated.monitor_change_title',
  'reusable-workflow': 'components.tipCard.curated.reusable_workflow_title',
  'remember-preference': 'components.tipCard.curated.remember_preference_title',
  'reusable-skill': 'components.tipCard.curated.reusable_skill_title',
  'add-mcp-server': 'components.tipCard.curated.add_mcp_server_title',
  'project-steering': 'components.tipCard.curated.project_steering_title',
  'knowledge-source': 'components.tipCard.curated.knowledge_source_title',
  'hooks-automation': 'components.tipCard.curated.hooks_automation_title',
  'custom-agent': 'components.tipCard.curated.custom_agent_title',
  'interactive-widget': 'components.tipCard.curated.interactive_widget_title',
  'always-on-service': 'components.tipCard.curated.always_on_service_title',
  'interfaces': 'components.tipCard.curated.interfaces_title',
  'approval-modes': 'components.tipCard.curated.approval_modes_title',
  'shortcuts-help': 'components.tipCard.curated.shortcuts_help_title',
} as const

type CuratedTipId = keyof typeof CURATED_TIP_TITLE_KEY

const CURATED_TIP_BODY_KEY = {
  'split-view': 'components.tipCard.curated.split_view_body',
  'command-palette': 'components.tipCard.curated.command_palette_body',
  'focus-mode': 'components.tipCard.curated.focus_mode_body',
  'steer-or-queue': 'components.tipCard.curated.steer_or_queue_body',
  'interface-cli-mode': 'components.tipCard.curated.interface_cli_mode_body',
  'warm-pool': 'components.tipCard.curated.warm_pool_body',
  'mcp-gateway': 'components.tipCard.curated.mcp_gateway_body',
  'delegate-independent-work': 'components.tipCard.curated.delegate_independent_work_body',
  'local-telemetry': 'components.tipCard.curated.local_telemetry_body',
  'zero-token-cron': 'components.tipCard.curated.zero_token_cron_body',
  'dev-fleet': 'components.tipCard.curated.dev_fleet_body',
  'app-store': 'components.tipCard.curated.app_store_body',
  'system-monitor': 'components.tipCard.curated.system_monitor_body',
  'web-terminal': 'components.tipCard.curated.web_terminal_body',
  'artifacts-library': 'components.tipCard.curated.artifacts_library_body',
  'language-picker': 'components.tipCard.curated.language_picker_body',
  'auto-skills': 'components.tipCard.curated.auto_skills_body',
  'role-models': 'components.tipCard.curated.role_models_body',
  'agent-browsing': 'components.tipCard.curated.agent_browsing_body',
  'computer-use': 'components.tipCard.curated.computer_use_body',
  'remote-crew': 'components.tipCard.curated.remote_crew_body',
  'secrets-vault': 'components.tipCard.curated.secrets_vault_body',
  'schedule-repeated-work': 'components.tipCard.curated.schedule_repeated_work_body',
  'monitor-change': 'components.tipCard.curated.monitor_change_body',
  'reusable-workflow': 'components.tipCard.curated.reusable_workflow_body',
  'remember-preference': 'components.tipCard.curated.remember_preference_body',
  'reusable-skill': 'components.tipCard.curated.reusable_skill_body',
  'add-mcp-server': 'components.tipCard.curated.add_mcp_server_body',
  'project-steering': 'components.tipCard.curated.project_steering_body',
  'knowledge-source': 'components.tipCard.curated.knowledge_source_body',
  'hooks-automation': 'components.tipCard.curated.hooks_automation_body',
  'custom-agent': 'components.tipCard.curated.custom_agent_body',
  'interactive-widget': 'components.tipCard.curated.interactive_widget_body',
  'always-on-service': 'components.tipCard.curated.always_on_service_body',
  'interfaces': 'components.tipCard.curated.interfaces_body',
  'approval-modes': 'components.tipCard.curated.approval_modes_body',
  'shortcuts-help': 'components.tipCard.curated.shortcuts_help_body',
} as const satisfies Record<CuratedTipId, string>

/** One entry per curated tip that carries an action button. */
const CURATED_TIP_ACTION_KEY = {
  'split-view': 'components.tipCard.curated.split_view_action',
  'command-palette': 'components.tipCard.curated.command_palette_action',
  'focus-mode': 'components.tipCard.curated.focus_mode_action',
  'interface-cli-mode': 'components.tipCard.curated.interface_cli_mode_action',
  'warm-pool': 'components.tipCard.curated.warm_pool_action',
  'mcp-gateway': 'components.tipCard.curated.mcp_gateway_action',
  'dev-fleet': 'components.tipCard.curated.dev_fleet_action',
  'app-store': 'components.tipCard.curated.app_store_action',
  'system-monitor': 'components.tipCard.curated.system_monitor_action',
  'web-terminal': 'components.tipCard.curated.web_terminal_action',
  'artifacts-library': 'components.tipCard.curated.artifacts_library_action',
  'language-picker': 'components.tipCard.curated.language_picker_action',
  'auto-skills': 'components.tipCard.curated.auto_skills_action',
  'role-models': 'components.tipCard.curated.role_models_action',
  'agent-browsing': 'components.tipCard.curated.agent_browsing_action',
  'computer-use': 'components.tipCard.curated.computer_use_action',
  'remote-crew': 'components.tipCard.curated.remote_crew_action',
  'secrets-vault': 'components.tipCard.curated.secrets_vault_action',
  'schedule-repeated-work': 'components.tipCard.curated.schedule_repeated_work_action',
  'reusable-workflow': 'components.tipCard.curated.reusable_workflow_action',
  'add-mcp-server': 'components.tipCard.curated.add_mcp_server_action',
  'project-steering': 'components.tipCard.curated.project_steering_action',
  'knowledge-source': 'components.tipCard.curated.knowledge_source_action',
  'hooks-automation': 'components.tipCard.curated.hooks_automation_action',
  'custom-agent': 'components.tipCard.curated.custom_agent_action',
  'shortcuts-help': 'components.tipCard.curated.shortcuts_help_action',
} as const satisfies Partial<Record<CuratedTipId, string>>

type CuratedActionId = keyof typeof CURATED_TIP_ACTION_KEY

interface TipText {
  id: string
  title: string
  body: string
  action?: { label: string } | null
}

/** A curated tip with its title, body and action label in the dashboard's
 *  language, but only while the server sends exactly the English the catalog
 *  holds. An edition's tip pool or a generated tip can reuse a curated id with
 *  text of its own; that tip, and any other, renders as served. */
export function localizedTip<T extends TipText>(tip: T): T {
  if (!Object.hasOwn(CURATED_TIP_TITLE_KEY, tip.id)) return tip
  const id = tip.id as CuratedTipId
  const actionId = id as CuratedActionId
  const shipped = tip.title === i18nT(CURATED_TIP_TITLE_KEY[id], { lng: 'en' })
    && tip.body === i18nT(CURATED_TIP_BODY_KEY[id], { lng: 'en' })
    && (!tip.action || (Object.hasOwn(CURATED_TIP_ACTION_KEY, id)
      && tip.action.label === i18nT(CURATED_TIP_ACTION_KEY[actionId], { lng: 'en' })))
  if (!shipped) return tip
  return {
    ...tip,
    title: i18nT(CURATED_TIP_TITLE_KEY[id]),
    body: i18nT(CURATED_TIP_BODY_KEY[id]),
    action: tip.action && { ...tip.action, label: i18nT(CURATED_TIP_ACTION_KEY[actionId]) },
  }
}
