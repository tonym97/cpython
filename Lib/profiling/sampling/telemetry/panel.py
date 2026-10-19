"""Live TUI panel for telemetry plugins."""

from typing import NamedTuple

# Change detection threshold, matching the CPU table's TrendTracker.
_CHANGE_THRESHOLD = 0.001


class Segment(NamedTuple):
    """A run of text in one style on a panel line.

    Styles map onto the TUI colour scheme: ``header`` and ``sorted`` (column
    headers), ``func``, ``file`` and ``samples`` (the CPU table's function,
    file:line and sample-count colours), ``good``, ``warn`` and ``hot``
    (green, yellow, red), ``cyan``, ``magenta``, ``trend_up``,
    ``trend_down``, ``bold``, ``dim`` and ``normal``. Unknown styles render
    as normal text.
    """

    text: str
    style: str = "normal"


class TelemetryPanel:
    """Base class for a plugin's live panel.

    The panel receives helper events through ``ingest`` and renders one view
    per mode through ``render_lines``. ``observe_sample`` is called for every
    CPU sample, which lets a panel relate external events (for example GPU
    launches, which carry an OS thread id and a timestamp) to the Python code
    running at the time. Subclasses that override ``__init__`` call
    ``super().__init__()``.
    """

    panel_modes = ("default",)
    # Keys the panel acts on in handle_key, as (key, description) or
    # (key, description, label) tuples: the description goes on the help
    # screen, the label (one word; default: the description's first word)
    # tags the key in the footer as 'e':label. Keys the TUI itself uses need
    # no listing: they still reach the panel, after the TUI has acted on them.
    key_bindings = ()

    def __init__(self):
        # Trend colours follow the TUI's toggle (x), which handle_key sees.
        # Starts on, like the TUI's own trend tracker.
        self.trends_enabled = True
        self._trend_values = {}

    def handle_key(self, key):
        """Called with every key the live TUI receives, after the TUI's own handling.

        ``key`` is the pressed character, or ``None`` for keys with no
        character (arrows). The base keeps ``trends_enabled`` in step with the
        TUI's ``x`` toggle; subclasses that override call ``super()``.
        """
        if key in ("x", "X"):
            self.trends_enabled = not self.trends_enabled

    def ingest(self, event_type, payload):
        """Consume one helper event."""
        raise NotImplementedError

    def render_lines(self, mode, width):
        """Return the panel body for ``mode``: one entry per line.

        Each entry is a string or a list of ``Segment`` (plain
        ``(text, style)`` tuples are accepted). Consumers without colour
        join the segment texts.
        """
        raise NotImplementedError

    def observe_sample(self, stack_frames, timestamp_ns):
        """Called for every CPU sample.

        ``stack_frames`` is what ``Collector.collect`` receives; walk it with
        ``iter_thread_frames`` and read frames with ``frame_location`` rather
        than relying on its structure. ``timestamp_ns`` is
        ``time.perf_counter_ns()`` when the unwind started.
        """

    def trend_style(self, key, metric, value, fallback="normal", version=None):
        """Style for a numeric cell, honouring the trend toggle.

        Tracks ``value`` per ``(key, metric)`` across renders like the CPU
        table's trend tracker and returns ``"trend_up"`` or ``"trend_down"``
        when it moved, else ``fallback``. With trends off it returns
        ``fallback`` but keeps tracking, so re-enabling does not flash every
        cell.

        The TUI redraws faster than telemetry arrives. With ``version``, a
        counter the panel bumps when new data lands, the trend is evaluated
        once per version and held until the next, so the colour stays on
        like the CPU table's instead of pulsing on one redraw.
        """
        value = float(value)
        slot = self._trend_values.get((key, metric))
        if slot is not None and version is not None and slot[0] == version:
            trend = slot[2]
        else:
            previous = slot[1] if slot is not None else value
            if value > previous + _CHANGE_THRESHOLD:
                trend = "up"
            elif value < previous - _CHANGE_THRESHOLD:
                trend = "down"
            else:
                trend = "stable"
            self._trend_values[(key, metric)] = (version, value, trend)
        if not self.trends_enabled:
            return fallback
        if trend == "up":
            return "trend_up"
        if trend == "down":
            return "trend_down"
        return fallback


def iter_thread_frames(stack_frames):
    """Yield ``(thread_id, frames)`` for each sampled thread with a stack.

    ``thread_id`` is the OS thread id (the Linux tid, which helpers can also
    report) and ``frames`` lists the thread's frames innermost first. This is
    the supported way to read the sample passed to ``observe_sample``.
    """
    for interpreter_info in stack_frames or ():
        for thread_info in getattr(interpreter_info, "threads", None) or ():
            thread_id = getattr(thread_info, "thread_id", None)
            frames = getattr(thread_info, "frame_info", None)
            if thread_id is not None and frames:
                yield thread_id, frames


def frame_location(frame):
    """``(funcname, filename, lineno)`` of a frame from ``iter_thread_frames``."""
    location = getattr(frame, "location", None)
    if location is None:
        lineno = getattr(frame, "lineno", 0) or 0
    elif isinstance(location, int):
        lineno = location
    else:
        try:
            lineno = int(location[0] or 0)
        except (TypeError, IndexError, ValueError):
            lineno = 0
    return getattr(frame, "funcname", ""), getattr(frame, "filename", ""), lineno


def line_text(line):
    """Plain text of a panel line (a string or a list of segments)."""
    if isinstance(line, str):
        return line
    return "".join(str(segment[0]) for segment in line)
