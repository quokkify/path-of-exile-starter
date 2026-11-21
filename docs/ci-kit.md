# ci-kit onboarding

This repository is managed by [`quokkify/ci-kit`](https://github.com/quokkify/ci-kit) at `v4.3.7`. This file is toolkit-owned: `ci-kit update` rewrites it, so keep project notes elsewhere.

## Modules

ci-kit is installed as a set of modules. Every module has its own Copier answers file, `.copier-answers.<module>.yml`, and a declaration, `.ci/modules/<module>.yml`. A module owns the files it renders: they are replaced on every update, so change them in ci-kit, not here. Starter files (`README.md`, `.github/pull_request_template.md`, and the starters of other modules) are written once and then belong to the project.

`core` is always installed. It owns the `Validate` workflow, the template update workflow `.github/workflows/copier-update.yml`, the `PR contract` check `.github/workflows/pr-contract.yml`, and this guide. The other modules, the files each one renders, and their questions are listed in [`docs/modules.md`](https://github.com/quokkify/ci-kit/blob/v4.3.7/docs/modules.md).

## Pull request titles and release notes

Release notes are built from merged pull requests: their title, their labels and three body sections. Nothing else in a PR is published.

- The type decides who sees the change. `feat`, `fix`, `perf` and `revert` are listed for users; every other type (`deps`, `chore`, `ci`, `test`, `build`, `docs`, `style`, `refactor`) goes under a collapsed "Internal changes" list. The label `release-notes: internal` moves a `feat` or `fix` that only maintainers notice there too.
- The title is the headline: `type(scope): summary`, at most 72 characters. Describe what the user notices, not the files or functions you changed.
- `## Release note` is one paragraph of at most 300 characters for someone who only reads release notes: what they can do now, or what was broken and how it showed, plus the input, flag or step to use. It is required for `feat`, `perf` and breaking PRs and encouraged for `fix`. When the title already says everything, add the label `release-notes: title-only`.
- `## Usage example` (optional, `feat`) is one short fenced snippet users can copy.
- A breaking change (`!` in the title) needs `## Migration` with the numbered steps users must take. It is shown first, in a warning box, and is not repeated elsewhere.
- Everything else in the body (`## Description`, test plans, tables) is for reviewers.
- To change a published note, edit the merged PR's title, body or labels; the open release PR is regenerated on the next push to the default branch. Do not edit the release PR body.

Projects created before v4.3.0 still have the old pull request template (`## Description`, `## Release notes`, `## Highlight`, ...), because `.github/pull_request_template.md` is written once and never updated. Their PRs keep working: the release-notes helper and the `PR contract` check accept `## Release notes` and `## Highlight` in place of `## Release note`. To switch to the current sections, replace `.github/pull_request_template.md` with the template from the latest release: `gh api -H 'Accept: application/vnd.github.raw' "repos/quokkify/ci-kit/contents/modules/core/template/.github/pull_request_template.md.jinja?ref=$TOOLKIT_REF" > .github/pull_request_template.md` (`TOOLKIT_REF` is set in the update steps below), then re-apply any local edits.

The `PR contract` check reads only the PR event: it re-runs when the title, body or labels change, skips PRs opened by bots (GitHub Apps), and prints the exact section to add. It reports problems as warnings until the toolkit switches it to `enforce`.

## Check the installation

```bash
pip install "git+https://github.com/quokkify/ci-kit@v4.3.7"
ci-kit doctor --repo .
```

`ci-kit doctor` reports missing or conflicting answers files, invalid module declarations, and files that differ from the template render. Each finding names an error code with a runbook in [`docs/errors/`](https://github.com/quokkify/ci-kit/tree/v4.3.7/docs/errors).

## Update to a newer toolkit release

Run the `Update project template` workflow; it updates every module to the latest release and pushes a branch for review. GitHub does not let the workflow's own token push changes to `.github/workflows/`, and most updates change them, so add a `TEMPLATE_UPDATE_TOKEN` repository secret: a fine-grained personal access token with an expiry date, for this repository only, with `Contents` and `Workflows` read and write. Only the workflow's `push` job, which runs no template code, receives it. Setting the token has a trade-off. Unlike a push with the workflow's own token, a push with it starts `on: push` workflows on the update branch before review ([GitHub docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow)). Keep workflows that use secrets behind `branches: [<default branch>]` filters or protected environments, or leave the token unset and update by hand when workflow files change. Without it, an update that changes workflow files stops with [`CIK-CORE-031`](https://github.com/quokkify/ci-kit/blob/v4.3.7/docs/errors/CIK-CORE-031.md) before pushing. Public Quokkify repositories also receive updates from the ci-kit fleet automation and do not need the secret. To update by hand:

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
copier copy --trust --vcs-ref v4.3.7 -a .copier-answers.<module>.yml -d module=<module> gh:quokkify/ci-kit .
```

Change a module's answers by re-running `copier update -a .copier-answers.<module>.yml --data <question>=<value>`. Question names and allowed values are defined in [`copier.yml`](https://github.com/quokkify/ci-kit/blob/v4.3.7/copier.yml).
