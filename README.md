# Compound Intelligence

Tooling for the Compound Intelligence channels.

## Contents

| Path | Purpose |
| --- | --- |
| [`channel-launch/api/ci-publisher`](channel-launch/api/ci-publisher) | CLI for programmatic publishing (YouTube Data API v3) |

## Credentials

No credential is ever committed. `client_secret*.json`, `.env` and
`token*.json` are gitignored at the repository root. OAuth tokens live outside
the tree in `~/.ci-publisher/` (override with `CI_PUBLISHER_HOME`).

If a secret does reach a commit, a transcript or a chat, rotate it rather than
deleting the message: reset it in Google Cloud Console under
**APIs & Services → Credentials**, then re-run `auth`.
