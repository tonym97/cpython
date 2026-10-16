"""Telemetry plugin interfaces."""

from __future__ import annotations

# Change detection threshold, matching the CPU table's TrendTracker.
_CHANGE_THRESHOLD = 0.001


class LiveTelemetryPlugin:
    """Base interface for telemetry plugins that render in the live TUI.

    A plugin receives helper events through ``ingest`` and renders one panel
    per mode through ``render_lines``. Optionally it can observe every CPU
    sample the profiler takes through ``observe_sample``; this is how a plugin
    attributes external events (for example GPU kernel launches, which carry an
    OS thread id and a wall-clock timestamp) to the Python code that was
    executing at the time.
    """

    plugin_id = "base"
    panel_modes = ("default",)
    # Set by the TUI before each render from its trend toggle (``x``); plugins
    # that colour changing values should honour it like the CPU table does.
    trends_enabled = False

    def ingest(self, event_type, payload):
        raise NotImplementedError

    def render_lines(self, mode, width):
        """Return the panel body: one entry per line.

        Each entry is a plain string or a list of ``(text, style)`` segments.
        Styles: ``header``, ``sorted``, ``bold``, ``dim``, ``normal``,
        ``good``, ``warn``, ``hot``, ``cyan``, ``magenta``, ``func``, ``file``,
        ``samples``, ``trend_up``, ``trend_down``. Unknown styles render as
        normal text; non-TUI consumers
        can join the segment texts.
        """
        raise NotImplementedError

    def trend_style(self, key, metric, value, fallback="normal", version=None):
        """Style for a numeric cell, honouring the TUI's trend toggle.

        Tracks ``value`` per ``(key, metric)`` like the CPU table's trend
        tracker. With ``trends_enabled`` it returns ``"trend_up"`` or
        ``"trend_down"`` when the value moved and ``fallback`` when it did not;
        with trends off it returns ``fallback`` but keeps tracking so
        re-enabling does not flash every cell.

        The TUI redraws faster than most telemetry arrives. Pass ``version``,
        a counter the plugin bumps whenever new data lands, and the trend is
        evaluated once per data version and held until the next one, so the
        colour stays on like the CPU table's instead of pulsing on the one
        redraw that happens to see the change.
        """
        history = self.__dict__.setdefault("_trend_values", {})
        value = float(value)
        slot = history.get((key, metric))
        if slot is not None and version is not None and slot[0] == version:
            # Same data as last evaluation: hold the trend.
            trend = slot[2]
        else:
            previous = slot[1] if slot is not None else value
            if value > previous + _CHANGE_THRESHOLD:
                trend = "up"
            elif value < previous - _CHANGE_THRESHOLD:
                trend = "down"
            else:
                trend = "stable"
            history[(key, metric)] = (version, value, trend)
        if not self.trends_enabled:
            return fallback
        if trend == "up":
            return "trend_up"
        if trend == "down":
            return "trend_down"
        return fallback

    def observe_sample(self, stack_frames, timestamp_ns):
        """Called for every CPU sample.

        ``stack_frames`` is the unwinder result (a list of interpreter infos,
        each with ``threads`` carrying ``thread_id`` and ``frame_info``) and
        ``timestamp_ns`` is ``time.perf_counter_ns()`` at collection time.
        """
        return None
