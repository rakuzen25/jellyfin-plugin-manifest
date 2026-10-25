# Operate the shared Jellyfin catalog

Use this guide to configure publication, manage versions, and recover failed updates. For the public repository URL and Jellyfin setup, see the [README](README.md).

## Work on the catalog

Clone the repository, then select the Python version in [`.python-version`](.python-version) before running scripts or tests:

```sh
git clone https://github.com/rakuzen25/jellyfin-plugin-manifest.git
cd jellyfin-plugin-manifest
```

Submit code, policy, and tooling changes through reviewed pull requests. Run the [local checks](#test-the-catalog) before submitting.

Shared modules keep validation independent of publication:

| Module                                  | Responsibility                                          |
| --------------------------------------- | ------------------------------------------------------- |
| `scripts/catalog.py`                    | Validate and merge catalog entries.                     |
| `scripts/catalog_core.py`               | Verify releases and reconcile history.                  |
| `scripts/catalog_maintenance_policy.py` | Validate maintenance changes against protected history. |

The sync and maintenance entry points import these modules without importing one another.

## Create one shared GitHub App

Create an App for the catalog and its allowlisted plugin repositories:

1. Open **Settings → Developer settings → GitHub Apps → New GitHub App**. Choose an available name, such as `rakuzen25-plugin-publisher`.
2. Set the homepage to this repository, disable webhooks, and limit installation to your account. No callback URL or user authorization is needed.
3. Grant repository **Contents: Read and write**, **Pull requests: Read and write**, **Issues: Read and write**, **Actions: Read-only**, and **Attestations: Read-only**. Metadata read is automatic. The catalog does not request workflow-writing permission. If this same App also synchronizes plugin toolkit bindings, grant Workflows: Read and write to that installation; catalog jobs continue to request only their listed scopes. Grant no organization or administration permissions.
4. Install the App using **Only select repositories**: `jellyfin-plugin-lyricbridge`, `jellyfin-plugin-music-metadata-manager`, `jellyfin-plugin-toolkit`, and `jellyfin-plugin-manifest`.
5. Generate and securely store a private key. Add Actions variable `PLUGIN_APP_CLIENT_ID` with the App's Client ID and secret `PLUGIN_APP_PRIVATE_KEY` with the complete PEM key.

Never commit the key or paste it into logs or issues. It can mint permissions on all installed repositories; keep installation narrow and rotate it when needed.

Workflows use the toolkit's native scoped App-token action and revoke tokens at job completion. Token acquisition failures retain the original GitHub error and add guidance for credentials, installation, and requested permissions. App setup cannot repair those settings. Catalog updates request write access only on this repository. Source inspection uses a separate read-only token scoped to allowlisted plugins. Pull request checks use the read-only workflow token without the App key.

Plugin publishers create releases and sign attestations; the catalog reads them.

## Protect the catalog and enable automatic additions

Configure these settings before enabling publication:

1. Run **Validate catalog** and confirm that `catalog-validate` passes.
2. Under **Settings → General → Pull Requests**, enable **Allow auto-merge** and squash merging, with squash commit subjects taken from the PR title.
3. Under **Settings → Rules → Rulesets**, protect `main`: require a pull request, `catalog-validate` and `title / pr-title` from GitHub Actions, and an up-to-date branch. Require no approving reviews for automated additions and owner-requested maintenance. Block force pushes and branch deletion; add no App bypass.
4. Under **Actions → General**, permit the reviewed SHA-pinned actions used here. Keep default workflow permissions read-only.
5. Run **Reconcile plugin catalog** manually. A current catalog produces no change; a qualifying release creates an App-owned pull request requesting squash auto-merge.

The automated paths use separate branches and the same required check:

| Path                                      | Branch                           | Files changed                                 |
| ----------------------------------------- | -------------------------------- | --------------------------------------------- |
| Verified release additions                | `automation/catalog-update`      | `history.json`, generated `manifest.json`     |
| Owner-requested withdrawal or restoration | `automation/catalog-maintenance` | `withdrawals.json`, generated `manifest.json` |

Both paths request auto-merge after checking the proposal. Ordinary code, policy, tooling, and manual withdrawal edits require reviewed pull requests; the updater doesn't enable auto-merge for them.

Both workflows share the non-cancelling `catalog-update` queue with `queue: max`. Execution order determines pending maintenance state; retry requests that exceed queue capacity. Duplicate release notifications are harmless.

## Verify releases before publication

Configure allowed repositories, immutable plugin GUIDs, asset prefixes, and evidence requirements in [`plugins.json`](plugins.json). Initial onboarding imports only the latest stable release. Later reconciliation retains verified versions and discovers missed releases; drafts and prereleases are excluded.

Scheduled reconciliation runs at **06:23 UTC** daily. Publishers can send a `plugin-published` repository dispatch; its payload is only a hint. Manual refresh uses the same authenticated release and tag APIs.

Each release must provide the plugin and source ZIPs, `manifest.json`, and `SHA256SUMS`. A web ZIP is required only when the repository policy declares `web: true`. A durable `validation.json` receipt must hash the exact policy-required assets; exclude it from its own inventory and from `SHA256SUMS`.

Verification checks:

- `gh attestation verify` against the approved signer workflow, a `main` source ref, and hosted runners.
- Authenticated successful attachment and aggregate validation jobs.
- Exact public asset hashes, release identity, plugin GUID, and version.

Missing assets, API failures, moved tags, and changed checksums stop publication and preserve the served manifest. Reconciliation opens or updates a deduplicated integrity issue and closes its issues after full recovery.

Only schema 7 public receipts with current toolkit provenance are accepted. Missing public receipts, historical receipt schemas, Actions artifact evidence, and old field aliases are rejected. Existing upstream release assets are never rewritten.

## Withdraw or restore a version from Actions

To propose an owner decision, open **Actions → Withdraw or restore version → Run workflow** in this repository:

1. Choose branch **main**.
2. Choose **Operation**: `Withdraw` or `Restore`.
3. Enter the allowlisted **Repository**, currently `rakuzen25/jellyfin-plugin-lyricbridge`.
4. Enter the exact four-component **Version**, such as `0.1.3.0`. Leading zeros are invalid except for a component equal to `0`.
5. Enter a **Reason**, then run the workflow. The request records your actor name and workflow-run link.

Dispatch authorizes the proposal. The Actions summary links to the App-owned maintenance pull request. Auto-merge waits for catalog validation, the shared PR-title check, and an up-to-date branch; no protection bypass or direct `main` push is used.

Withdrawal retains history, release files, and installed plugins. Reconciliation cannot re-add an excluded version. Restoration verifies the original stable release, source, receipt, and hashes before removing its exclusion. Failed verification preserves pending decisions.

Unmerged requests accumulate on the maintenance branch. An opposite request can cancel a pending change; if nothing remains, the App closes the pull request. Cancelling an unmerged withdrawal needs no remote verification because it restores the existing `main` state. Repeating a completed operation succeeds without changes.

Required maintenance validation compares against the authenticated pull request base or preceding `main` commit. It checks the exact manifest projection while preserving policy, history, and unrelated records. Only restored versions need remote checks; unrelated integrity failures or failure to mint the optional source token don't block withdrawal. A separate non-blocking audit reports remaining problems, and scheduled reconciliation reports persistent issues.

### Edit exclusions offline

For a manually reviewed change, edit `withdrawals.json` with an allowed GUID, canonical version, and nonempty reason. Then regenerate the manifest:

```sh
uv run --no-sync python scripts/catalog_sync.py withdraw
```

Invalid versions, such as `00.1.3.0`, leave the manifest untouched. Keep history intact and submit both `withdrawals.json` and `manifest.json` together.

## Recover failed updates

Use the workflow log and integrity issue to choose a recovery action:

| Symptom                                                               | Action                                                                                                                                                                                        |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Missing assets, unavailable evidence, API failure, or changed release | Resolve the cause, then rerun **Reconcile plugin catalog**. Preserve existing releases and history.                                                                                           |
| `main` advances during preparation                                    | Rerun reconciliation or the maintenance request against the new base.                                                                                                                         |
| Push succeeds but publication pull request creation fails             | Rerun reconciliation. Recovery requires the branch to contain the captured `main` commit, change only regular non-executable catalog files, and exactly match the freshly verified candidate. |
| Unmatched or stale publication branch                                 | Inspect its diff and resolve or remove it as owner before retrying. Don't delete a branch containing a pending owner decision.                                                                |
| Conflicting newer maintenance decision or non-App branch              | Resolve the existing pull request before retrying; automation won't overwrite the conflict.                                                                                                   |
| Failed refresh leaves auto-merge disabled                             | Explicitly retry the unchanged proposal to resume auto-merge after validation.                                                                                                                |
| Actionlint rejects only `concurrency.queue`                           | Ignore only that unsupported-key diagnostic on older versions. Keep other diagnostics enabled; the policy test checks both workflows' queue settings.                                         |

Publication verifies against a captured protected `main` commit and checks `main` again before pushing or recovering a pull request. Existing branches use the observed SHA as the push lease; new branches require an absent remote branch. Local publication requires a clean tracked working tree.

Open publication pull requests must belong to this App and target `main` in this repository. Automation rejects ambiguous ownership. Completed branches can be reused when their exact head belongs to a merged or closed App-owned pull request; branch deletion is optional.

Maintenance reapplies pending decisions when `main` advances and rejects conflicting newer decisions. Both paths disable auto-merge before refreshing an open proposal and request it again after checking the updated proposal.

## Onboard another plugin

Before onboarding, prepare a stable release with the required evidence:

1. Add its repository, immutable GUID, asset prefix, required groups, and approved workflow paths to `plugins.json` in a reviewed pull request.
2. Install the shared App on the plugin repository and configure its credentials.
3. Extend the read-only source token repository lists in both [sync](.github/workflows/sync.yml) and [maintenance](.github/workflows/maintenance.yml) workflows.
4. Adopt the durable receipt, attestation, and notification contract, then verify a stable release. Initial import selects only the latest stable release.

Other naming conventions or receipt policies require a reviewed adapter, without an unvalidated fallback.

## Test the catalog

Use Conventional Commit messages and PR titles, such as `fix(catalog): verify release metadata`, `chore(deps): update tooling`, or `docs: clarify setup`. A separate shared CI check validates PR titles; title/body edits do not rerun catalog validation, while base-branch retargeting does. PR titles are used as the squash commit title when merging. Automated catalog commits and squash merges use `chore(catalog): ...`; Renovate uses `chore(deps): ...`.

Install the Python and Node versions declared in `.python-version` and `.node-version`, the pnpm version from `package.json`, the uv version required by `pyproject.toml`, and Docker. uv uses the installed Python interpreter; setup-uv reads its version from the project rather than a second workflow pin. Restore tools explicitly before checks:

```sh
uv sync --locked
pnpm install --frozen-lockfile
uv run --no-sync jpt prepare
pnpm check
pnpm test
uv run --no-sync python scripts/tools.py test-renovate
```

Use `pnpm format` to apply formatting. The configuration check uses pinned Docker images and has no network access. GitHub CLI is pinned in `toolchain.json`; install the verified Linux AMD64 binary before live verification:

```sh
uv run --no-sync python scripts/tools.py install-gh --directory /tmp/catalog-tools
export PATH="/tmp/catalog-tools:$PATH"
```

Renovate inherits the versioned shared tooling preset. Routine patch and minor updates can automerge after required checks and a one-day release delay. Major updates, runtime/compiler/package-manager minor upgrades, and changes to the shared preset require review. Package-specific policy remains local. The catalog installs the pinned toolkit wheel only as a development dependency for shared checks and configuration. Scheduled catalog synchronization and release verification use `uv sync --locked --no-dev` and retain their independent verifier. Toolkit updates use the existing trusted binding synchronization workflow; its App installation needs workflow-write permission when synchronizing workflow pins. After configuring the App, dispatch **Set up connected GitHub App** on the default branch and merge its reviewed identity/Renovate configuration PR. Setup is manual-only; dispatch it again when changing App identity. Credential rotation for the same App only requires retrying the affected operation. Missing setup is a notice in ordinary CI, but blocks binding writes with a setup-workflow link. Malformed ownership records or inconsistent Renovate entries fail static automation checks and must be repaired. Catalog runtime and release verification remain independent of the ownership record and toolkit Python tooling.

The suite uses deterministic GitHub fixtures and disposable local Git repositories to test merging, release policy, maintenance, and publication. It needs Git but no GitHub credentials or remote writes, and removes temporary repositories afterward. Confirm that it exits successfully with `OK`.

For live validation, install GitHub CLI (`gh`) and set `SOURCE_GH_TOKEN` or `GH_TOKEN` with source read access. This check verifies the current catalog without publishing:

```sh
uv run --no-sync python scripts/catalog_sync.py validate
```

For discovery and installation checks, provide Docker and access to public plugin archives. With a nonempty manifest, run:

```sh
uv run --no-sync python tests/CatalogDiscovery.py manifest.json
```

Discovery starts and removes a disposable Jellyfin server; it doesn't run in the automated suite. Success prints `PASS: shared manifest discovered and exact public plugins installed/active in disposable Jellyfin 12.2`.

### Check hosted acceptance

When configuring or changing automation, confirm these behaviors on GitHub:

- A stable release opens an App-owned pull request, starts checks, and merges only after successful required validation.
- Retries create no duplicates, and reconciliation discovers a missed notification.
- A maintenance dispatch from `main` creates an App-owned proposal; failed target verification blocks restoration.
- The served manifest and plugin archive download without authentication, and a disposable Jellyfin server discovers and installs the plugin.

Local fixtures don't establish GitHub authentication, Sigstore verification, release events, branch protection, or auto-merge behavior.

For ongoing changes, use [withdrawal and restoration](#withdraw-or-restore-a-version-from-actions), [plugin onboarding](#onboard-another-plugin), or [recovery actions](#recover-failed-updates).

## Shared toolkit releases

Schema 7 receipts identify the toolkit wheel and the SHA256 of `uv.lock` from the tagged plugin source. Verification checks the canonical released wheel URL, wheel and source distribution hashes, `SHA256SUMS`, and schema 2 `build-provenance.json` against the toolkit release tag. The approved reusable signer workflow must match that exact tag commit. Update plugin allowlists and the GitHub App installation when onboarding another repository.
