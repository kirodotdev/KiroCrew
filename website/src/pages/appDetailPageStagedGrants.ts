/**
 * What an app's approved-grants set holds back, as the App Store detail page shows it.
 *
 * An update that widens a declared grant waits for the owner's approval on the
 * app's page: each family's entries outside the approved set are "staged". Pure,
 * and never imports the page.
 */
import type { AppInfo } from './appDetailPageTypes'
import { knownDesktopCapabilities } from '../components/appstore/desktopCapabilities'

export function stagedGrantsOf(app: AppInfo | null) {
  // Declared api/events entries outside the approved set (`approvedGrants`) are
  // held back: the app runs without them until the owner approves them here.
  const stagedApi = app?.approvedGrants
    ? (app.manifest?.permissions?.api || []).filter(p => !(app.approvedGrants?.api || []).includes(p))
    : []
  const stagedEvents = app?.approvedGrants
    ? (app.manifest?.permissions?.events || []).filter(e => !(app.approvedGrants?.events || []).includes(e))
    : []
  // Link-preview hosts and text patterns an update widened wait the same way.
  const stagedLinkHosts = app?.linkPreviewGrant?.stagedHosts || []
  const stagedLinkPatterns = app?.linkPreviewGrant?.stagedTextPatterns || []
  const stagedFullUrl = app?.linkPreviewGrant?.stagedFullUrl === true
  // Message-action reach an update widened (the user role, markdown, a new action).
  const stagedMessageActions = app?.messageActionGrant?.staged || []
  const declaredMessageActions = (app?.manifest as { contributes?: { messageActions?: unknown } } | undefined)?.contributes?.messageActions
  // So do link-open hosts and whole URLs for opened links.
  const stagedOpenHosts = app?.linkOpenGrant?.stagedHosts || []
  const stagedOpenFullUrl = app?.linkOpenGrant?.stagedFullUrl === true
  // A composer button an update added waits the same way.
  const stagedComposerActions = app?.composerActionGrant?.staged || []
  // Selection actions an update added or changed (a new source, more text, a new effect).
  const stagedSelection = app?.selectionActionGrant?.staged || []
  const declaredSelectionActions = (app?.manifest as { contributes?: { selectionActions?: unknown } } | undefined)?.contributes?.selectionActions
  // An update that added `permissions.workerAutoApprove` waits the same way.
  const stagedWorkerAutoApprove = app?.workerAutoApproveGrant?.staged === true
  const stagedUserSessionReach = app?.userSessionReachGrant?.staged || []
  const stagedDesktopCapabilities = knownDesktopCapabilities(app?.desktopCapabilitiesGrant?.staged)
  const hasStagedGrants = stagedApi.length + stagedEvents.length + stagedLinkHosts.length + stagedLinkPatterns.length + stagedMessageActions.length + stagedOpenHosts.length + stagedComposerActions.length + stagedSelection.length + stagedUserSessionReach.length + stagedDesktopCapabilities.length > 0 || stagedFullUrl || stagedOpenFullUrl || stagedWorkerAutoApprove
  return {
    stagedApi, stagedEvents, stagedLinkHosts, stagedLinkPatterns, stagedFullUrl,
    stagedMessageActions, declaredMessageActions, stagedOpenHosts, stagedOpenFullUrl,
    stagedComposerActions, stagedSelection, declaredSelectionActions, stagedWorkerAutoApprove,
    stagedUserSessionReach, stagedDesktopCapabilities, hasStagedGrants,
  }
}
