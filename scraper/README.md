# Public Cloud Web Fetcher

This directory wraps the open-source [Scrapling](https://github.com/D4Vinci/Scrapling) project for GitHub Actions.

## Dependency

```text
scrapling[rag]==0.4.15
```

Scrapling is used as a pinned dependency rather than vendoring its entire source tree. This keeps upstream security fixes and upgrades manageable while preserving a reproducible version.

## Fetch ladder

```text
Scrapling Fetcher (HTTP)
    ↓ insufficient
DynamicFetcher (Chromium / JavaScript)
    ↓ insufficient
StealthyFetcher
    ↓
result artifact
```

Browser fetches also capture XHR/fetch responses so dynamically delivered article text can be recovered when it is absent from the initial DOM.

## Output

Each run uploads `out/` as an Actions Artifact with a **1 day retention period**.

The artifact contains:

- `manifest.json`
- `manifest.md`
- one directory per URL
  - `content.md`
  - `result.json`

No fetched body is committed to the public repository.
