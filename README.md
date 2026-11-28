# ci-fallback

Run GitHub Actions jobs on my own notebook first and fall back to GitHub-hosted runners.

- **`pick.yml`** (reusable): outputs `local=true` when the notebook heartbeat is fresh and the event
  isn't a PR from a fork.
- **`rerun.yml`** (reusable, called from `workflow_run`): retries notebook failures and confirmed
  job timeouts on GitHub-hosted runners. Each eligible job gets one automatic retry.
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
  checks: read
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

Jobs inherit the daemon's `PATH` (see the plist): Homebrew, the system paths and
`~/.ci-fallback/bin`. Symlink extra CLIs that jobs need there (e.g. `ln -s "$(command -v aws)" ~/.ci-fallback/bin/`)
instead of adding a personal bin directory.

Each slot's runner gets its own empty `HOME` (`~/.ci-fallback/slots/<n>/_home`, mode 0700),
wiped before and after every job. So parallel jobs can't clobber each other's `~/setup-pnpm`
(pnpm/action-setup), and jobs don't pick up your `~/.npmrc`, `~/.aws`, `gh` or `wrangler`
logins: as on GitHub-hosted runners, credentials come only from the workflow's secrets. Tools
that cache under `~` (pnpm, Maestro, CocoaPods) download again per job. This is not a sandbox:
jobs still run as your user, and only same-repo (non-fork) runs are picked up.

GitHub can report a job that exceeds `timeout-minutes` as `cancelled`. For a cancelled job,
`rerun.yml` reads its check annotations and requires GitHub's maximum-execution-time message.
Manual and concurrency cancellations are terminal. The notebook daemon only provisions runners.
