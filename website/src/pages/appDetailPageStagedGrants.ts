/**
 * What an app's approved-grants set holds back, as the App Store detail page shows it.
 *
 * An update that widens a declared grant waits for the owner's approval on the
 * app's page: each family's entries outside the approved set are "staged". Pure,
 * and never imports the page.
 */
import type { AppInfo } from './appDetailPageTypes'
import { knownDesktopCapabilities } from '../components/appstore/desktopCapabilities'
import { knownBrowserVerbs } from '../components/appstore/browserVerbs'

import { MEMORY_ADMIN_API_PREFIX, disclosedApi } from '../components/appstore/trustGrants'

export function stagedGrantsOf(app: AppInfo | null, memoryProvidersActive = true) {
  // Declared api/events entries outside the approved set (`approvedGrants`) are
  // held back: the app runs without them until the owner approves them here.
  // `memoryAdmin` is granted as the one API prefix it opens (`apps/point_grants.py`),
  // so an update that adds it waits here like any declared `api` entry. While the
  // memory provider preview is off the gateway grants no such prefix, so it never waits.
  const declaredApi = (disclosedApi(app?.manifest?.permissions) ?? []).filter(
    p => memoryProvidersActive || p !== MEMORY_ADMIN_API_PREFIX,
  )
  const stagedApi = app?.approvedGrants
    ? declaredApi.filter(p => !(app.approvedGrants?.api || []).includes(p))
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
  // Framed sites (`ui.frameSources`) an update or an edit added wait the same way.
  const stagedFrameSources = app?.frameSourcesGrant?.staged || []
  // Pages the app's own agent may browse unasked: an install never approves them.
  const stagedBrowserSites = app?.browserSitesGrant?.staged || []
  // Kiro operations an install did not show, or an update added.
  const stagedKiroOperations = app?.kiroOperationsGrant?.staged || []
  // `permissions.browser` verbs: an install never approves them either.
  const stagedBrowser = knownBrowserVerbs(app?.browserGrant?.staged)
  const hasStagedGrants = stagedApi.length + stagedEvents.length + stagedLinkHosts.length + stagedLinkPatterns.length + stagedMessageActions.length + stagedOpenHosts.length + stagedComposerActions.length + stagedSelection.length + stagedUserSessionReach.length + stagedDesktopCapabilities.length + stagedFrameSources.length + stagedBrowserSites.length + stagedKiroOperations.length + stagedBrowser.length > 0 || stagedFullUrl || stagedOpenFullUrl || stagedWorkerAutoApprove
  // Only Kiro operations wait, and none was ever approved: this is the first approval
  // (an install approves none), not an update asking for more.
  const firstKiroApproval = stagedKiroOperations.length > 0 && !(app?.kiroOperationsGrant?.approved || []).length
    && stagedKiroOperations.length === stagedApi.length + stagedEvents.length + stagedLinkHosts.length + stagedLinkPatterns.length + stagedMessageActions.length + stagedOpenHosts.length + stagedComposerActions.length + stagedSelection.length + stagedUserSessionReach.length + stagedDesktopCapabilities.length + stagedFrameSources.length + stagedBrowserSites.length + stagedKiroOperations.length + stagedBrowser.length
    && !stagedFullUrl && !stagedOpenFullUrl && !stagedWorkerAutoApprove
  return {
    stagedApi, stagedEvents, stagedLinkHosts, stagedLinkPatterns, stagedFullUrl,
    stagedMessageActions, declaredMessageActions, stagedOpenHosts, stagedOpenFullUrl,
    stagedComposerActions, stagedSelection, declaredSelectionActions, stagedWorkerAutoApprove,
    stagedUserSessionReach, stagedDesktopCapabilities, stagedFrameSources, stagedBrowserSites, stagedKiroOperations, stagedBrowser, hasStagedGrants, firstKiroApproval,
  }
}
