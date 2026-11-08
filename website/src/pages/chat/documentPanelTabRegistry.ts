/**
 * Document tab registry: tabs a downstream edition shows beside a file or
 * artifact opened in the chat side panel.
 *
 * App panel tabs (`contributes.panelTabs`, `hooks/panelTabRegistry.ts`) are
 * free-standing side-panel tabs and do not know which document is open. An
 * edition that has something to say about THE document on screen (where it is
 * published, its versions) registers a tab here from its entry module, instead
 * of editing the file and artifact bodies on every upstream sync.
 *
 * Each entry is a self-contained component, like an `overviewStatCards` entry:
 * it owns its own reads and writes through the edition's own routes, and gets
 * the opened document as `target`. `DocumentPanelTabs` places the registered
 * tabs in a strip above the document, each inside its own ErrorBoundary. The
 * core registers none, so the file and artifact bodies are unchanged in the
 * stock build.
 *
 * Registration is expected at module-load time (edition composition), before
 * the panel renders: this registry is not reactive.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from '../../apps/seamCollision'

/** The document a tab sits beside. */
export type DocumentPanelTarget =
  | { kind: 'file'; path: string }
  | { kind: 'artifact'; slug: string; artifactKind: string }

export interface DocumentPanelTabProps {
  target: DocumentPanelTarget
  /** Whether the side-panel tab holding this document is the visible one. */
  active: boolean
  /** The chat slot that owns the side panel. */
  slot: string
}

export interface DocumentPanelTab {
  /** Stable key for React reconciliation, de-dup and the ErrorBoundary scope. */
  id: string
  /**
   * The tab's label, read at render time so it follows the language the user
   * picked after registration (a string fixed at module load would not).
   */
  label: () => string
  /**
   * Show the tab only while this installed app is enabled. The same rule app
   * contributions follow: the app's enable state is the opt-in. Omit to always
   * show it.
   */
  requiresApp?: string
  /** The tab body. Rendered only while its tab is selected. */
  component: ComponentType<DocumentPanelTabProps>
}

const TABS: DocumentPanelTab[] = []

/**
 * Register one or more document tabs. A duplicate `id` is a collision
 * (`reportSeamCollision`) and is ignored.
 */
export function registerDocumentPanelTabs(tabs: DocumentPanelTab[]): void {
  for (const t of tabs) {
    if (TABS.some(existing => existing.id === t.id)) {
      reportSeamCollision('documentPanelTabs', `document tab ${t.id} already registered; ignoring duplicate`)
      continue
    }
    TABS.push(t)
  }
}

/** All registered document tabs, in registration order. */
export function getDocumentPanelTabs(): readonly DocumentPanelTab[] {
  return TABS
}

/** Test-only: forget every registration. */
export function resetDocumentPanelTabsForTests(): void {
  TABS.length = 0
}
