# ci-kit onboarding

This repository is managed by [`quokkify/ci-kit`](https://github.com/quokkify/ci-kit) at `v4.1.0`. This file is toolkit-owned: `ci-kit update` rewrites it, so keep project notes elsewhere.

## Modules

ci-kit is installed as a set of modules. Every module has its own Copier answers file, `.copier-answers.<module>.yml`, and a declaration, `.ci/modules/<module>.yml`. A module owns the files it renders: they are replaced on every update, so change them in ci-kit, not here. Starter files (`README.md`, `.github/pull_request_template.md`, and the starters of other modules) are written once and then belong to the project.

`core` is always installed. It owns the `Validate` workflow, the template update workflow `.github/workflows/copier-update.yml`, and this guide. The other modules, the files each one renders, and their questions are listed in [`docs/modules.md`](https://github.com/quokkify/ci-kit/blob/v4.1.0/docs/modules.md).

## Check the installation

```bash
pip install "git+https://github.com/quokkify/ci-kit@v4.1.0"
ci-kit doctor --repo .
```

`ci-kit doctor` reports missing or conflicting answers files, invalid module declarations, and files that differ from the template render. Each finding names an error code with a runbook in [`docs/errors/`](https://github.com/quokkify/ci-kit/tree/v4.1.0/docs/errors).

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
copier copy --trust --vcs-ref v4.1.0 -a .copier-answers.<module>.yml -d module=<module> gh:quokkify/ci-kit .
```

Change a module's answers by re-running `copier update -a .copier-answers.<module>.yml --data <question>=<value>`. Question names and allowed values are defined in [`copier.yml`](https://github.com/quokkify/ci-kit/blob/v4.1.0/copier.yml).
