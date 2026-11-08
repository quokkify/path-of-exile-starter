# ci-kit onboarding

This repository is managed by [`quokkify/ci-kit`](https://github.com/quokkify/ci-kit) at `v4.3.5`. This file is toolkit-owned: `ci-kit update` rewrites it, so keep project notes elsewhere.

## Modules

ci-kit is installed as a set of modules. Every module has its own Copier answers file, `.copier-answers.<module>.yml`, and a declaration, `.ci/modules/<module>.yml`. A module owns the files it renders: they are replaced on every update, so change them in ci-kit, not here. Starter files (`README.md`, `.github/pull_request_template.md`, and the starters of other modules) are written once and then belong to the project.

`core` is always installed. It owns the `Validate` workflow, the template update workflow `.github/workflows/copier-update.yml`, the `PR contract` check `.github/workflows/pr-contract.yml`, and this guide. The other modules, the files each one renders, and their questions are listed in [`docs/modules.md`](https://github.com/quokkify/ci-kit/blob/v4.3.5/docs/modules.md).

## Pull request titles and release notes

Release notes are built from merged pull requests: their title, their labels and three body sections. Nothing else in a PR is published.

- The type decides who sees the change. `feat`, `fix`, `perf` and `revert` are listed for users; every other type (`deps`, `chore`, `ci`, `test`, `build`, `docs`, `style`, `refactor`) goes under a collapsed "Internal changes" list. The label `release-notes: internal` moves a `feat` or `fix` that only maintainers notice there too.
- The title is the headline: `type(scope): summary`, at most 72 characters. Describe what the user notices, not the files or functions you changed.
- `## Release note` is one paragraph of at most 300 characters for someone who only reads release notes: what they can do now, or what was broken and how it showed, plus the input, flag or step to use. It is required for `feat`, `perf` and breaking PRs and encouraged for `fix`. When the title already says everything, add the label `release-notes: title-only`.
- `## Usage example` (optional, `feat`) is one short fenced snippet users can copy.
- A breaking change (`!` in the title) needs `## Migration` with the numbered steps users must take. It is shown first, in a warning box, and is not repeated elsewhere.
- Everything else in the body (`## Description`, test plans, tables) is for reviewers.
- To change a published note, edit the merged PR's title, body or labels; the open release PR is regenerated on the next push to the default branch. Do not edit the release PR body.

The `PR contract` check reads only the PR event: it re-runs when the title, body or labels change, skips PRs opened by bots (GitHub Apps), and prints the exact section to add. It reports problems as warnings until the toolkit switches it to `enforce`.

## Check the installation

```bash
pip install "git+https://github.com/quokkify/ci-kit@v4.3.5"
ci-kit doctor --repo .
```

`ci-kit doctor` reports missing or conflicting answers files, invalid module declarations, and files that differ from the template render. Each finding names an error code with a runbook in [`docs/errors/`](https://github.com/quokkify/ci-kit/tree/v4.3.5/docs/errors).

## Update to a newer toolkit release

Run the `Update project template` workflow; it updates every module to the latest release and pushes a branch for review. To update by hand:

```bash
git switch main && git pull --ff-only
export TOOLKIT_REF="$(gh release view --repo quokkify/ci-kit --json tagName --jq .tagName)"
git switch -c "chore/update-ci-kit-${TOOLKIT_REF}"
pip install "git+https://github.com/quokkify/ci-kit@${TOOLKIT_REF}"
ci-kit update --repo . --to "$TOOLKIT_REF"
git push -u origin HEAD
gh pr create --fill
```

`ci-kit update` commits one update per module and stops on the first Copier conflict.

## Add a module

```bash
copier copy --trust --vcs-ref v4.3.5 -a .copier-answers.<module>.yml -d module=<module> gh:quokkify/ci-kit .
```

Change a module's answers by re-running `copier update -a .copier-answers.<module>.yml --data <question>=<value>`. Question names and allowed values are defined in [`copier.yml`](https://github.com/quokkify/ci-kit/blob/v4.3.5/copier.yml).
