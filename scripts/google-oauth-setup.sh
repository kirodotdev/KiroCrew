#!/usr/bin/env bash
# google-oauth-setup.sh -- set up ONE Google Cloud project for every Kiro Crew
# Google card (Gmail, Drive, Calendar, Sheets, Docs, Slides), for private use.
#
# What it does, with gcloud:
#   1. picks or creates the project (argument, else the project named "Kiro Crew",
#      else a new "kiro-crew-XXXXXX"),
#   2. enables every API and MCP API the six Google MCP servers need,
#   3. prints the Project number and the three console links Google offers no
#      API for (Developer Preview form, consent screen, OAuth client).
#
# Safe to run again: enabling an enabled API is a no-op, and the project is found
# by name. Nothing is written to disk; the client secret never passes through here.
# Guide: docs/guides/oauth-app-registration/google.md
#
# Usage: scripts/google-oauth-setup.sh [PROJECT_ID]
set -euo pipefail

# Keep in step with src/kiro_crew/connections/registry.json (a test checks it).
APIS="gmail.googleapis.com drive.googleapis.com calendar-json.googleapis.com sheets.googleapis.com docs.googleapis.com slides.googleapis.com"
MCP_APIS="gmailmcp.googleapis.com drivemcp.googleapis.com calendarmcp.googleapis.com sheetsmcp.googleapis.com docsmcp.googleapis.com slidesmcp.googleapis.com"
REDIRECT_URIS="http://127.0.0.1:48103/callback http://127.0.0.1:48104/callback http://127.0.0.1:48105/callback http://127.0.0.1:48109/callback http://127.0.0.1:48110/callback http://127.0.0.1:48111/callback"
SCOPES="https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/calendar.calendarlist.readonly,https://www.googleapis.com/auth/calendar.events.readonly,https://www.googleapis.com/auth/calendar.events.freebusy,https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/documents.readonly,https://www.googleapis.com/auth/presentations.readonly"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is not installed. Install it from https://cloud.google.com/sdk/docs/install and run this again." >&2
  exit 1
fi

account=$(gcloud config get-value account 2>/dev/null || true)
if [ -z "$account" ]; then
  gcloud auth login
  account=$(gcloud config get-value account 2>/dev/null)
fi

project=${1:-}
if [ -z "$project" ]; then
  project=$(gcloud projects list --filter='name="Kiro Crew"' --format='value(projectId)' --limit=1)
fi
if [ -z "$project" ]; then
  project="kiro-crew-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
  echo "Creating project $project ..."
  gcloud projects create "$project" --name="Kiro Crew"
elif ! gcloud projects describe "$project" >/dev/null 2>&1; then
  echo "Creating project $project ..."
  gcloud projects create "$project" --name="Kiro Crew"
fi
echo "Using project $project (signed in as $account)."

# shellcheck disable=SC2086 -- deliberate word split of the API lists.
gcloud services enable $APIS --project="$project"
mcp_note=""
# shellcheck disable=SC2086
if ! gcloud services enable $MCP_APIS --project="$project"; then
  mcp_note="  The MCP APIs did not enable yet. Run this script again after Google accepts step 1."
fi

number=$(gcloud projects describe "$project" --format='value(projectNumber)')
parent=$(gcloud projects describe "$project" --format='value(parent.type)')
console="https://console.cloud.google.com"

echo
echo "Done. Project ID: $project   Project number: $number"
if [ -n "$mcp_note" ]; then echo "$mcp_note"; fi
echo
echo "Three clicks left (Google has no API for these):"
echo
echo "1. Join the Developer Preview (once per project; Google answers in a few days):"
echo "   https://developers.google.com/workspace/preview"
echo "   Paste the Project number: $number"
echo
echo "2. Consent screen: $console/auth/branding?project=$project"
echo "   Click Get Started. App name: Kiro Crew. Pick your own email."
# Any parent (an organization, or a folder inside one) means a Workspace org.
if [ -n "$parent" ]; then
  echo "   Audience: Internal."
else
  echo "   Audience: External (Internal needs a Workspace org). Then add yourself as a test user:"
  echo "   $console/auth/audience?project=$project  -> Test users -> Add users -> $account"
fi
echo "   Then Data Access -> Add or Remove Scopes -> Manually add scopes, paste:"
echo "   $console/auth/scopes?project=$project"
echo "   $SCOPES"
echo
echo "3. OAuth client: $console/auth/clients/create?project=$project"
echo "   Type: Web application. Tick 'Use this client for an AI-powered agent'."
echo "   Authorized redirect URIs (one per Google card):"
for uri in $REDIRECT_URIS; do echo "     $uri"; done
echo "   Click Create and copy the Client ID and Client secret."
echo
echo "Last: Kiro Crew -> Settings -> OAuth Apps -> any Google card. Paste both once and Save;"
echo "'Also save on ...' (on by default) fills the other five."
