# Artifact Cleanup Queue

After a fetched Artifact has been successfully transferred into the private knowledge base, create a temporary JSON file here:

```json
{
  "artifact_ids": [123456789],
  "reason": "ingested-to-private"
}
```

The cleanup workflow deletes those GitHub Actions Artifacts and removes the cleanup request file from the current branch.

Do not put page content in this directory.
