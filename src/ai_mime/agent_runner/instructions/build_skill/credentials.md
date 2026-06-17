# Credentials — user secrets (API keys, tokens, account emails)

Read this only when the task needs **user-specific secrets** to run: API keys,
tokens, account emails/usernames, workspace domains, etc. (e.g. Jira needs an
account email, an API token, and a site domain). Do NOT use this for ordinary
task inputs — those stay in `inputs/`.

Never hardcode a secret in `scripts/run.py`, and never write a real secret into
any file that ships with the skill (`credentials.template.json`,
`inputs/inputs.example.json`, references, etc.).

## What to create

1. **Manifest** — `{skill_dir}/credentials.template.json`. Declares which
   service and keys the skill needs, with **placeholder** values only:
   ```json
   {
     "jira": {
       "email": "<FILL IN: Atlassian account email>",
       "api_token": "<FILL IN: Jira API token from id.atlassian.com>",
       "domain": "<FILL IN: your-company.atlassian.net>"
     }
   }
   ```
   Group keys under a short service name. This file ships with the skill and is
   what the installer is prompted to fill in. Every value must stay a
   `<FILL IN: ...>` placeholder — packaging validation rejects real values here.

2. **Build-time values** — `agent/credentials.local.json` (in the workflow's
   `agent/` dir, NOT in the skill). Put the developer's real values here so the
   e2e run works, using the same shape as the manifest:
   ```json
   { "jira": { "email": "me@company.com", "api_token": "ATATT...", "domain": "company.atlassian.net" } }
   ```
   This file never ships (it is stripped on export and auto-merged into the
   user's global credential store when the skill is finalized). Ask the user for
   these values in chat if you don't already have them.

## How `scripts/run.py` reads credentials

Read the file at `$AI_MIME_CREDENTIALS_PATH`, keyed by service — never read the
global store and never read `credentials.template.json` at runtime:
```python
import json, os
creds = json.load(open(os.environ["AI_MIME_CREDENTIALS_PATH"]))
jira = creds["jira"]  # {"email": ..., "api_token": ..., "domain": ...}
```
The app injects `AI_MIME_CREDENTIALS_PATH` automatically (a scoped, read-only
file containing only this skill's declared keys). During the build it points at
`agent/credentials.local.json`; for an installed skill it is projected from the
user's global store. Your script code is identical in both cases.

Document the required credentials in `SKILL.md` (a short `## Credentials`
section listing service + keys) so users know what they'll be asked for.
