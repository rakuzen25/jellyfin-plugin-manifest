# rakuzen25's Jellyfin plugins

Add this repository URL in Jellyfin **Dashboard → Plugins → Repositories**:

```text
https://raw.githubusercontent.com/rakuzen25/jellyfin-plugin-manifest/main/manifest.json
```

The prepared catalog initially includes verified LyricBridge v0.1.3. Each plugin keeps its own source repository and release archives. An empty manifest means onboarding is waiting for authenticated release evidence.

Stable publication triggers a verified, automatically merged catalog PR; daily reconciliation catches missed notifications. Older catalogued versions are retained. Changes to existing artifacts fail verification, and withdrawals require an explicit owner change.

See [operating instructions](OPERATIONS.md) for App setup, required checks, retries, withdrawals and onboarding. Run `python3 tests/CatalogPublication.py` locally; live verification additionally requires `gh` and `GH_TOKEN` with public source read access. Run `python3 scripts/catalog_sync.py validate` to verify the current catalog without publishing anything.

Owners can use **Actions → Withdraw or restore version → Run workflow** on `main` with a plugin repository, exact four-component version and reason. Changes go through an App-owned, validated auto-merge PR. See [operations](OPERATIONS.md#withdraw-or-restore-a-version-from-actions) for pending-request and recovery behavior.
