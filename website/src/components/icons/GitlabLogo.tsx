import gitlabLogoUrl from './gitlab-logo.svg'

/** Official GitLab tanuki mark, rendered as a theme-aware CSS mask. */
export default function GitlabLogo({ size = 13, className = '' }: { size?: number; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block ${className}`}
      data-provider-mark="gitlab"
      style={{
        width: size,
        height: size,
        backgroundColor: 'currentColor',
        maskImage: `url("${gitlabLogoUrl}")`,
        maskRepeat: 'no-repeat',
        maskSize: 'contain',
        maskPosition: 'center',
        WebkitMaskImage: `url("${gitlabLogoUrl}")`,
        WebkitMaskRepeat: 'no-repeat',
        WebkitMaskSize: 'contain',
        WebkitMaskPosition: 'center',
      }}
    />
  )
}
