#!/usr/bin/env bash
set -euo pipefail

PUBLISH_REPOSITORY_NAME="${1:-genai2agent-relay}"
PUBLISH_VISIBILITY="${2:-public}"
PUBLISH_DESCRIPTION="Protocol-neutral text action transport for tool-call constrained model gateways"
PUBLISH_API_ROOT="https://api.github.com"
PUBLISH_API_VERSION="2026-03-10"
PUBLISH_RESPONSE_FILE=""
PUBLISH_ASKPASS_FILE=""

publish_usage() {
    printf 'Usage: %s [repository-name] [public|private]\n' "${0##*/}"
    printf 'Example: %s genai2agent-relay public\n' "${0##*/}"
}

publish_cleanup() {
    if [[ -n "$PUBLISH_RESPONSE_FILE" ]]; then
        rm -f -- "$PUBLISH_RESPONSE_FILE"
    fi
    if [[ -n "$PUBLISH_ASKPASS_FILE" ]]; then
        rm -f -- "$PUBLISH_ASKPASS_FILE"
    fi
    unset PUBLISH_GITHUB_TOKEN
}
trap publish_cleanup EXIT

if [[ "$PUBLISH_REPOSITORY_NAME" == "-h" || "$PUBLISH_REPOSITORY_NAME" == "--help" ]]; then
    publish_usage
    exit 0
fi
if [[ ! "$PUBLISH_REPOSITORY_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'Invalid repository name: %s\n' "$PUBLISH_REPOSITORY_NAME" >&2
    exit 2
fi
if [[ "$PUBLISH_VISIBILITY" != "public" && "$PUBLISH_VISIBILITY" != "private" ]]; then
    publish_usage >&2
    exit 2
fi

for PUBLISH_COMMAND in git curl python3; do
    if ! command -v "$PUBLISH_COMMAND" >/dev/null 2>&1; then
        printf 'Required command is unavailable: %s\n' "$PUBLISH_COMMAND" >&2
        exit 1
    fi
done

PUBLISH_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$PUBLISH_ROOT" || "$PWD" != "$PUBLISH_ROOT" ]]; then
    printf 'Run this command from the repository root.\n' >&2
    exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
    printf 'The worktree is not clean. Commit or stash changes before publishing.\n' >&2
    exit 1
fi

PUBLISH_GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [[ -z "$PUBLISH_GITHUB_TOKEN" ]]; then
    read -r -s -p 'GitHub token (input hidden): ' PUBLISH_GITHUB_TOKEN
    printf '\n'
fi
if [[ -z "$PUBLISH_GITHUB_TOKEN" ]]; then
    printf 'A GitHub token is required.\n' >&2
    exit 1
fi
if [[ "$PUBLISH_GITHUB_TOKEN" == *$'\n'* || "$PUBLISH_GITHUB_TOKEN" == *'"'* ]]; then
    printf 'The token contains unsupported characters.\n' >&2
    exit 1
fi

PUBLISH_RESPONSE_FILE="$(mktemp)"

publish_api_call() {
    local publish_method="$1"
    local publish_url="$2"
    local publish_output="$3"
    local publish_payload="${4:-}"
    local -a publish_curl_args=(
        --disable
        --config -
        --connect-timeout 15
        --max-time 60
        --request "$publish_method"
        --url "$publish_url"
        --output "$publish_output"
        --write-out '%{http_code}'
    )
    if [[ -n "$publish_payload" ]]; then
        publish_curl_args+=(--data-binary "$publish_payload")
    fi
    {
        printf 'silent\n'
        printf 'show-error\n'
        printf 'header = "Accept: application/vnd.github+json"\n'
        printf 'header = "Authorization: Bearer %s"\n' "$PUBLISH_GITHUB_TOKEN"
        printf 'header = "X-GitHub-Api-Version: %s"\n' "$PUBLISH_API_VERSION"
        printf 'header = "Content-Type: application/json"\n'
    } | curl "${publish_curl_args[@]}"
}

publish_json_field() {
    local publish_field="$1"
    python3 - "$PUBLISH_RESPONSE_FILE" "$publish_field" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
field = value.get(sys.argv[2], "") if isinstance(value, dict) else ""
print(field if isinstance(field, (str, int, float)) else "")
PY
}

PUBLISH_STATUS="$(publish_api_call GET "$PUBLISH_API_ROOT/user" "$PUBLISH_RESPONSE_FILE")"
if [[ "$PUBLISH_STATUS" != "200" ]]; then
    printf 'GitHub authentication failed (HTTP %s): %s\n' \
        "$PUBLISH_STATUS" "$(publish_json_field message)" >&2
    exit 1
fi
PUBLISH_OWNER="$(publish_json_field login)"
if [[ -z "$PUBLISH_OWNER" ]]; then
    printf 'GitHub did not return an authenticated username.\n' >&2
    exit 1
fi

PUBLISH_REPOSITORY_API="$PUBLISH_API_ROOT/repos/$PUBLISH_OWNER/$PUBLISH_REPOSITORY_NAME"
PUBLISH_STATUS="$(publish_api_call GET "$PUBLISH_REPOSITORY_API" "$PUBLISH_RESPONSE_FILE")"
if [[ "$PUBLISH_STATUS" == "404" ]]; then
    PUBLISH_PRIVATE=false
    if [[ "$PUBLISH_VISIBILITY" == "private" ]]; then
        PUBLISH_PRIVATE=true
    fi
    PUBLISH_CREATE_PAYLOAD="$(
        PUBLISH_NAME="$PUBLISH_REPOSITORY_NAME" \
        PUBLISH_DESCRIPTION="$PUBLISH_DESCRIPTION" \
        PUBLISH_PRIVATE="$PUBLISH_PRIVATE" \
        python3 - <<'PY'
import json
import os

print(json.dumps({
    "name": os.environ["PUBLISH_NAME"],
    "description": os.environ["PUBLISH_DESCRIPTION"],
    "private": os.environ["PUBLISH_PRIVATE"] == "true",
    "has_issues": True,
    "has_projects": False,
    "has_wiki": False,
}))
PY
    )"
    PUBLISH_STATUS="$(publish_api_call POST "$PUBLISH_API_ROOT/user/repos" "$PUBLISH_RESPONSE_FILE" "$PUBLISH_CREATE_PAYLOAD")"
    if [[ "$PUBLISH_STATUS" != "201" ]]; then
        printf 'Repository creation failed (HTTP %s): %s\n' \
            "$PUBLISH_STATUS" "$(publish_json_field message)" >&2
        exit 1
    fi
    printf 'Created GitHub repository: %s/%s\n' "$PUBLISH_OWNER" "$PUBLISH_REPOSITORY_NAME"
elif [[ "$PUBLISH_STATUS" == "200" ]]; then
    printf 'GitHub repository already exists: %s/%s\n' "$PUBLISH_OWNER" "$PUBLISH_REPOSITORY_NAME"
else
    printf 'Repository lookup failed (HTTP %s): %s\n' \
        "$PUBLISH_STATUS" "$(publish_json_field message)" >&2
    exit 1
fi

PUBLISH_REMOTE_URL="https://github.com/$PUBLISH_OWNER/$PUBLISH_REPOSITORY_NAME.git"
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$PUBLISH_REMOTE_URL"
else
    git remote add origin "$PUBLISH_REMOTE_URL"
fi

PUBLISH_ASKPASS_FILE="$(mktemp)"
chmod 700 "$PUBLISH_ASKPASS_FILE"
printf '%s\n' \
    '#!/bin/sh' \
    'case "$1" in' \
    '  *Username*) printf "%s\\n" "x-access-token" ;;' \
    '  *) printf "%s\\n" "$PUBLISH_GITHUB_TOKEN" ;;' \
    'esac' >"$PUBLISH_ASKPASS_FILE"

PUBLISH_GITHUB_TOKEN="$PUBLISH_GITHUB_TOKEN" \
GIT_ASKPASS="$PUBLISH_ASKPASS_FILE" \
GIT_TERMINAL_PROMPT=0 \
git push --set-upstream origin main

printf 'Published: https://github.com/%s/%s\n' "$PUBLISH_OWNER" "$PUBLISH_REPOSITORY_NAME"
