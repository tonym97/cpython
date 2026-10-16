"""Helper process management for telemetry plugins.

One ``TelemetryHelperManager`` owns one helper subprocess. The helper prints
JSON lines ``{"plugin": id, "event_type": str, "payload": {...}}`` on
stdout. A reader thread parses them into a bounded queue so the profiler's
sampling loop only ever does a cheap non-blocking drain. Helper death is
reported as a synthetic ``health`` event carrying the exit status and the
helper's last stderr lines.
"""

import collections
import json
import subprocess
import threading

MAX_QUEUED_EVENTS = 50_000
STDERR_TAIL_LINES = 20


class TelemetryHelperManager:
    """Runs one plugin's helper ``command`` and queues the events it emits."""

    def __init__(self, plugin_id, command):
        self.plugin_id = plugin_id
        self.command = list(command)
        self.process = None
        self._events = collections.deque(maxlen=MAX_QUEUED_EVENTS)
        self._dropped = 0
        self._lock = threading.Lock()
        self._stdout_thread = None
        self._stderr_thread = None
        self._stderr_tail = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._started = False
        self._exit_reported = False

    # ---- lifecycle ----------------------------------------------------------

    def start(self):
        if self._started:
            return
        self._started = True
        try:
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._push(self._health({
                "helper": "failed",
                "notes": [f"could not start helper: {exc}"],
                "command": self.command,
            }))
            self._exit_reported = True
            return
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name=f"telemetry-{self.plugin_id}-out", daemon=True)
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name=f"telemetry-{self.plugin_id}-err", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def stop(self):
        """Terminate the helper and wait for its final output to be read."""
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._report_exit(process)

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    # ---- event flow ---------------------------------------------------------

    def _health(self, payload):
        return {"plugin": self.plugin_id, "event_type": "health", "payload": payload}

    def _push(self, event):
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append(event)

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._push(event)
        except (OSError, ValueError):
            pass

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line[:300])
        except (OSError, ValueError):
            pass

    def _report_exit(self, process):
        if self._exit_reported or process.poll() is None:
            return
        self._exit_reported = True
        self._push(self._health({
            "helper": "exited",
            "returncode": process.returncode,
            "stderr_tail": list(self._stderr_tail),
            "command": self.command,
        }))

    def poll(self):
        """Return and clear the queued events; never blocks.

        Works after ``stop()`` too, so the helper's shutdown reports can be
        drained once the reader thread has finished.
        """
        if self.process is not None:
            self._report_exit(self.process)
        with self._lock:
            events = list(self._events)
            self._events.clear()
            dropped, self._dropped = self._dropped, 0
        if dropped:
            events.append(self._health({
                "helper": "overflow",
                "notes": [f"dropped {dropped} helper events (consumer too slow)"],
            }))
        return events
