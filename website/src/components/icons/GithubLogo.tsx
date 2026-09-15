import githubLogoUrl from './github-logo.svg'

/** Official GitHub Invertocat mark, rendered as a theme-aware CSS mask. */
export default function GithubLogo({ size = 13, className = '' }: { size?: number; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block ${className}`}
      data-provider-mark="github"
      style={{
        width: size,
        height: size,
        backgroundColor: 'currentColor',
        maskImage: `url("${githubLogoUrl}")`,
        maskRepeat: 'no-repeat',
        maskSize: 'contain',
        maskPosition: 'center',
        WebkitMaskImage: `url("${githubLogoUrl}")`,
        WebkitMaskRepeat: 'no-repeat',
        WebkitMaskSize: 'contain',
        WebkitMaskPosition: 'center',
      }}
    />
  )
}
