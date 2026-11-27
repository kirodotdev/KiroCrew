/**
 * The presentational pieces a coding-agent picker is built from, shared by
 * first-run setup (`KiroPrerequisiteGate`'s "Use other coding agents") and
 * Settings > Agent Harness (`AgentBackendTab`), so the two screens that choose
 * a harness look and behave the same. Data, queries and the config write stay
 * with each screen; nothing here reads a query or writes config.
 */
export { AgentPickerRow } from './AgentPickerRow'
export { AgentStatusBadge } from './AgentStatusBadge'
export { AgentDetailActions, AgentInstallDetail } from './AgentDetail'
export { AgentNotReadyDetail, WorkloadPodSandboxHint, type AgentNotReadyReason } from './AgentNotReadyDetail'
export { CopyCommand } from './CopyCommand'
export { HarnessPickerList, type HarnessRowParts } from './HarnessPickerList'
export { harnessList } from './harnessList'
export { HarnessInstallDetail, KIRO_CLI_INSTALL_DOCS_URL } from './HarnessInstallDetail'
export { KiroInstallCommands } from './KiroInstallCommands'
