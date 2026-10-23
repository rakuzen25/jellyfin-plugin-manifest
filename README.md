# rakuzen25's Jellyfin plugins

Add this repository URL in Jellyfin **Dashboard → Plugins → Repositories**:

```text
https://raw.githubusercontent.com/rakuzen25/jellyfin-plugin-manifest/main/manifest.json
```

Each plugin keeps its own source repository and release archives. Versions enter the catalog after authenticated release verification. An empty manifest means no versions are currently available, including when all catalogued versions have been withdrawn.

Daily reconciliation catches missed release notifications and retains older catalogued versions. Changes to existing artifacts fail verification, and withdrawals require an explicit owner decision.

Maintainers: see [operating instructions](OPERATIONS.md) for setup and onboarding, [local checks](OPERATIONS.md#test-the-catalog), or [withdrawal and restoration](OPERATIONS.md#withdraw-or-restore-a-version-from-actions).

Run `pnpm check` to verify formatting and linting, and `pnpm format` to apply formatting. Install the pinned Python check requirements and run `pnpm install --frozen-lockfile` first. Checks do not restore dependencies. CI runs the same check command.
