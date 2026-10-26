// Run inside the pinned Renovate image; exercise its actual extraction logic.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { extractPackageFile } from "../.build/jpt/node/renovate-api.mjs";
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const config = JSON.parse(fs.readFileSync(path.join(root, "renovate.json"), "utf8"));
const pins = JSON.parse(fs.readFileSync(path.join(root, "toolchain.json"), "utf8"));
const shared = JSON.parse(fs.readFileSync(path.join(root, ".build/jpt/tooling.json"), "utf8"));
const preset = JSON.parse(fs.readFileSync(path.join(root, ".build/jpt/default.json"), "utf8"));
const dependencies = [];
for (const manager of [
    ...shared.customManagers,
    ...preset.customManagers.filter((manager) => manager.managerFilePatterns.some((pattern) => pattern.includes("pyproject"))),
    ...config.customManagers,
]) {
    const filename = manager.managerFilePatterns[0].includes("pyproject") ? "pyproject.toml" : "toolchain.json";
    dependencies.push(...(await extractPackageFile(fs.readFileSync(path.join(root, filename), "utf8"), filename, manager)).deps);
}
const gh = dependencies.find((dep) => dep.depName === "cli/cli");
assert.equal(gh.currentValue, pins.gh);
assert.equal(dependencies.filter((dep) => dep.depName === "uv").length, 1);
const uv = dependencies.find((dep) => dep.depName === "uv");
assert.equal(uv.packageName, "astral-sh/uv");
assert.equal(uv.datasource, "github-releases");
const project = fs.readFileSync(path.join(root, "pyproject.toml"), "utf8");
assert.ok(project.includes('required-version = "==' + uv.currentValue + '"'));
const toolkit = dependencies.find((dep) => dep.depName === "rakuzen25/jellyfin-plugin-toolkit");
assert.ok(project.includes("/releases/download/" + toolkit.currentValue + "/"));
assert.equal(toolkit.datasource, "github-releases");
assert.equal(dependencies.length, 3);
console.log("PASS: Renovate tracks repository-owned GitHub CLI, uv and the canonical toolkit wheel");
