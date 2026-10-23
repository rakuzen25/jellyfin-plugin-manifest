// Run inside the pinned Renovate image; exercise its actual extraction logic.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { extractPackageFile } from "/usr/local/renovate/dist/modules/manager/custom/regex/index.js";
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const config = JSON.parse(fs.readFileSync(path.join(root, "renovate.json"), "utf8"));
const pins = JSON.parse(fs.readFileSync(path.join(root, "toolchain.json"), "utf8"));
const dependencies = [];
for (const manager of config.customManagers) {
    const filename = manager.managerFilePatterns[0].includes("pyproject") ? "pyproject.toml" : "toolchain.json";
    dependencies.push(...(await extractPackageFile(fs.readFileSync(path.join(root, filename), "utf8"), filename, manager)).deps);
}
const gh = dependencies.find((dep) => dep.depName === "cli/cli");
assert.equal(gh.currentValue, pins.gh);
const uv = dependencies.find((dep) => dep.depName === "uv");
assert.equal(uv.packageName, "astral-sh/uv");
assert.equal(uv.datasource, "github-releases");
const project = fs.readFileSync(path.join(root, "pyproject.toml"), "utf8");
assert.ok(project.includes('required-version = "==' + uv.currentValue + '"'));
for (const pin of [pins.actionlint, pins.renovate]) {
    const [name, version, digest] = pin.match(/^(.*):([^@]+)@(sha256:.*)$/).slice(1);
    const dep = dependencies.find((candidate) => candidate.depName === name);
    assert.equal(dep.currentValue, version);
    assert.equal(dep.currentDigest, digest);
}
assert.equal(dependencies.length, 4);
console.log("PASS: Renovate tracks GitHub CLI, uv, Actionlint and its own pinned image");
