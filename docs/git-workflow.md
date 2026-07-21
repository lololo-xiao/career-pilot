# CareerPilot Git and GitHub workflow

Use Git as a sequence of recoverable states. Keep `main` releasable and do ordinary work on
short-lived branches that merge through GitHub pull requests.

## The state model

| State | Meaning | Inspect it |
|---|---|---|
| Working tree | Files edited locally but not selected for a commit | `git diff` |
| Untracked | New local files Git does not yet manage | `git status -sb` |
| Staging area | The exact snapshot selected for the next commit | `git diff --cached` |
| Local commit | A named, recoverable snapshot on the current branch | `git log --oneline -5` |
| Remote branch | Commits published to GitHub | `git status -sb` and `git branch -vv` |
| Pull request | Review and CI around a branch-to-`main` merge | GitHub's Pull requests page |
| Tag | A permanent name for one merged release commit | `git tag --list` |

`git status -sb` is the safest first command whenever the repository feels confusing.

## Start one change

```bash
git status -sb
git fetch origin
git switch main
git pull --ff-only
git switch -c feature/short-description
```

Do not develop directly on `main`. Keep one purpose per branch and prefer small pull
requests that can be tested and reviewed independently.

## Review and commit intentionally

Inspect before staging:

```bash
git diff --check
git diff
```

Stage explicit paths, then inspect the exact commit candidate:

```bash
git add -- path/to/file another/path
git diff --cached --check
git diff --cached
git status -sb
git commit -m "Describe the completed change"
```

Avoid a blind `git add -A` in a working tree that may contain local experiments. Never
stage `.env*`, credentials, signing material, private user data, generated Xcode build
products, or exported application databases.

## Publish and review

Authenticate GitHub CLI through its browser flow if needed:

```bash
gh auth login -h github.com
gh auth status -h github.com
```

Then publish the branch and open a draft pull request:

```bash
git push -u origin HEAD
gh pr create --draft --fill
gh pr checks --watch
```

Turn the pull request ready only after its description explains the user-visible change,
risks, test evidence, and rollout/rollback plan. Merge after required checks pass. Delete
the remote feature branch after merge; the commits remain reachable through `main` and the
pull request.

## Bring a branch up to date

Use a merge for the least surprising beginner workflow:

```bash
git fetch origin
git switch feature/short-description
git merge origin/main
```

If Git reports conflicts, open each marked file, choose the correct combined content, run
tests, then stage the resolved files and complete the merge commit. Do not force-push or
rewrite shared history merely to make a conflict disappear.

## Safe recovery commands

Unstage a file while preserving its local edits:

```bash
git restore --staged path/to/file
```

Temporarily store unfinished tracked and untracked work:

```bash
git stash push -u -m "why this work is paused"
git stash list
git stash pop
```

Inspect recent commits and recovery history:

```bash
git log --oneline --graph --decorate --all -20
git reflog -20
```

`git restore path/to/file` discards uncommitted edits in that file. `git reset --hard`,
recursive deletion, and force-push can destroy work or remote history; do not use them as
routine cleanup commands.

## After a pull request merges

```bash
git switch main
git pull --ff-only
git branch -d feature/short-description
```

For a released App Store version, tag the exact tested merge commit only after release:

```bash
git tag -a v1.0.0 -m "CareerPilot iOS 1.0.0"
git push origin v1.0.0
```

The Xcode public version should match the release tag. Every TestFlight/App Store upload
must still have a new build number even when the public version is unchanged.

## Recommended `main` ruleset

In **GitHub repository → Settings → Rules → Rulesets**, add an active branch ruleset that
targets the default branch and:

- requires a pull request before merging;
- requires the backend, frontend, Hermes contract, and iOS CI checks to pass;
- blocks force pushes and branch deletion;
- dismisses approvals after material new pushes when another reviewer is available;
- avoids broad bypass permissions.

GitHub documents the choices under
[available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).
For a solo repository, requiring a pull request and CI is still useful even if a second
human approval is not always possible.

## Daily ten-second check

```bash
git status -sb
git branch -vv
git log --oneline --decorate -5
```

The healthy result is: you know which branch is active, whether it is ahead/behind GitHub,
which files are modified or staged, and which commit will be released next.
