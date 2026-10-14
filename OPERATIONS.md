# Shared Jellyfin catalog

The locally prepared catalog serves one installation URL after you create and publish its repository:

```text
https://raw.githubusercontent.com/rakuzen25/jellyfin-plugin-manifest/main/manifest.json
```

Each plugin keeps its source, GUID, versions and downloadable releases. Initially only LyricBridge is allowlisted. The prepared catalog contains LyricBridge v0.1.3, verified against authenticated CI evidence and the public archives. Subsequent plugins start with their latest stable release only. Missing evidence is a failed reconciliation with a deduplicated issue, never an implicit exemption.

## Prepare the new repository locally

Run from the LyricBridge checkout, choosing a destination that does not exist:

```sh
python3 scripts/prepare_catalog.py /tmp/jellyfin-plugin-manifest
cd /tmp/jellyfin-plugin-manifest
python3 tests/CatalogPublication.py
```

The exporter copies the templates and maintained Python implementation; no Git repository, commit, push, App or remote release is created. `scripts/catalog.py` remains the shared source for merge validation. In the exported repository, those scripts are self-contained. Future catalog-tool changes should be reviewed in the catalog repository as well as in this development copy; exporting over an existing checkout is deliberately forbidden.

When ready to publish the reviewed files, create an **empty public** repository named `rakuzen25/jellyfin-plugin-manifest` with default branch `main`, then use:

```sh
cd /tmp/jellyfin-plugin-manifest
git init -b main
git add .
git commit -m "Initialize shared Jellyfin catalog"
git remote add origin https://github.com/rakuzen25/jellyfin-plugin-manifest.git
git push -u origin main
```

These commands are handoff instructions, not actions performed during implementation. GitHub CLI is required by the automation and provided on hosted runners; local remote checks also require an authenticated `gh` installation.

## Create one shared GitHub App

1. Open your GitHub **Settings → Developer settings → GitHub Apps → New GitHub App**. Choose a globally available name such as `rakuzen25-plugin-publisher`. Set the homepage to the catalog repository. Disable webhooks; no webhook server, callback URL or user authorization is needed. Limit installation to your account.
2. Grant repository **Contents: Read and write**, **Pull requests: Read and write**, **Issues: Read and write**, **Actions: Read-only**, and **Attestations: Read-only**. Metadata read is automatic. Grant no organization, administration or workflow-writing permissions.
3. Install the App using **Only select repositories**: `jellyfin-plugin-lyricbridge` and `jellyfin-plugin-manifest`.
4. Generate and securely store a private key. In **both** repositories, add Actions variable `PLUGIN_APP_CLIENT_ID` containing the App's Client ID, and Actions secret `PLUGIN_APP_PRIVATE_KEY` containing the complete PEM private key. Never commit the PEM or paste it into an issue/log. Personal-account repository secrets must be configured separately; the App and key are shared, not long-lived access tokens.
5. In LyricBridge, keep the existing required `validate` check. Land the App workflow migration only after these credentials exist. Confirm the first App-created Release Please PR triggers ordinary validation. Once confirmed, delete the obsolete `RELEASE_PLEASE_TOKEN` secret and revoke its PAT if it is no longer used elsewhere.

Tokens are generated just before use and revoked at job completion. Release Please requests write permissions only on the current plugin. Notification requests Contents write only on the catalog (required by repository_dispatch). Catalog updates request write access only on the catalog. Read-only source inspection uses a separate token scoped to LyricBridge. Catalog PR checks use the read-only workflow token and never receive the App private key. The shared private key can mint permissions on all installed repositories: keeping installation narrow and rotating that key matters even though individual job tokens are scoped.

GitHub's ordinary workflow token still uploads the verified draft assets and attestation. Attestation permissions (`id-token: write`, `attestations: write`) belong only to the attachment job and its caller; they are not App-wide write permissions.

## Protect the catalog and enable automatic additions

After the initial push:

1. Run **Validate catalog** once and confirm the job named `catalog-validate` exists and passes for the prepared catalog.
2. Under **Settings → General → Pull Requests**, enable **Allow auto-merge** and squash merging.
3. Under **Settings → Rules → Rulesets**, create an active branch rule for `main`: require a pull request, require the `catalog-validate` status check (GitHub Actions as its provider), and require branches to be up to date before merging. Require no approving reviews for automated additions. Block force pushes and branch deletion. Add no App bypass.
4. Confirm **Actions → General** permits the reviewed SHA-pinned GitHub actions used here. Preserve read-only default workflow permissions; individual jobs request only what they need.
5. Run **Reconcile plugin catalog** manually. It verifies releases and is a no-op when the prepared catalog is current. A new stable release creates an App-owned PR, enables squash auto-merge and waits for required validation. Inspect the first resulting PR and its required-check/merge history.

Automated PRs change only `history.json` and generated `manifest.json`. Policy, tooling and withdrawal edits are ordinary manually reviewed PRs; the updater never enables auto-merge for them. The updater owns one App-authored branch, serializes reconciliation, and uses a lease when refreshing that branch. Duplicate notifications are harmless. No plugin build or browser suite runs in the catalog repository.

## Publication, evidence and recovery

The release flow remains: release PR → draft/tag → all required artifact gates → verified assets → your final **Publish** click. A stable publication triggers **Notify shared plugin catalog**. The catalog ignores the payload as authority and reconciles its allowlist using GitHub's release/tag APIs.

Future releases add a sixth asset, `validation.json`. Its hashes cover the original five assets; it is excluded from its own hash inventory and `SHA256SUMS`. The authenticated attachment workflow attests its exact bytes before uploading. Verification uses `gh attestation verify`, the approved reusable signer workflow, a main-branch source ref and hosted-runner policy, followed by authenticated successful attachment/aggregate jobs and exact public-asset hashes. A generic green workflow or unsigned uploaded JSON is insufficient.

Older releases can bootstrap through a nonexpired authenticated `lyricbridge-validation-receipt` CI artifact. The small original ZIP is retained in catalog history, bound to the artifact ID, GitHub-provided SHA-256 digest and validation run. Later checks verify this archive against public artifact metadata instead of downloading an expired archive. If lookup is exhausted, evidence has expired or no full release receipt exists, wait for the next qualifying release. Existing public releases are never modified to add evidence. If GitHub subsequently deletes the bootstrap artifact metadata or originating run, historical CI-only evidence becomes unavailable; the served catalog remains unchanged and an issue requests owner attention.

Daily reconciliation runs at **06:23 UTC** and the Actions UI also offers manual refresh. It discovers missed publications after the initial latest-only onboarding and retains all subsequently verified stable versions. Drafts and prereleases are excluded. Downloads, source archives, license/source inventories and fresh advisory assessment remain the plugin release workflow's responsibility; catalog verification does not rebuild or bypass them.

Missing assets, API/rate-limit errors, moved tags or checksum conflicts fail before a candidate is published. Existing manifest entries remain intact. Automation opens or updates a deduplicated integrity issue and closes its issues after full recovery. Retry the plugin's **Notify shared plugin catalog** workflow with the published tag, or the catalog's **Reconcile plugin catalog** workflow without arguments.

## Withdraw or restore a version from Actions

In **rakuzen25/jellyfin-plugin-manifest → Actions → Withdraw or restore version → Run workflow**, choose branch **main** and enter:

- **Operation:** `Withdraw` or `Restore`.
- **Repository:** the allowlisted plugin repository, currently `rakuzen25/jellyfin-plugin-lyricbridge`.
- **Version:** its exact catalog version, for example `0.1.3.0` (four components).
- **Reason:** the owner decision recorded with your actor name and workflow-run link.

Running the workflow authorizes the proposal. The shared App opens or updates its dedicated `automation/catalog-maintenance` PR and enables squash auto-merge behind the existing `catalog-validate` and up-to-date requirements. The Actions summary links to the PR; auto-merge is requested, not reported as already completed. No extra credentials, environment approvals, protection bypass or direct main push is used.

Withdrawal changes only `withdrawals.json` and the generated `manifest.json`. History, release files and already installed plugins are retained. Reconciliation cannot re-add an excluded version. Restoration freshly verifies the original stable release, source, receipt and hashes before removing its exclusion; failures leave pending decisions intact. Cancelling an unmerged withdrawal restores the existing main state without introducing a new version and therefore needs no remote verification. Repeating a completed operation is a successful no-op.

Requests share a non-cancelling `catalog-update` queue with release additions. `queue: max` preserves up to 100 pending runs; requests beyond GitHub's queue capacity must be retried. Execution order follows the runner queue, so the latest successfully processed request determines a version's pending state. Several unmerged requests accumulate on the maintenance branch. A later opposite request can cancel an earlier pending change; if nothing remains, the App closes that PR without committing to main. Completed App-owned branches can be reused safely even when automatic branch deletion is disabled.

When main advances, the updater reapplies only pending maintenance decisions to the new base. A conflicting newer main decision fails clearly rather than being overwritten. If the summary says main advanced during preparation, rerun the request. If it reports a conflicting owner decision or non-App branch, inspect and resolve the existing PR first. A failed refresh can leave auto-merge disabled; an unchanged explicit retry safely resumes it after validation.

`catalog-validate` recognizes a narrow maintenance-only diff against the authenticated PR base or preceding main commit. It proves the exact exclusion/removal projection while keeping policy, history and unrelated records unchanged. Only versions reintroduced by restoration require remote checks. Unrelated network/integrity failures cannot prevent a safe withdrawal. Failure to mint the optional read-only source token also does not block a removal; a restoration still fails unless its required public evidence can be verified. A separate non-blocking audit reports remaining catalog problems in Actions; full scheduled reconciliation continues to report persistent problems as issues. Ordinary addition/policy/tooling PRs retain full verification.

The maintenance Action and its tests must land before attempting to use it. Existing `catalog-validate` protection is unchanged. After pushing, confirm a real main-branch dispatch creates an App-owned PR, that failed target verification blocks restoration, and that auto-merge waits for the required check. These GitHub observations have not been established by mocked local tests.

For manually reviewed offline edits, the existing `python3 scripts/catalog_sync.py withdraw` command still regenerates the manifest after editing exclusions. Keep history and submit both exclusions and manifest together.

The installed Actionlint 1.7.12 predates GitHub's documented `concurrency.queue` key. Local linting ignores only its unsupported-key diagnostic, with a focused policy test checking that both state-changing workflows use the same `catalog-update`, `queue: max`, `cancel-in-progress: false` settings. All other Actionlint diagnostics remain enabled.

## Onboard another plugin

Add its repository, immutable GUID, asset prefix, receipt schema/group requirements and approved workflow paths to `plugins.json` in a manually reviewed PR. Install the shared App on that plugin, configure its credentials, extend the updater’s read-only source token repository list, adopt the same durable-receipt/attestation release contract and notification workflow, and verify a stable release. The initial importer chooses that plugin's latest stable release only. Other naming conventions or receipt policies require a reviewed adapter, not an unvalidated generic fallback.

## Local evidence and hosted acceptance

Local commands:

```sh
python3 tests/CatalogPublication.py
python3 tests/CatalogPreparation.py
python3 tests/Catalog.py
python3 tests/ReleaseAutomation.py
python3 tests/CiPolicy.py
python3 tests/CatalogDiscovery.py catalog-repository/manifest.json
```

Run the exported catalog tests independently as well. Mocked policy tests do not establish real GitHub authentication, Sigstore verification, release events, branch protection or auto-merge behavior.

After setup, confirm a real App-created Release Please PR runs CI; a verified draft uploads an attested receipt; stable publication opens an App-owned catalog PR; failed catalog validation blocks merging; successful validation auto-merges; retries create no duplicates; daily recovery discovers a missed release. Download the served public manifest and plugin asset without authentication, then add the URL to a disposable Jellyfin server and confirm discovery/installability. These hosted checks remain outstanding until the repository and App exist.

### Observed local verification — October 14, 2026

LyricBridge `v0.1.3` was bootstrapped from run `38561503416` and authenticated receipt artifact `11893465855` (GitHub SHA-256 archive digest `222baf2f7c66948615e44ef06dc17fd301d89b2d90ed7c76c492131f18702a34`). All public artifact hashes, source/tag identity, complete release groups, aggregate job, Jellyfin MD5 and plugin ZIP integrity matched. The prepared shared manifest was discovered by a disposable official Jellyfin 12.2 container, and the exact public plugin installed and became active; the container was removed afterward.

Maintained catalog/release/CI policy tests, standalone export tests, Ruff formatting/lint, Mypy, Actionlint, ESLint and Prettier passed. The broader Node tooling suite exposed an existing `tsc6 --version` failure (empty output instead of `Version 6.0.2`); its script and dependency pins were not changed by this work. Hosted App/attestation creation, notifications, repository protection and auto-merge have not been exercised.
