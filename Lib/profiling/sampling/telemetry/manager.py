"""Manage helper-process telemetry plugins.

One ``TelemetryHelperManager`` owns one helper subprocess. The helper prints
JSON lines ``{"plugin": id, "event_type": str, "payload": {...}}`` on stdout.
A reader thread parses them into a bounded queue so the profiler's sampling
loop only ever does a cheap non-blocking drain. Helper death is reported to
the collector as a synthetic ``health`` event carrying the exit status and the
helper's last stderr lines.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
import threading

from .plugin_registry import resolve_helper_command

MAX_QUEUED_EVENTS = 50_000
STDERR_TAIL_LINES = 20


class TelemetryHelperManager:
    """Single-plugin helper process manager."""

    def __init__(self, plugin_id, plugin_config=None):
        self.plugin_id = plugin_id
        self.plugin_config = dict(plugin_config or {})
        self.process = None
        self.command = None
        self._events = collections.deque(maxlen=MAX_QUEUED_EVENTS)
        self._dropped = 0
        self._lock = threading.Lock()
        self._stdout_thread = None
        self._stderr_thread = None
        self._stderr_tail = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._started = False
        self._exit_reported = False

    # ---- lifecycle ---------------------------------------------------------

    def _default_command(self):
        return [
            sys.executable,
            "-m",
            "profiling.sampling.telemetry",
            "--plugin",
            self.plugin_id,
            "--config-json",
            json.dumps(self.plugin_config, separators=(",", ":")),
        ]

    def start(self):
        if self._started:
            return
        self.command = resolve_helper_command(self.plugin_id, self.plugin_config) or self._default_command()
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
            self._push({
                "plugin": self.plugin_id,
                "event_type": "health",
                "payload": {"helper": "failed", "notes": [f"could not start helper: {exc}"], "command": self.command},
            })
            self._exit_reported = True
            self._started = True
            return
        self._stdout_thread = threading.Thread(target=self._read_stdout, name=f"telemetry-{self.plugin_id}-out", daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name=f"telemetry-{self.plugin_id}-err", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._started = True

    def stop(self):
        process = self.process
        if process is not None:
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
        self.process = None
        self._started = False

    # ---- event flow --------------------------------------------------------

    def _push(self, event):
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append(event)

    def _read_stdout(self):
        stream = self.process.stdout
        try:
            for line in stream:
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
        stream = self.process.stderr
        try:
            for line in stream:
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line[:300])
        except (OSError, ValueError):
            pass

    def poll(self):
        """Return and clear all queued events (never blocks)."""
        if not self._started:
            return []
        with self._lock:
            events = list(self._events)
            self._events.clear()
            dropped = self._dropped
            self._dropped = 0
        if dropped:
            events.append({
                "plugin": self.plugin_id,
                "event_type": "health",
                "payload": {"helper": "overflow", "notes": [f"dropped {dropped} helper events (consumer too slow)"]},
            })
        process = self.process
        if process is not None and not self._exit_reported and process.poll() is not None:
            self._exit_reported = True
            events.append({
                "plugin": self.plugin_id,
                "event_type": "health",
                "payload": {
                    "helper": "exited",
                    "returncode": process.returncode,
                    "stderr_tail": list(self._stderr_tail),
                    "command": self.command,
                },
            })
        return events

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None
