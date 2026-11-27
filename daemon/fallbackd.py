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
- Each slot's runner gets its own HOME (<slot>/_home) that mirrors the real home through symlinks,
  except for the entries in SLOT_PRIVATE. Actions that install into `~` (pnpm/action-setup's
  ~/setup-pnpm) then get one copy per slot, so parallel jobs stop overwriting each other's install.

Config via env: CI_FALLBACK_HOME (default ~/.ci-fallback), RUNNER_BASE (~/actions-runner),
GIST_ID, MAX_SLOTS (4), POLL (15), LABELS (self-hosted,macOS,ARM64),
SLOT_PRIVATE (setup-pnpm: comma-separated home entries each slot keeps to itself).
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
REAL_HOME = os.path.expanduser("~")
SLOT_PRIVATE = {n for n in os.environ.get("SLOT_PRIVATE", "setup-pnpm").split(",") if n}
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

    def prepare_home(self):
        """Point every entry of the real home into <slot>/_home, except SLOT_PRIVATE.

        Run before each job, so entries created in the real home since the last job show up too.
        Files a job writes at the top of its home stay in the slot; anything under a symlinked
        entry (Library, .npm, .cache, ...) still lands in the real home, as before.
        """
        home = os.path.join(self.dir, "_home")
        os.makedirs(home, exist_ok=True)
        for name in os.listdir(home):
            p = os.path.join(home, name)
            if os.path.islink(p) and (name in SLOT_PRIVATE or not os.path.lexists(os.path.join(REAL_HOME, name))):
                os.remove(p)
        for name in os.listdir(REAL_HOME):
            if name in SLOT_PRIVATE:
                continue
            p = os.path.join(home, name)
            if not os.path.lexists(p):
                os.symlink(os.path.join(REAL_HOME, name), p)
        return home

    @property
    def busy(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, gh, repo, job_id):
        self.ensure_dir()
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
        env["HOME"] = self.prepare_home()
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
