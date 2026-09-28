// Fixtures for semgrep/supply-chain.yaml (the one TS rule), exercised by
// `semgrep --test` in the SAST job. `ruleid:` asserts the NEXT line MUST
// match; `ok:` asserts it must NOT. Test mode ignores the rule's `paths:`
// filters, so this file does not need to live under website/src/.

declare const remoteUrl: string;

export function externalScriptInject(): void {
  const script = document.createElement("script");
  // Dynamic script src assignment: externally controlled URLs enable
  // supply-chain code injection.
  // ruleid: kirocrew.frontend-external-script-inject
  script.src = remoteUrl;
  document.head.appendChild(script);
}

export function plainLink(): void {
  const link = document.createElement("a");
  // A href assignment is navigation, not script execution.
  // ok: kirocrew.frontend-external-script-inject
  link.href = remoteUrl;
  document.body.appendChild(link);
}
