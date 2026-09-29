# AGENTS.md

This public repository is an execution/staging repository only.

## Rules

1. Final knowledge must never be stored here.
2. Only public URLs may be committed under `requests/`.
3. Never commit cookies, tokens, credentials, private/internal URLs, customer data, or authenticated content.
4. Fetched page bodies must be kept in short-lived GitHub Actions Artifacts, not Git history.
5. After an Agent has ingested a successful Artifact into `sowar1987/Knowledge-Base`, it must:
   - delete the corresponding request JSON from the current branch;
   - trigger Artifact cleanup using `cleanup/*.json`.
6. Failed or truncated fetches must not be promoted into the private knowledge base as verified content.
7. The canonical knowledge base is always `sowar1987/Knowledge-Base`.
