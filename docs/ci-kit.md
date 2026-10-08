# ci-kit onboarding

This repository is managed by the [`quokkify/ci-kit`](https://github.com/quokkify/ci-kit) Copier template at `v3.1.0`. This file is toolkit-owned: `copier update` rewrites it, so keep project notes elsewhere.

## What this project received

| Path                                                     | Purpose                                               | Owner   |
| -------------------------------------------------------- | ----------------------------------------------------- | ------- |
| `.copier-answers.yml`                                    | Copier answers; required for `copier update`          | toolkit |
| `.github/workflows/validate.yml`                         | `Validate` workflow                                   | toolkit |
| `.github/workflows/copier-update.yml`                    | Template update automation                            | toolkit |
| `.github/pull_request_template.md`                       | Pull request template                                 | project |
| `README.md`                                              | Starter README                                        | project |
| `docs/ci-kit.md`                                         | This guide                                            | toolkit |
| `.github/workflows/codeql.yml`                           | CodeQL code scanning                                  | toolkit |
| `.github/workflows/gitleaks.yml`                         | Secret scanning                                       | toolkit |
| `.github/workflows/allure-report.yml`, `.github/allure/` | Allure 3 pull-request report                          | toolkit |
| `.github/workflows/release.yml`, `.github/scripts/`      | Release Please (single)                               | toolkit |
| `.github/release-please/`                                | Release Please config and manifest                    | project |
| `.github/renovate.json`                                  | Renovate config extending `quokkify/renovate-presets` | project |

Toolkit-owned files are replaced by `copier update`; change them in ci-kit, not here. Project-owned files are written once and never overwritten.

## First-day checklist

- [ ] `.copier-answers.yml` is committed.
- [ ] Test runners write Allure results as described in the `README.md` Allure section.
- [ ] Code scanning is available for this repository (GitHub Advanced Security for private repositories).
- [ ] The Renovate GitHub App has access to this repository.
- [ ] The first pull request passes `Validate`; then mark its checks as required in branch protection.

## Update to a newer toolkit release

```bash
git switch main && git pull --ff-only
export TOOLKIT_REF="$(gh release view --repo quokkify/ci-kit --json tagName --jq .tagName)"
git switch -c "chore/update-ci-kit-${TOOLKIT_REF}"
copier update --vcs-ref "$TOOLKIT_REF" --data "toolkit_version=$TOOLKIT_REF" --trust
git diff --check
git add -A
git commit -m "chore: update ci-kit to ${TOOLKIT_REF}"
git push -u origin HEAD
gh pr create --fill
```

Resolve any Copier conflict markers (`<<<<<<<`) before committing. Renovate updates workflow version references between template updates.

## Change answers

Re-run `copier update` with `--data <question>=<value>`, for example `--data gitleaks=false`. Prefer this over editing `.copier-answers.yml` by hand. Question names and allowed values are defined in [`copier.yml`](https://github.com/quokkify/ci-kit/blob/main/copier.yml).
