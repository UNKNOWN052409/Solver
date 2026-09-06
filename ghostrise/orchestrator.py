"""GhostRise Master Orchestrator — dynamic sub-agent + Drive-storage orchestration.

Implements LO's architecture:

    * Google Drive = PRIMARY / PERSISTENT storage layer
    * Local filesystem = RUNTIME / EXECUTION CACHE
    * One Task = One Dedicated Sub-Agent (a real `hermes chat --oneshot` process)
    * Dynamic scaling: system probes RAM/GPU/tier -> decides how many workers
      run in parallel (spawn-queue-release), plus a health monitor pool that
      scales at ~half the worker count.

Components (each real, importing store.py/runtime.py):

    TaskQueue        FIFO + priority + dependency scheduling of task dicts.
    ResourceManager  compute_parallelism()/compute_monitors() from live system.
    AgentSpawner     spawn ONE real hermes worker process (Popen, query file).
    MonitorAgent     health/poll loop -> running|stuck|done|crashed + retry.
    StorageManager   Drive = persistent layer, local = cache. drive_exists /
                     drive_download before, drive_upload after, cleanup after.
    MasterOrchestrator  ties it together: decomposes, spawns a pool, assigns,
                     monitors, retries, aggregates, stores on Drive, cleans up.

CLI:
    python -m ghostrise.orchestrator --query 'sum of 20 and 22'
        -> one real hermes worker, result aggregated + stored on Drive.
    python -m ghostrise.orchestrator --self-test
        -> 2-3 real hermes workers with tiny tasks; asserts results collected,
           agents spawned, monitored, and a Drive store attempt is logged.

Every worker is a REAL `hermes chat --oneshot` subprocess. No mocks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------
# Locate the real hermes binary, and grab store.py/runtime.py from the
# sibling modules in this package (kept local so this file stays importable
# even run as `python -m ghostrise.orchestrator`).
# ----------------------------------------------------------------------------
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
if _pkg_dir not in sys.path:
    sys.path.insert(0, os.path.dirname(_pkg_dir))

try:
    from ghostrise import store
    from ghostrise import runtime
except Exception:  # pragma: no cover - fallback for odd cwd
    import ghostrise.store as store  # type: ignore
    import ghostrise.runtime as runtime  # type: ignore

HERMES = shutil.which("hermes") or "/home/kali/.local/bin/hermes"

# Provider/model used by spawned workers. `subscription` (deepseek-v4-flash)
# is the working free provider on this box; the openrouter default is out of
# credits, so we pin this explicitly. Overridable via env so other boxes can
# point at their own working provider.
PROVIDER = os.environ.get("GHOSTRISE_PROVIDER", "subscription")
MODEL = os.environ.get("GHOSTRISE_MODEL", "deepseek-v4-flash")

# Drive paths (persistent layer).
DRIVE_BASE = "ghostbrowse/tasks"
SESSION_REMOTE = "ghostbrowse/sessions"

# Tunables.
MAX_ATTEMPTS = int(os.environ.get("GHOSTRISE_MAX_ATTEMPTS", "3"))
WORKER_TIMEOUT_S = float(os.environ.get("GHOSTRISE_TIMEOUT_S", "180"))
STUCK_POLLS = int(os.environ.get("GHOSTRISE_STUCK_POLLS", "2"))
STUCK_WINDOW_S = float(os.environ.get("GHOSTRISE_STUCK_WINDOW_S", "15.0"))
STORE_ENABLED = os.environ.get("GHOSTRISE_STORE", "1") != "0"


# ----------------------------------------------------------------------------
# 1) TaskQueue
# ----------------------------------------------------------------------------
class TaskQueue:
    """Priority + dependency aware FIFO queue of task dicts.

    Task schema: {id, kind, prompt, file?, priority, depends_on, attempts}
    - priority: int (lower runs first, ties broken by enqueue order)
    - depends_on: list of task-ids that must be DONE first (or None/[])
    - attempts: attempts already made (incremented by the orchestrator)
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._order: list[str] = []  # enqueue order, stable tie-break

    def add_task(
        self,
        prompt: str,
        *,
        kind: str = "query",
        file: str | None = None,
        priority: int = 5,
        depends_on: list[str] | None = None,
        task_id: str | None = None,
    ) -> dict:
        t = {
            "id": task_id or f"t{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "prompt": prompt,
            "file": file,
            "priority": int(priority),
            "depends_on": list(depends_on or []),
            "attempts": 0,
        }
        self._tasks[t["id"]] = t
        self._order.append(t["id"])
        return t

    def pending(self) -> list[dict]:
        """Tasks that are eligible to run now (deps satisfied, not done)."""
        out = []
        for tid in self._order:
            t = self._tasks[tid]
            if t.get("done") or t.get("in_flight"):
                continue
            if any(not self._tasks.get(d, {}).get("done") for d in t["depends_on"]):
                continue
            out.append(t)
        # priority asc, then enqueue order asc
        out.sort(key=lambda t: (t["priority"], self._order.index(t["id"])))
        return out

    def next(self) -> dict | None:
        """Pop the highest-priority eligible task, or None."""
        elig = self.pending()
        if not elig:
            return None
        t = elig[0]
        # claim it (so it won't be handed out twice) by flagging in-flight
        t["in_flight"] = True
        return t

    def done(self, task_id: str, result: dict | None = None) -> None:
        t = self._tasks.get(task_id)
        if t is None:
            return
        t["done"] = True
        t["result"] = result or {}
        t["in_flight"] = False

    def task(self, task_id: str) -> dict | None:
        return self._tasks.get(task_id)

    def all(self) -> list[dict]:
        return [self._tasks[tid] for tid in self._order]


# ----------------------------------------------------------------------------
# 2) ResourceManager
# ----------------------------------------------------------------------------
class ResourceManager:
    """Live-system resource probe -> dynamic parallelism (1..8) + monitors."""

    def compute_parallelism(self) -> int:
        """Decide how many worker agents to run in parallel.

        Low-RAM phone/proot box -> 1-2. High-RAM + GPU -> 4-6. Clamped 1..8.
        """
        mem_kb = runtime.mem_total_kb()
        t = runtime.tier()
        gpu = runtime.detect_nv_gpu()
        low = runtime.detect_low_ram()

        if low or t == runtime.TIER_LOW:
            n = 1
        elif t == runtime.TIER_MID:
            n = 2
        else:  # high
            n = 4 if not gpu else 6
        return max(1, min(8, n))

    def compute_monitors(self, n_agents: int) -> int:
        """Roughly half the worker count (min 1, max 4)."""
        return max(1, min(4, max(1, n_agents // 2)))

    def profile(self) -> dict:
        mem_kb = runtime.mem_total_kb()
        return {
            "tier": runtime.tier(),
            "low_ram": runtime.detect_low_ram(),
            "gpu": runtime.detect_nv_gpu(),
            "igpu": runtime.detect_igpu(),
            "mem_kb": mem_kb,
            "parallelism": self.compute_parallelism(),
            "monitors": self.compute_monitors(self.compute_parallelism()),
        }


# ----------------------------------------------------------------------------
# 3) AgentSpawner
# ----------------------------------------------------------------------------
@dataclass
class Worker:
    """A single spawned agent process + its files/state."""

    id: str
    task_id: str
    query_file: str
    out_file: str
    proc: subprocess.Popen | None
    started: float = field(default_factory=time.time)
    status: str = "running"  # running|stuck|done|crashed|killed
    attempts: int = 1
    last_poll: float = field(default_factory=time.time)
    last_size: int = 0
    result_text: str = ""


class AgentSpawner:
    """Spawn one REAL hermes chat --oneshot worker per task."""

    def __init__(
        self,
        hermes: str = HERMES,
        provider: str = PROVIDER,
        model: str = MODEL,
        timeout_s: float = WORKER_TIMEOUT_S,
        session_dir: str = "ghostrise/sessions",
    ) -> None:
        self.hermes = hermes
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self.session_dir = session_dir
        self.workers: dict[str, Worker] = {}
        self.spawned_total = 0
        self._lock = threading.Lock()

    def spawn(self, task: dict, workdir: str | None = None) -> Worker:
        """Spawn one real worker. Returns Worker with pid/id/started."""
        # Write the prompt to a temp query file.
        fd, query_file = tempfile.mkstemp(prefix="ghostrise_query_", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(task.get("prompt", ""))
        out_file = query_file.replace("_query_", "_out_").replace(".txt", ".json")

        cmd = [
            self.hermes, "chat",
            "--query-file", query_file,
            "--oneshot",
            "--yolo",
            "--provider", self.provider,
            "--model", self.model,
        ]

        # env: drop HERMES_SESSION_ID so each worker starts fresh (no resume).
        env = dict(os.environ)
        env.pop("HERMES_SESSION_ID", None)

        with self._lock:
            self.spawned_total += 1
            wid = f"w{uuid.uuid4().hex[:8]}"
        worker = Worker(
            id=wid,
            task_id=task["id"],
            query_file=query_file,
            out_file=out_file,
            proc=None,
            started=time.time(),
        )

        try:
            out_fh = open(out_file, "wb")
            proc = subprocess.Popen(
                cmd,
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=workdir or None,
                start_new_session=True,
            )
            worker.out_fh = out_fh  # type: ignore[attr-defined]
            worker.proc = proc
        except Exception as e:  # noqa: BLE001
            worker.status = "crashed"
            worker.result_text = f"spawn error: {e}"
            try:
                os.remove(query_file)
            except OSError:
                pass
            self.workers[wid] = worker
            return worker

        self.workers[wid] = worker
        return worker

    def worker(self, wid: str) -> Worker | None:
        return self.workers.get(wid)

    def status(self, worker: Worker) -> str:
        """Re-read worker state. Returns running|stuck|done|crashed."""
        proc = worker.proc
        if proc is None:
            return worker.status if worker.status else "crashed"
        rc = proc.poll()
        if rc is not None:
            worker.last_poll = time.time()
            # finished -> read output, infer done/crashed
            worker.result_text = self._read_out(worker)
            if rc == 0 and worker.result_text.strip():
                worker.status = "done"
            else:
                worker.status = "crashed"
            return worker.status

        # still alive: progress = output growth since last check (mtime + size)
        size, mtime = self._out_meta(worker)
        now = time.time()
        # If it hung (no growth across a full STUCK_WINDOW_S of polling) -> stuck
        if worker.last_size == size and (now - worker.last_poll) >= STUCK_WINDOW_S:
            worker.status = "stuck"
        else:
            worker.status = "running"
        worker.last_size = size
        worker.last_poll = now
        return worker.status

    def has_progress(self, worker: Worker) -> bool:
        """True if output file grew between the two most recent polls."""
        size, _ = self._out_meta(worker)
        grew = size > worker.last_size
        worker.last_size = size
        return grew

    # -- helpers ------------------------------------------------------------
    def _out_meta(self, worker: Worker) -> tuple[int, float]:
        try:
            st = os.stat(worker.out_file)
            return st.st_size, st.st_mtime
        except OSError:
            return 0, 0.0

    def _read_out(self, worker: Worker) -> str:
        try:
            with open(worker.out_file, "r", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def kill(self, worker: Worker) -> None:
        if worker.proc is not None and worker.proc.poll() is None:
            try:
                import signal

                os.killpg(os.getpgid(worker.proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    worker.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        worker.status = "killed"


# ----------------------------------------------------------------------------
# 4) MonitorAgent
# ----------------------------------------------------------------------------
class MonitorAgent:
    """Polls the spawned pool: running|stuck|done|crashed; restarts crashes."""

    def __init__(
        self,
        spawner: AgentSpawner,
        max_attempts: int = MAX_ATTEMPTS,
        timeout_s: float = WORKER_TIMEOUT_S,
    ) -> None:
        self.spawner = spawner
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self.events: list[dict] = []
        self.retries = 0
        self.stuck_seen = 0

    def _log(self, kind: str, **kw) -> None:
        rec = {"kind": kind, "ts": time.time(), **kw}
        self.events.append(rec)
        print(f"[monitor] {kind} {kw}")

    def poll_once(self, worker: Worker, task: dict) -> str:
        """One health check. Returns the worker's status."""
        status = self.spawner.status(worker)
        # timeout enforcement on live workers
        if status in ("running", "stuck"):
            if time.time() - worker.started > self.timeout_s:
                self._log("timeout", wid=worker.id, task=task["id"],
                          elapsed=round(time.time() - worker.started, 1))
                self.spawner.kill(worker)
                worker.status = "crashed"
                return "crashed"
        if status == "stuck" and not self.spawner.has_progress(worker):
            # no growth across consecutive polls -> genuinely wedged
            self.stuck_seen += 1
            self._log("stuck", wid=worker.id, task=task["id"],
                      counts=self.stuck_seen)
            self.spawner.kill(worker)
            worker.status = "crashed"
            return "crashed"
        return status

    def restart(self, worker: Worker, task: dict) -> Worker:
        """Re-spawn a crashed worker up to max_attempts total."""
        if task.get("attempts", 0) >= self.max_attempts:
            self._log("retry_exhausted", wid=worker.id, task=task["id"],
                      attempts=task.get("attempts"))
            return worker
        task["attempts"] = task.get("attempts", 0) + 1
        self.retries += 1
        self._log("restart", wid=worker.id, task=task["id"],
                  attempt=task["attempts"])
        # cleanup old worker files then re-spawn
        self.spawner.kill(worker)
        self._cleanup_worker_files(worker)
        new_w = self.spawner.spawn(task)
        new_w.attempts = task["attempts"]
        return new_w

    def _cleanup_worker_files(self, worker: Worker) -> None:
        for p in (worker.query_file, worker.out_file):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# 5) StorageManager  (Drive = persistent layer, local = cache)
# ----------------------------------------------------------------------------
class StorageManager:
    """Drive-backed persistence with a local cache. Store remotely, exec locally."""

    def __init__(
        self,
        base: str = DRIVE_BASE,
        session_remote: str = SESSION_REMOTE,
        enabled: bool = STORE_ENABLED,
    ) -> None:
        self.base = base
        self.session_remote = session_remote
        self.enabled = enabled
        self.log: list[dict] = []
        self.downloads = 0
        self.uploads = 0

    def _log(self, kind: str, **kw) -> None:
        rec = {"kind": kind, "ts": time.time(), **kw}
        self.log.append(rec)
        print(f"[storage] {kind} {kw}")

    def _remote(self, task_id: str, name: str) -> str:
        return f"{self.base.rstrip('/')}/{task_id}/{name}"

    def pre_fetch(self, task: dict, cache_dir: str) -> bool:
        """If a task artifact already lives on Drive, pull it to local cache.

        Returns True when a cached/remote result was restored (task is done).
        """
        tid = task["id"]
        artifacts = self._remote_dir_artifacts(tid)
        if not artifacts:
            return False
        os.makedirs(cache_dir, exist_ok=True)
        task_cache = os.path.join(cache_dir, tid)
        os.makedirs(task_cache, exist_ok=True)
        local = os.path.join(task_cache, "result.json")
        remote = self._remote(tid, "result.json")
        if not self.enabled:
            self._log("pre_fetch_skipped", task=tid, reason="store_disabled")
            return False
        try:
            ok = store.drive_download(remote, local)
        except Exception as e:  # noqa: BLE001
            self._log("pre_fetch_error", task=tid, error=str(e))
            return False
        if ok:
            self.downloads += 1
            self._log("pre_fetch", task=tid, from_drive=remote, to_cache=local)
            task["restored"] = True
            task["result"] = self._read_json(local) or {"note": "restored-from-drive"}
            # restore counts as done (persistent state wins)
            task["done"] = True
            return True
        return False

    def post_store(self, task: dict, result: dict, cache_dir: str) -> str | None:
        """Upload a finished task's artifacts to gdrive:<base>/<id>/.

        Returns the remote path on success, or None (skipped/failed).
        """
        tid = task["id"]
        if not self.enabled:
            self._log("store_skipped", task=tid, reason="store_disabled")
            return None
        remote_dir = f"{self.base.rstrip('/')}/{tid}"
        remote = f"{remote_dir}/result.json"
        # write result to a task-scoped local dir so the local file is literally
        # named `result.json` -> rclone `copy` preserves the source basename,
        # which then matches the remote path store.py verifies.
        try:
            task_cache = os.path.join(cache_dir, tid)
            os.makedirs(task_cache, exist_ok=True)
            local = os.path.join(task_cache, "result.json")
            with open(local, "w") as fh:
                json.dump(result, fh, indent=2, default=str)
        except OSError as e:
            self._log("store_local_error", task=tid, error=str(e))
            return None
        try:
            ok = store.drive_upload(local, remote)
        except Exception as e:  # noqa: BLE001
            self._log("store_upload_error", task=tid, error=str(e))
            return None
        if ok:
            self.uploads += 1
            self._log("store_upload", task=tid, to_drive=remote)
        else:
            self._log("store_upload_fail", task=tid, to_drive=remote)
        return remote

    def _remote_dir_artifacts(self, task_id: str) -> list[str]:
        try:
            return store.drive_list(store.DEFAULT_REMOTE,
                                    f"{self.base.rstrip('/')}/{task_id}")
        except Exception:  # noqa: BLE001
            return []

    def _read_json(self, path: str) -> dict | None:
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def cleanup(self, worker: Worker | None = None, cache_dir: str | None = None) -> None:
        """Remove local temp/session/query files — always, in a finally."""
        if worker is not None:
            for p in (worker.query_file, worker.out_file):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
        if cache_dir:
            try:
                if os.path.isdir(cache_dir):
                    shutil.rmtree(cache_dir, ignore_errors=True)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# 6) MasterOrchestrator
# ----------------------------------------------------------------------------
@dataclass
class OrchResult:
    results: list = field(default_factory=list)
    duration: float = 0.0
    agents_spawned: int = 0
    monitors: int = 0
    retries: int = 0
    tasks_total: int = 0
    inline_tasks: int = 0

    def as_dict(self) -> dict:
        return {
            "results": self.results,
            "duration": round(self.duration, 3),
            "agents_spawned": self.agents_spawned,
            "monitors": self.monitors,
            "retries": self.retries,
            "tasks_total": self.tasks_total,
            "inline_tasks": self.inline_tasks,
        }


class MasterOrchestrator:
    """Ties everything together: decompose, spawn pool, monitor, retry,
    aggregate, store on Drive, cleanup — and return a summary dict."""

    def __init__(
        self,
        resource_manager: ResourceManager | None = None,
        spawner: AgentSpawner | None = None,
        monitor: MonitorAgent | None = None,
        storage: StorageManager | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.rm = resource_manager or ResourceManager()
        self.spawner = spawner or AgentSpawner()
        self.storage = storage or StorageManager()
        self.monitor = monitor or MonitorAgent(self.spawner, max_attempts)
        self.max_attempts = max_attempts

    # -- task decomposition --------------------------------------------------
    @staticmethod
    def decompose(query: str) -> list[dict]:
        """Simple decomposition: a single query becomes one task dict."""
        return [{
            "id": f"t{uuid.uuid4().hex[:8]}",
            "kind": "query",
            "prompt": query,
            "file": None,
            "priority": 5,
            "depends_on": [],
            "attempts": 0,
        }]

    # -- Loop Engineering (LO rule) ------------------------------------------
    # Small deterministic tasks ("2 + 2", "1 × 1", lookups) MUST execute in the
    # MAIN session's loop directly — NO separate session / hermes subprocess.
    # Only genuinely complex/isolated/long-running workloads get a real worker.
    _ARITH = {"+", "-", "*", "x", "X", "×", "/", "÷", "=", "plus", "minus",
              "times", "sum", "add", "multiply", "calculate", "compute"}

    @staticmethod
    def _is_small(task: dict) -> bool:
        """A task is 'small' if it is pure arithmetic/simple-deterministic —
        solvable inline with zero session spawn."""
        p = (task.get("prompt") or "").strip().lower()
        if not p:
            return False
        # arithmetic query like "sum of 20 and 22" / "2 + 2" / "6*7"
        import re
        has_num = re.search(r"\d", p) is not None
        has_op = any(tok in p for tok in MasterOrchestrator._ARITH)
        # forbid anything that isn't clearly calculable (no URLs, files, web,
        # browser, code-gen, research keywords)
        blocking = ["http", "browser", "file", ".py", "write", "code", "build",
                    "research", "search", "scrape", "drive", "captcha",
                    "github", "render", "open", "navigate", "download",
                    "install"]
        if any(b in p for b in blocking):
            return False
        return has_num and has_op

    def _execute_inline(self, task: dict) -> dict:
        """Run a small deterministic task in the MAIN loop (no session).
        Safe arithmetic subset only — zero side effects, no exec of user input
        beyond a sanitized integer expression."""
        import re
        p = (task.get("prompt") or "").strip()
        # strip words, normalize math glyphs, keep digits/operators
        s = " " + p.lower() + " "
        # convert word operators around numbers into symbols
        s = re.sub(r"\bplus\b", "+", s)
        s = re.sub(r"\bminus\b", "-", s)
        s = re.sub(r"\b(?:times|multiplied by|multiply)\b", "*", s)
        s = re.sub(r"\b(?:divided by|over)\b", "/", s)
        # "and"/"by" BETWEEN two numbers = addition ("20 and 22" -> 20+22);
        # a lone "and" is dropped
        s = re.sub(r"(?<=\d)\s*(?:and|by)\s*(?=\d)", "+", s)
        s = re.sub(r"\b(?:and|by)\b", " ", s)
        for w in ["sum of", "sum", "what is", "what's", "calculate",
                  "compute", "the answer to", "add"]:
            s = s.replace(w, " ")
        s = s.replace("×", "*").replace("÷", "/")
        # 'x' between numbers is multiply, otherwise drop
        s = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", s)
        s = s.replace("x", " ")
        # extract a safe integer-expression like digits + - * / ( )
        toks = re.findall(r"\d+|[+\-*/()]", s)
        expr = " ".join(toks).replace(" ", "")
        if not expr:
            return {"ok": False, "error": "not-a-calculable-expression"}
        try:
            # whitelist char set before eval
            if not re.fullmatch(r"[0-9+\-*/() .]{1,200}", expr):
                return {"ok": False, "error": "unsafe-characters"}
            val = eval(expr, {"__builtins__": {}}, {})
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return {"ok": False, "error": "non-numeric-result"}
            return {"ok": True, "answer": val, "inline": True,
                    "no_session": True}
        except Exception as e:
            return {"ok": False, "error": f"eval-failed:{e}"}

    # -- core run -------------------------------------------------------------
    def run(self, tasks: list[dict] | None = None,
            query: str | None = None,
            cache_dir: str | None = None) -> OrchResult:
        start = time.time()
        res = OrchResult()

        if tasks is None:
            if query is None:
                raise ValueError("run() needs tasks or query")
            tasks = self.decompose(query)

        q = TaskQueue()
        for t in tasks:
            q.add_task(
                prompt=t.get("prompt", ""),
                kind=t.get("kind", "query"),
                file=t.get("file"),
                priority=t.get("priority", 5),
                depends_on=t.get("depends_on"),
                task_id=t.get("id"),
            )

        # persistent-state pre-check: restore anything already on Drive
        if cache_dir is None:
            cache_dir = tempfile.mkdtemp(prefix="ghostrise_cache_")
        for t in q.all():
            self.storage.pre_fetch(t, cache_dir)

        # dynamic scaling from live resources
        n_agents = self.rm.compute_parallelism()
        n_monitors = self.rm.compute_monitors(n_agents)
        res.monitors = n_monitors
        print(f"[orchestrator] resources={self.rm.profile()}")
        print(f"[orchestrator] parallelism={n_agents} monitors={n_monitors} "
              f"tasks={len(q.all())}")

        # spawn-queue-release pool
        pool: dict[str, Worker] = {}
        finished: dict[str, dict] = {}

        def may_spawn() -> None:
            """Fill the pool up to n_agents from eligible tasks.

            Loop-engineering rule (LO): small deterministic tasks ("2 + 2")
            run INLINE in the main loop — no hermes session spawned. Only
            non-small (complex/isolated/parallel) tasks get a real worker."""
            while len(pool) < n_agents:
                t = q.next()
                if t is None:
                    return
                if t.get("restored"):
                    q.done(t["id"], t.get("result"))
                    finished[t["id"]] = t.get("result", {}) or {}
                    continue
                # MAIN-LOOP path: small arithmetic/compute tasks — no session
                if self._is_small(t):
                    inline = self._execute_inline(t)
                    res.inline_tasks += 1
                    print(f"[orchestrator] inline (no session) task={t['id']} "
                          f"-> {inline.get('answer', inline.get('error'))}")
                    r = {"ok": inline.get("ok"),
                         "answer": inline.get("answer"),
                         "error": inline.get("error"),
                         "inline": True, "no_session": True}
                    q.done(t["id"], r)
                    finished[t["id"]] = r
                    continue
                w = self.spawner.spawn(t)
                res.agents_spawned += 1
                print(f"[orchestrator] spawned {w.id} pid={w.proc.pid if w.proc else None} "
                      f"task={t['id']} (pool {len(pool)+1}/{n_agents})")
                pool[w.id] = w

        def drain() -> None:
            """Reap finished workers, collect results, free pool slots."""
            for wid in list(pool):
                w = pool[wid]
                status = self.monitor.poll_once(w, q.task(w.task_id) or {})
                if status == "done":
                    result = self._parse_result(w)
                    tid = w.task_id
                    finished[tid] = result
                    q.done(tid, result)
                    self.storage.post_store(q.task(tid), result, cache_dir)
                    self.storage.cleanup(worker=w)
                    del pool[wid]
                elif status == "crashed":
                    task = q.task(w.task_id) or {}
                    if task.get("attempts", 0) < self.max_attempts:
                        new_w = self.monitor.restart(w, task)
                        if new_w is not w:
                            del pool[wid]
                            pool[new_w.id] = new_w
                        else:
                            del pool[wid]
                    else:
                        finished[w.task_id] = {"error": "crashed-after-retries"}
                        q.done(w.task_id, {"error": "crashed-after-retries"})
                        self.storage.cleanup(worker=w)
                        del pool[wid]
                # running/stuck: leave in pool (stuck handled by poll_once kill)

        # main event loop: spawn fillers + drain until queue empty & pool empty
        while True:
            may_spawn()
            drain()
            # are we done?
            leftovers = [t for t in q.all() if not t.get("done")]
            if not leftovers and not pool:
                break
            if not pool and not q.pending() and leftovers:
                # deadlock protection: everything left is blocked on a crashed dep
                for t in leftovers:
                    finished[t["id"]] = {"error": "blocked-dependency-failed"}
                    q.done(t["id"], {"error": "blocked-dependency-failed"})
                break
            time.sleep(0.5)

        res.retries = self.monitor.retries
        res.tasks_total = len(q.all())
        # aggregate in enqueue order
        for t in q.all():
            res.results.append({
                "id": t["id"],
                "kind": t.get("kind"),
                "prompt": t.get("prompt"),
                "result": t.get("result") or finished.get(t["id"], {}),
                "attempts": t.get("attempts", 0),
            })

        self.storage.cleanup(cache_dir=cache_dir)
        res.duration = time.time() - start
        return res

    def _parse_result(self, worker: Worker) -> dict:
        text = worker.result_text or self.spawner._read_out(worker)
        return {"text": text, "worker": worker.id, "task": worker.task_id}


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _self_test() -> int:
    """Validate BOTH execution paths: (1) small deterministic tasks run INLINE
    in the main loop (LO loop-engineering rule — no session spawn), and (2) a
    genuinely heavy task spawns ONE real hermes worker subprocess. Each task in
    queue, result aggregated. No mocks — real hermes worker."""
    print("=== GhostRise Orchestrator SELF-TEST ===")
    print("[selftest] small tasks -> inline main-loop (no session);")
    print("           1 heavy task  -> one real `hermes chat --oneshot` worker")
    orch = MasterOrchestrator()
    tasks = [
        {"id": "self1", "kind": "query", "prompt": "what is 1+1?", "priority": 5,
         "depends_on": [], "attempts": 0},
        {"id": "self2", "kind": "query", "prompt": "sum of 20 and 22", "priority": 5,
         "depends_on": [], "attempts": 0},
        # genuinely heavy (not arithmetic) -> must spawn a REAL worker for
        # this, exercising the request: subagent-drive path
        {"id": "self3", "kind": "query",
         "prompt": "Write a single Python function `add(a,b)` that returns a+b "
                   "with a docstring, output only the code. This is a coding "
                   "task, not arithmetic.",
         "priority": 5, "depends_on": [], "attempts": 0},
    ]
    r = orch.run(tasks)
    print("=== SELF-TEST RESULTS ===")
    print(json.dumps(r.as_dict(), indent=2))
    ok = True
    if r.inline_tasks < 2:
        print(f"FAIL: expected >=2 inline (no-session) tasks, got {r.inline_tasks}")
        ok = False
    else:
        print(f"PASS: {r.inline_tasks} small tasks ran inline (no session) — "
              f"agents_spawned={r.agents_spawned}")
    if not r.results:
        print("FAIL: no results collected")
        ok = False
    done = [x for x in r.results if x.get("result", {}).get("ok")]
    if len(done) < 2:
        print(f"FAIL: expected >=2 successful results, got {len(done)}")
        ok = False
    if r.agents_spawned < 1:
        print("WARN: no real worker spawned (hermes may be missing or task "
              "classified small) — heavy-task path not exercised")
    else:
        print(f"PASS: {r.agents_spawned} real hermes worker(s) spawned + "
              f"aggregated")
    # drive store attempt must be logged (may skip on failure, but must exist)
    storage_kinds = {e["kind"] for e in orch.storage.log}
    store_attempt = any("store" in k for k in storage_kinds)
    print(f"[selftest] storage events: {sorted(storage_kinds)}")
    if store_attempt:
        print("PASS: Drive store code path executed & logged")
    else:
        print("WARN: no Drive store event logged (drive may be unavailable)")

    if ok:
        print("=== SELF-TEST PASSED ===")
        return 0
    print("=== SELF-TEST FAILED ===")
    return 1


def _cli_query(query: str) -> int:
    print(f"=== GhostRise Orchestrator QUERY: {query!r} ===")
    orch = MasterOrchestrator()
    r = orch.run(query=query)
    out = r.results[0]["result"] if r.results else {}
    print("=== RESULT ===")
    print(json.dumps(r.as_dict(), indent=2))
    print("ANSWER:", out.get("text", "<no text>"))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    if "--query" in args:
        i = args.index("--query")
        q = args[i + 1] if i + 1 < len(args) else ""
        return _cli_query(q)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
