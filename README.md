# Knowledge-Base-Public

Public fallback runner and temporary staging area for the private `sowar1987/Knowledge-Base` repository.

## Purpose

This repository is **not** the canonical knowledge base. It exists to provide a free GitHub Actions execution path for fetching **publicly accessible web pages** when private-repository Actions minutes are unavailable or should be conserved.

### Data flow

```text
Public URL
   ↓
requests/*.json
   ↓
GitHub Actions (public standard runner)
   ↓
Scrapling HTTP / Dynamic / Stealth fetch
   ↓
1-day Actions Artifact
   ↓
ChatGPT retrieves artifact
   ↓
Private Knowledge-Base
   ↓
Structured knowledge / index / tree
```

## Privacy boundary

Everything committed to this repository is public and remains visible in Git history.

**Allowed**
- Public URLs
- Open-source scraping code
- Temporary request descriptors containing public URLs

**Never put here**
- Private/internal URLs
- Cookies, tokens, passwords, API keys
- Customer/internal documents
- Private knowledge notes
- Authenticated page contents

Fetched page bodies are uploaded as short-lived GitHub Actions artifacts instead of being committed to this repository.

## Canonical repository

Final knowledge remains in the private repository:

`sowar1987/Knowledge-Base`
