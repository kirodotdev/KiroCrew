import ErrorBoundary from '../../components/ErrorBoundary'
import { getFeaturePreviewRows } from './featurePreviewRows'

/**
 * Settings > Developer > Feature Previews: the rows an edition registered with
 * `registerFeaturePreviewRows`. Each row is the edition's own component, so its
 * card, switch, read and write are the edition's; this only places them, each
 * isolated so a throwing row shows the standard crash card (with Try again)
 * in its place instead of taking the section down. Renders nothing in the
 * stock build.
 */
export function EditionPreviewRows() {
  return (
    <>
      {getFeaturePreviewRows().map(({ id, component: Row }) => (
        <ErrorBoundary key={id} scope={`feature-preview-row-${id}`}>
          <Row />
        </ErrorBoundary>
      ))}
    </>
  )
}
