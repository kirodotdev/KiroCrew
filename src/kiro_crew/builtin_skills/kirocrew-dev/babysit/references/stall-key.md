# Stall progress key

Read from the babysit skill's "Stall tripwire" section.

For GitHub the optional helper emits these key fields; otherwise derive equivalent
fields from the host verdict ("Read state from the host"). GitLab and Bitbucket need their own equivalent
key before applying this rule:

| Field | Meaning |
|---|---|
| `head_sha` | Exact revision |
| `failing_checks` | Sorted workflow-qualified identities, not just names |
| `checks_failing` | Failure count |
| `readiness_kind` | Running, failing or unpublished |
| `exit_code` | Verdict |
| `status` | Verdict reason, so changed blockers count as progress |
