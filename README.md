# ci-fallback

Run GitHub Actions jobs on my own notebook first and fall back to GitHub-hosted runners.

- **`pick.yml`** (reusable): outputs `local=true` when the notebook heartbeat is fresh and the event
  isn't a PR from a fork.
- **`rerun.yml`** (reusable, called from `workflow_run`): when a job failed on the notebook,
  re-runs the failed jobs. The second attempt goes to GitHub-hosted runners.
- **`daemon/fallbackd.py`**: runs on the notebook (launchd). It polls the listed repos for queued
  `self-hosted` jobs, starts an ephemeral JIT runner per job (max 4 at a time), and writes the heartbeat
  gist every minute. It never runs jobs from fork PRs.

## Using it in a repo

```yaml
jobs:
  pick:
    uses: guhcostan/ci-fallback/.github/workflows/pick.yml@main
  test:
    needs: pick
    runs-on: ${{ needs.pick.outputs.local == 'true' && github.run_attempt == '1' && fromJSON('["self-hosted","macOS","ARM64"]') || 'ubuntu-latest' }}
```

and `.github/workflows/runner-fallback.yml`:

```yaml
name: runner-fallback
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
permissions:
  actions: write
jobs:
  rerun:
    uses: guhcostan/ci-fallback/.github/workflows/rerun.yml@main
```

Add the repo to `~/.ci-fallback/repos.txt` on the notebook.

## Notebook install

```sh
# base runner install (osx-arm64) in ~/actions-runner, not registered
mkdir -p ~/.ci-fallback/logs && cp daemon/fallbackd.py ~/.ci-fallback/
sed "s|__HOME__|$HOME|g" daemon/dev.ci-fallback.plist > ~/Library/LaunchAgents/dev.ci-fallback.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.ci-fallback.plist
tail -f ~/.ci-fallback/logs/daemon.log
```

Uses `gh auth token` for the API (needs `repo` and `gist` scopes).
