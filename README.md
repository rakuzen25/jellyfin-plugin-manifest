# rakuzen25's Jellyfin plugins

Add this repository URL in Jellyfin **Dashboard → Plugins → Repositories**:

```text
https://raw.githubusercontent.com/rakuzen25/jellyfin-plugin-manifest/main/manifest.json
```

Each plugin keeps its own source repository and release archives. Versions enter the catalog after authenticated release verification. An empty manifest means no versions are currently available, including when all catalogued versions have been withdrawn.

Daily reconciliation catches missed release notifications and retains older catalogued versions. Changes to existing artifacts fail verification, and withdrawals require an explicit owner decision.

Maintainers: see [operating instructions](OPERATIONS.md) for setup and onboarding, [local checks](OPERATIONS.md#test-the-catalog), or [withdrawal and restoration](OPERATIONS.md#withdraw-or-restore-a-version-from-actions).

Install the pinned Python and JavaScript tools with `uv sync --locked` and `pnpm install --frozen-lockfile`. Run `pnpm check` for formatting, lint, and strict Python type checking, `pnpm test` for offline tests, and `pnpm format` to apply formatting. See [local checks](OPERATIONS.md#test-the-catalog) for workflow validation and the pinned GitHub verifier.
