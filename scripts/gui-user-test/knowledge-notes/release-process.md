# Release process

How a change travels from a merged pull request to users.

1. Every merge to the main branch builds a release candidate automatically.
2. The candidate soaks on the staging environment for one working day.
3. The release captain reads the staging dashboards and the open incident list.
4. If nothing is red, the captain promotes the candidate on Tuesday or Thursday.
5. The release note is posted in the announcements channel the same afternoon.

## Rolling back

A rollback is a promotion of the previous candidate, using the same button.
Roll back first and investigate second; a broken release is never debugged
in production.

## Who is the release captain

The role rotates weekly and follows the on-call schedule. The captain for the
week is named in the channel topic.
