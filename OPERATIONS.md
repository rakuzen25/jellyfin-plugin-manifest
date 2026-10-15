# Shared Jellyfin catalog

This repository maintains the shared plugin catalog at:

```text
https://raw.githubusercontent.com/rakuzen25/jellyfin-plugin-manifest/main/manifest.json
```

Each plugin keeps its source, GUID, versions and downloadable releases. `plugins.json` defines the allowlist and verification requirements. New plugins start with their latest stable release only. Missing evidence is a failed reconciliation with a deduplicated issue, never an implicit exemption.

## Work on the catalog

Clone this repository and select the Python version in `.python-version`:

```sh
git clone https://github.com/rakuzen25/jellyfin-plugin-manifest.git
cd jellyfin-plugin-manifest
python3 -m unittest discover -s tests -p 'Catalog*.py'
```

In this checkout, `scripts/catalog.py` owns merge validation, `scripts/catalog_core.py` owns release verification and reconciliation, and `scripts/catalog_maintenance_policy.py` owns maintenance-delta validation. The executable sync and maintenance scripts import those shared modules without importing one another.

Submit code, policy and tooling changes through reviewed pull requests. GitHub CLI is required by the automation and provided on hosted runners; live local verification also requires an authenticated source-read token.

## Create one shared GitHub App

1. Open your GitHub **Settings → Developer settings → GitHub Apps → New GitHub App**. Choose a globally available name such as `rakuzen25-plugin-publisher`. Set the homepage to the catalog repository. Disable webhooks; no webhook server, callback URL or user authorization is needed. Limit installation to your account.
2. Grant repository **Contents: Read and write**, **Pull requests: Read and write**, **Issues: Read and write**, **Actions: Read-only**, and **Attestations: Read-only**. Metadata read is automatic. Grant no organization, administration or workflow-writing permissions.
3. Install the App using **Only select repositories**: `jellyfin-plugin-lyricbridge` and `jellyfin-plugin-manifest`.
4. Generate and securely store a private key. In this repository, add Actions variable `PLUGIN_APP_CLIENT_ID` containing the App's Client ID, and Actions secret `PLUGIN_APP_PRIVATE_KEY` containing the complete PEM private key. Never commit the PEM or paste it into an issue/log.

Tokens are generated just before use and revoked at job completion. Catalog updates request write access only on this repository. Read-only source inspection uses a separate token scoped to the allowlisted plugin repositories. Catalog PR checks use the read-only workflow token and never receive the App private key. The private key can mint permissions on all installed repositories: keep installation narrow and rotate the key when needed.

The catalog consumes release assets and attestations. Its App needs Attestations read access; release creation and attestation signing belong to the plugin publisher.

## Protect the catalog and enable automatic additions

Configure the catalog repository:

1. Run **Validate catalog** and confirm the job named `catalog-validate` exists and passes.
2. Under **Settings → General → Pull Requests**, enable **Allow auto-merge** and squash merging.
3. Under **Settings → Rules → Rulesets**, create an active branch rule for `main`: require a pull request, require the `catalog-validate` status check (GitHub Actions as its provider), and require branches to be up to date before merging. Require no approving reviews for automated additions. Block force pushes and branch deletion. Add no App bypass.
4. Confirm **Actions → General** permits the reviewed SHA-pinned GitHub actions used here. Preserve read-only default workflow permissions; individual jobs request only what they need.
5. Run **Reconcile plugin catalog** manually. It verifies releases and is a no-op when the catalog is current. A new stable release creates an App-owned PR, enables squash auto-merge and waits for required validation. Inspect the resulting PR and its required-check/merge history.

Automated PRs change only `history.json` and generated `manifest.json`. Policy, tooling and withdrawal edits are ordinary manually reviewed PRs; the updater never enables auto-merge for them. The updater owns one App-authored branch, serializes reconciliation, and uses a lease when refreshing that branch. Duplicate notifications are harmless. No plugin build or browser suite runs in the catalog repository.

Publication loads its inputs from a captured protected main commit and freshly verifies the candidate against that snapshot. A surviving `automation/catalog-update` branch can be reused when its exact head SHA belongs to a merged or closed PR owned by this App, targeting main in this repository. Branch deletion after merging is optional. Open publication PRs must meet the same ownership and base checks; ambiguous PRs are rejected.

If a branch push succeeds but PR creation fails, retry **Reconcile plugin catalog**. The updater can open the missing PR without another push only when the branch includes the captured main commit, its complete diff changes only regular non-executable `history.json` and `manifest.json` files, and both JSON documents exactly match the freshly verified candidate. Unmatched or stale orphan branches are neither overwritten nor adopted. Inspect their diff and resolve or remove the branch as the owner before retrying; do not delete a branch belonging to a pending owner decision.

Publication checks main again before pushing or recovering a PR. Main advancement requires a fresh retry. Existing-branch updates use the exact observed SHA as the push lease; new branches require that no remote branch exists. Auto-merge is disabled before refreshing an open proposal and restored only after the updated PR is checked. A failed refresh can leave auto-merge disabled; retrying an unchanged verified proposal resumes it without another commit or push. Local publication requires a clean tracked working tree.

## Publication, evidence and recovery

The catalog consumes published stable releases from allowlisted repositories using GitHub's release/tag APIs. Publishers may send a `plugin-published` repository dispatch as a reconciliation hint. Daily reconciliation and manual refresh also discover releases; the dispatch payload is never trusted as authority.

Release verification requires the plugin, web and source ZIPs, `manifest.json`, and `SHA256SUMS`, using the asset prefix configured in `plugins.json`. A durable `validation.json` receipt contains hashes of those five assets; it is excluded from its own hash inventory and `SHA256SUMS`. Verification uses `gh attestation verify`, the approved signer workflow, a main-branch source ref and hosted-runner policy, followed by authenticated successful attachment/aggregate jobs and exact public-asset hashes. A generic green workflow or unsigned uploaded JSON is insufficient.

Older releases can bootstrap through a nonexpired authenticated `lyricbridge-validation-receipt` CI artifact. The small original ZIP is retained in catalog history, bound to the artifact ID, GitHub-provided SHA-256 digest and validation run. Later checks verify this archive against public artifact metadata instead of downloading an expired archive. If lookup is exhausted, evidence has expired or no full release receipt exists, wait for the next qualifying release. Existing public releases are never modified to add evidence. If GitHub subsequently deletes the bootstrap artifact metadata or originating run, historical CI-only evidence becomes unavailable; the served catalog remains unchanged and an issue requests owner attention.

Daily reconciliation runs at **06:23 UTC** and the Actions UI also offers manual refresh. It discovers missed publications after the initial latest-only onboarding and retains all subsequently verified stable versions. Drafts and prereleases are excluded. Downloads, source archives, license/source inventories and fresh advisory assessment remain the plugin release workflow's responsibility; catalog verification does not rebuild or bypass them.

Missing assets, API/rate-limit errors, moved tags or checksum conflicts fail before a candidate is published. Existing manifest entries remain intact. Automation opens or updates a deduplicated integrity issue and closes its issues after full recovery. Retry **Reconcile plugin catalog** without arguments after resolving the reported problem.

## Withdraw or restore a version from Actions

In **rakuzen25/jellyfin-plugin-manifest → Actions → Withdraw or restore version → Run workflow**, choose branch **main** and enter:

- **Operation:** `Withdraw` or `Restore`.
- **Repository:** the allowlisted plugin repository, currently `rakuzen25/jellyfin-plugin-lyricbridge`.
- **Version:** its exact catalog version, for example `0.1.3.0` (four numeric components, with no leading zeros except a component equal to `0`).
- **Reason:** the owner decision recorded with your actor name and workflow-run link.

Running the workflow authorizes the proposal. The shared App opens or updates its dedicated `automation/catalog-maintenance` PR and enables squash auto-merge behind the existing `catalog-validate` and up-to-date requirements. The Actions summary links to the PR; auto-merge is requested, not reported as already completed. No extra credentials, environment approvals, protection bypass or direct main push is used.

Withdrawal changes only `withdrawals.json` and the generated `manifest.json`. History, release files and already installed plugins are retained. Reconciliation cannot re-add an excluded version. Restoration freshly verifies the original stable release, source, receipt and hashes before removing its exclusion; failures leave pending decisions intact. Cancelling an unmerged withdrawal restores the existing main state without introducing a new version and therefore needs no remote verification. Repeating a completed operation is a successful no-op.

Requests share a non-cancelling `catalog-update` queue with release additions. `queue: max` preserves up to 100 pending runs; requests beyond GitHub's queue capacity must be retried. Execution order follows the runner queue, so the latest successfully processed request determines a version's pending state. Several unmerged requests accumulate on the maintenance branch. A later opposite request can cancel an earlier pending change; if nothing remains, the App closes that PR without committing to main. Completed App-owned branches can be reused safely even when automatic branch deletion is disabled.

When main advances, the updater reapplies only pending maintenance decisions to the new base. A conflicting newer main decision fails clearly rather than being overwritten. If the summary says main advanced during preparation, rerun the request. If it reports a conflicting owner decision or non-App branch, inspect and resolve the existing PR first. A failed refresh can leave auto-merge disabled; an unchanged explicit retry safely resumes it after validation.

`catalog-validate` recognizes a narrow maintenance-only diff against the authenticated PR base or preceding main commit. It proves the exact exclusion/removal projection while keeping policy, history and unrelated records unchanged. Only versions reintroduced by restoration require remote checks. Unrelated network/integrity failures cannot prevent a safe withdrawal. Failure to mint the optional read-only source token also does not block a removal; a restoration still fails unless its required public evidence can be verified. A separate non-blocking audit reports remaining catalog problems in Actions; full scheduled reconciliation continues to report persistent problems as issues. Ordinary addition/policy/tooling PRs retain full verification.

Required `catalog-validate` protection applies to maintenance PRs. When configuring or changing the maintenance workflow, confirm that a main-branch dispatch creates an App-owned PR, failed target verification blocks restoration, and auto-merge waits for the required check.

For manually reviewed offline edits, `python3 scripts/catalog_sync.py withdraw` regenerates the manifest after editing exclusions. The same canonical version rule applies to `withdrawals.json`: `00.1.3.0` is rejected rather than treated as `0.1.3.0`. Invalid versions leave the existing manifest untouched. Keep history and submit both exclusions and manifest together.

Older Actionlint versions may predate the `concurrency.queue` key. If your installed version reports that unsupported key, ignore only that diagnostic; a focused policy test checks that both state-changing workflows use the same `catalog-update`, `queue: max`, `cancel-in-progress: false` settings. Keep all other Actionlint diagnostics enabled.

## Onboard another plugin

Add its repository, immutable GUID, asset prefix, receipt schema/group requirements and approved workflow paths to `plugins.json` in a manually reviewed PR. Install the shared App on that plugin, configure its credentials, extend the updater’s read-only source token repository list, adopt the same durable-receipt/attestation release contract and notification workflow, and verify a stable release. The initial importer chooses that plugin's latest stable release only. Other naming conventions or receipt policies require a reviewed adapter, not an unvalidated generic fallback.

## Local evidence and hosted acceptance

From this catalog checkout, select the Python version in `.python-version`, then run the same automated suite used by `catalog-validate`:

```sh
python3 -m unittest discover -s tests -p 'Catalog*.py'
```

The suite covers catalog merge/validation, release policy, maintenance, and publication against real temporary Git repositories and a bare remote. GitHub responses and release assets are deterministic fixtures. The temporary repositories are removed afterward; no credentials or remote GitHub writes are required.

Optional live checks, run separately:

```sh
python3 scripts/catalog_sync.py validate
python3 tests/CatalogDiscovery.py manifest.json
```

Live validation requires `gh` and a `GH_TOKEN` or `SOURCE_GH_TOKEN` with source read access. Discovery requires Docker and access to the public plugin archives; it starts and removes a disposable Jellyfin server and does not run in the automated suite. Local fixtures do not establish real GitHub authentication, Sigstore verification, release events, branch protection or auto-merge behavior.

When configuring or changing automation, verify that a published stable release opens an App-owned catalog PR; that PR runs CI; failed catalog validation blocks merging; successful validation auto-merges; retries create no duplicates; and daily reconciliation discovers a missed release. Download the served public manifest and plugin asset without authentication, then add the repository URL to a disposable Jellyfin server and confirm discovery/installability.
