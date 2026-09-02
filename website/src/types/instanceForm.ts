/**
 * The remote-crew form's value shape, and an unsaved edit of one crew.
 *
 * Neutral module rather than the form's own file: the store holds these while the
 * route that renders the form is unmounted, and a store importing a page module
 * inverts the dependency — harmless while the import is type-only, but the
 * inversion is what makes it easy to grow a runtime one later.
 */
export interface InstanceFormValues {
  name: string
  method: 'ssh' | 'ssm'
  sshHost: string
  ssmTarget: string
  awsProfile: string
  awsRegion: string
  ssmRunAs: string
  remotePort: string
  ttl: string
  remoteBin: string
}

/**
 * Unsaved edit values plus the server record they were typed against.
 *
 * The two travel together because a draft is only meaningful relative to its
 * baseline — rebasing it onto a newer poll is how an "adopt the current record"
 * choice is expressed, and diffing against anything else would read someone
 * else's concurrent change as the user's own edit.
 */
export interface InstanceDraft {
  values: InstanceFormValues
  baseline: import('../api/client').InstanceView
}
