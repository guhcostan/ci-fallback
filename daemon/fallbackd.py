#!/usr/bin/env python3
"""ci-fallback daemon: on-demand ephemeral (JIT) GitHub Actions runners on this machine.

- Every POLL seconds, looks for queued jobs labelled `self-hosted` in the repos listed in
  REPOS_FILE (one owner/name per line). Polling uses ETags, so unchanged answers (HTTP 304)
  don't count against the API rate limit.
- For each such job it generates a JIT runner config and starts `run.sh --jitconfig` in a free
  slot directory (an APFS clone of the base runner install). The runner takes one job and exits.
- Every HEARTBEAT seconds it writes {"ts": <unix time>} to the heartbeat gist, which the
  `pick` reusable workflow reads to decide notebook vs GitHub-hosted.
- Idle JIT runners that never got a job (e.g. the job was cancelled) are removed after IDLE_TTL.
- Each runner gets its own empty HOME (<slot>/_home, mode 0700), wiped before and after every
  job. Actions that install into `~` (pnpm/action-setup's ~/setup-pnpm) then can't collide across
  parallel slots, and jobs don't silently pick up the owner's ~/.npmrc, ~/.aws, gh or wrangler
  logins: like on GitHub-hosted runners, credentials come only from the workflow's secrets.
  This is not a sandbox (jobs still run as this user); trust comes from the fork filter below.
- The slot's tool cache (_work/_tool) persists between jobs, unlike on GitHub-hosted runners. A job
  that ran `corepack enable` leaves pnpm/yarn shims in a cached Node's bin/, which then shadow the
  pnpm that pnpm/action-setup installs (a job asking for pnpm 9 ran pnpm 11). Before each job,
  shims that point into corepack are removed, so cached Nodes look as downloaded.

Config via env: CI_FALLBACK_HOME (default ~/.ci-fallback), RUNNER_BASE (~/actions-runner),
GIST_ID, MAX_SLOTS (4), POLL (15), LABELS (self-hosted,macOS,ARM64).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser(os.environ.get("CI_FALLBACK_HOME", "~/.ci-fallback"))
RUNNER_BASE = os.path.expanduser(os.environ.get("RUNNER_BASE", "~/actions-runner"))
REPOS_FILE = os.path.join(HOME, "repos.txt")
SLOTS_DIR = os.path.join(HOME, "slots")
GIST_ID = os.environ.get("GIST_ID", "2bb2e6b71187826dccbf96447e5a1494")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "4"))
POLL = int(os.environ.get("POLL", "15"))
HEARTBEAT = 60
IDLE_TTL = 600
REPOS_RELOAD = 300
LABELS = os.environ.get("LABELS", "self-hosted,macOS,ARM64").split(",")
def _host():
    try:
        return subprocess.check_output(["scutil", "--get", "LocalHostName"], text=True).strip()
    except Exception:
        return socket.gethostname().split(".")[0]


HOST = _host()
API = "https://api.github.com"


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


class GitHub:
    def __init__(self):
        self.token = None
        self.token_at = 0
        self.etags = {}  # url -> (etag, parsed body)

    def _token(self):
        if not self.token or time.time() - self.token_at > 3600:
            env_token = os.environ.get("GH_TOKEN")
            if env_token:
                self.token = env_token
            else:
                gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
                self.token = subprocess.check_output([gh, "auth", "token"], text=True).strip()
            self.token_at = time.time()
        return self.token

    def request(self, method, path, body=None, cache=False):
        url = path if path.startswith("http") else API + path
        headers = {
            "Authorization": "Bearer " + self._token(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-fallback",
        }
        if cache and url in self.etags:
            headers["If-None-Match"] = self.etags[url][0]
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                parsed = json.loads(raw) if raw else None
                if cache and resp.headers.get("ETag"):
                    self.etags[url] = (resp.headers["ETag"], parsed)
                return parsed
        except urllib.error.HTTPError as e:
            if e.code == 304 and cache:
                return self.etags[url][1]
            if e.code == 401:
                self.token = None
            raise


def _rmtree(root):
    """rmtree that also clears read-only trees jobs leave behind (e.g. Go's module cache).

    Only ever chmods real directories inside root; symlinks are removed, never followed.
    """
    def retry(func, path, exc_info):
        for p in (os.path.dirname(path), path):
            inside = os.path.commonpath([root, p]) == root
            if inside and not os.path.islink(p) and os.path.isdir(p):
                os.chmod(p, 0o700)
        func(path)
    shutil.rmtree(root, onerror=retry)


COREPACK_SHIMS = ("pnpm", "pnpx", "yarn", "yarnpkg")


def remove_corepack_shims(slot_dir):
    """Undo `corepack enable` in the slot's cached Nodes: _work/_tool/node/<ver>/<arch>/bin/<shim>.

    Every directory from the slot down to bin/ must be a real directory (no symlink), and must
    resolve inside the slot, so this can never delete anything outside it.
    """
    removed = []
    root = os.path.realpath(slot_dir)

    def real_dir_inside(path):
        return (not os.path.islink(path) and os.path.isdir(path)
                and os.path.commonpath([root, os.path.realpath(path)]) == root)

    node_root = slot_dir
    for part in ("_work", "_tool", "node"):
        node_root = os.path.join(node_root, part)
        if not real_dir_inside(node_root):
            return removed
    for version in os.listdir(node_root):
        vdir = os.path.join(node_root, version)
        if not real_dir_inside(vdir):
            continue
        for arch in os.listdir(vdir):
            adir = os.path.join(vdir, arch)
            bindir = os.path.join(adir, "bin")
            if not (real_dir_inside(adir) and real_dir_inside(bindir)):
                continue
            for name in COREPACK_SHIMS:
                p = os.path.join(bindir, name)
                if os.path.islink(p) and "corepack" in os.readlink(p).split(os.sep):
                    os.remove(p)
                    removed.append(p)
    return removed


class Slot:
    def __init__(self, index):
        self.index = index
        self.dir = os.path.join(SLOTS_DIR, str(index))
        self.proc = None
        self.repo = None
        self.job_id = None
        self.runner_id = None
        self.started = 0
        self.log = None

    def ensure_dir(self):
        if not os.path.exists(os.path.join(self.dir, "run.sh")):
            os.makedirs(SLOTS_DIR, exist_ok=True)
            shutil.rmtree(self.dir, ignore_errors=True)
            # APFS clone: instant and no extra disk space.
            subprocess.check_call(["cp", "-cR", RUNNER_BASE, self.dir])
            for leftover in (".runner", ".credentials", ".credentials_rsaparams", ".service", "_diag"):
                p = os.path.join(self.dir, leftover)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.remove(p)

    @property
    def home(self):
        return os.path.join(self.dir, "_home")

    def reset_home(self, create=True):
        """Empty this slot's private HOME. Refuses a symlinked HOME instead of following it."""
        home = self.home
        if os.path.islink(home):
            raise OSError("slot %d: %s is a symlink, refusing to use it" % (self.index, home))
        if os.path.lexists(home):
            _rmtree(home)
        if create:
            os.mkdir(home, 0o700)
            os.chmod(home, 0o700)
        return home

    @property
    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, gh, repo, job_id):
        self.ensure_dir()
        home = self.reset_home()
        try:
            for p in remove_corepack_shims(self.dir):
                log("slot %d: removed corepack shim %s" % (self.index, p))
        except OSError as e:  # contained: the job still runs, only with whatever shims remain
            log("slot %d: corepack shim cleanup failed: %r" % (self.index, e))
        name = "%s-%s-%d" % (HOST, self.index, int(time.time()))
        cfg = gh.request("POST", "/repos/%s/actions/runners/generate-jitconfig" % repo, {
            "name": name,
            "runner_group_id": 1,
            "labels": LABELS,
            "work_folder": "_work",
        })
        os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
        self.log = open(os.path.join(HOME, "logs", "slot-%d.log" % self.index), "a")
        self.log.write("\n==== %s %s job %s runner %s\n" % (time.ctime(), repo, job_id, name))
        self.log.flush()
        env = dict(os.environ)
        env.pop("GH_TOKEN", None)
        env["HOME"] = home
        self.proc = subprocess.Popen(
            [os.path.join(self.dir, "run.sh"), "--jitconfig", cfg["encoded_jit_config"]],
            cwd=self.dir, stdout=self.log, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
        self.repo, self.job_id, self.runner_id = repo, job_id, cfg["runner"]["id"]
        self.started = time.time()
        log("slot %d: started runner %s for %s job %s" % (self.index, name, repo, job_id))

    def reap(self):
        if self.proc is not None and self.proc.poll() is not None:
            log("slot %d: runner for %s job %s exited (%s)" % (self.index, self.repo, self.job_id, self.proc.returncode))
            self.proc = None
            if self.log:
                self.log.close()
                self.log = None
            try:
                self.reset_home(create=False)
            except OSError as e:
                log("slot %d: could not clean home: %r" % (self.index, e))

    def check_idle(self, gh):
        """Drop a runner that has waited IDLE_TTL without getting a job."""
        if not self.busy or time.time() - self.started < IDLE_TTL:
            return
        try:
            r = gh.request("GET", "/repos/%s/actions/runners/%s" % (self.repo, self.runner_id))
            if r.get("busy"):
                return
            log("slot %d: runner idle for %ds, removing" % (self.index, IDLE_TTL))
            gh.request("DELETE", "/repos/%s/actions/runners/%s" % (self.repo, self.runner_id))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        self.stop()

    def stop(self):
        if self.busy:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=30)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        self.reap()


def load_repos():
    try:
        with open(REPOS_FILE) as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def queued_self_hosted_jobs(gh, repo):
    jobs = []
    for status in ("queued", "in_progress"):
        runs = gh.request("GET", "/repos/%s/actions/runs?status=%s&per_page=30" % (repo, status), cache=True)
        for run in (runs or {}).get("workflow_runs", []):
            # Never run code from forks on this machine: a fork PR can edit its own workflow
            # to target `self-hosted`, so this check must live here, not only in pick.yml.
            head = (run.get("head_repository") or {}).get("full_name")
            if head != repo or run.get("event") == "pull_request_target":
                continue
            data =gh.request("GET", "/repos/%s/actions/runs/%s/jobs?filter=latest&per_page=100" % (repo, run["id"]), cache=True)
            for job in (data or {}).get("jobs", []):
                if job["status"] in ("queued", "waiting") and "self-hosted" in job.get("labels", []) \
                        and set(job["labels"]) <= set(LABELS):
                    jobs.append(job["id"])
    return jobs


def heartbeat(gh, slots):
    content = json.dumps({
        "ts": int(time.time()),
        "host": HOST,
        "busy": sum(1 for s in slots if s.busy),
        "slots": len(slots),
    })
    gh.request("PATCH", "/gists/" + GIST_ID, {"files": {"heartbeat.json": {"content": content}}})


def main():
    os.makedirs(HOME, exist_ok=True)
    gh = GitHub()
    slots = [Slot(i) for i in range(1, MAX_SLOTS + 1)]
    repos, repos_at, beat_at = [], 0, 0
    running = {"on": True}

    def shutdown(*_):
        running["on"] = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log("ci-fallback daemon starting: %d slots, labels %s" % (MAX_SLOTS, LABELS))
    while running["on"]:
        now = time.time()
        try:
            if now - repos_at > REPOS_RELOAD:
                repos, repos_at = load_repos(), now
            if now - beat_at > HEARTBEAT:
                heartbeat(gh, slots)
                beat_at = now
            for s in slots:
                s.reap()
                s.check_idle(gh)
            handled = {(s.repo, s.job_id) for s in slots if s.busy}
            for repo in repos:
                free = [s for s in slots if not s.busy]
                if not free:
                    break
                try:
                    pending = queued_self_hosted_jobs(gh, repo)
                except urllib.error.HTTPError as e:
                    log("poll %s: HTTP %s" % (repo, e.code))
                    continue
                for job_id in pending:
                    if (repo, job_id) in handled:
                        continue
                    free = [s for s in slots if not s.busy]
                    if not free:
                        break
                    free[0].start(gh, repo, job_id)
                    handled.add((repo, job_id))
        except Exception as e:  # network down, laptop just woke up, etc.
            log("loop error: %r" % (e,))
        for _ in range(POLL):
            if not running["on"]:
                break
            time.sleep(1)

    log("stopping runners")
    for s in slots:
        s.stop()


if __name__ == "__main__":
    sys.exit(main())
