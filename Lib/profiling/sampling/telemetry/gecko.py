"""Plugin contributions to the Gecko (Firefox Profiler) export.

The gecko collector knows nothing about any telemetry provider. At export it
asks each plugin for a ``GeckoContribution`` built from the plugin's records:

* ``tracks``          marker-only tracks (one Firefox Profiler thread each),
                      for events that happen off the CPU, such as GPU kernels
* ``thread_markers``  markers placed on an existing CPU thread by Linux tid,
                      for events that belong to Python code, such as launches
* ``graphs``          numeric series drawn as track graphs, for counters
* ``marker_schemas``  Firefox Profiler marker schema entries for the ``type``
                      values the markers use

Times are profile milliseconds. A ``TelemetryClock`` converts the plugin's
wall-clock nanoseconds into them from a ``{"utc_ns", "monotonic_ns"}`` pair
read at the same instant: the helper emits one in its ``metadata`` record,
and the collector measures one of its own for live captures.
"""

from dataclasses import dataclass, field

# The Firefox Profiler accepts only these graph colours and marker field
# formats; the collector replaces anything else so the profile still loads.
GECKO_GRAPH_COLORS = frozenset({
    "blue", "green", "grey", "ink", "magenta", "orange", "purple", "red", "teal", "yellow",
})
GECKO_FIELD_FORMATS = frozenset({
    "url", "file-path", "sanitized-string", "string", "unique-string", "duration", "time",
    "seconds", "milliseconds", "microseconds", "nanoseconds", "bytes", "percentage",
    "integer", "decimal", "flow-id", "terminating-flow-id", "list", "pid", "tid",
})


class TelemetryClock:
    """Maps wall-clock nanoseconds (CLOCK_REALTIME) to profile milliseconds.

    ``base_monotonic_us`` is the profile's zero in monotonic microseconds
    (``time.monotonic()`` for live captures, the first sample's timestamp for
    a binary replay). ``utc_ns`` and ``monotonic_ns`` were read together.
    """

    def __init__(self, base_monotonic_us, utc_ns, monotonic_ns):
        self.base_monotonic_us = float(base_monotonic_us)
        self.utc_minus_monotonic_ns = int(utc_ns) - int(monotonic_ns)

    @classmethod
    def from_records(cls, base_monotonic_us, records, fallback_pair=None):
        """Use the clock pair from a plugin's metadata record, else ``fallback_pair``."""
        for record in records:
            if record.event_type != "metadata" or not isinstance(record.payload, dict):
                continue
            clock = record.payload.get("clock")
            if isinstance(clock, dict) and "utc_ns" in clock and "monotonic_ns" in clock:
                return cls(base_monotonic_us, clock["utc_ns"], clock["monotonic_ns"])
        if fallback_pair is not None:
            return cls(base_monotonic_us, *fallback_pair)
        return None

    def to_ms(self, utc_ns):
        if not utc_ns:
            return None
        monotonic_us = (int(utc_ns) - self.utc_minus_monotonic_ns) / 1000.0
        return (monotonic_us - self.base_monotonic_us) / 1000.0


@dataclass
class GeckoMarker:
    """An interval marker (``end_ms`` set) or instant marker (``end_ms`` None).

    ``data`` becomes the marker's payload; its ``type`` selects the schema and
    defaults to the marker name without spaces. ``tid`` places the marker on
    that CPU thread instead of a plugin track.
    """

    name: str
    start_ms: float
    end_ms: float = None
    data: dict = field(default_factory=dict)
    tid: int = None


@dataclass
class GeckoTrack:
    """A marker-only track, exported as its own Firefox Profiler thread."""

    name: str
    process_name: str = "Telemetry"
    markers: list = field(default_factory=list)

    def add_marker(self, name, start_ms, end_ms=None, data=None):
        marker = GeckoMarker(name, start_ms, end_ms, dict(data or {}))
        self.markers.append(marker)
        return marker


@dataclass
class GeckoSeries:
    """One line in a graph track; ``format`` is a Firefox marker field format."""

    key: str
    label: str = ""
    color: str = "grey"
    format: str = "decimal"


@dataclass
class GeckoGraphPoint:
    start_ms: float
    end_ms: float
    values: dict = field(default_factory=dict)


@dataclass
class GeckoGraph:
    """A track drawn as line graphs of ``series`` over ``points``."""

    name: str
    series: list = field(default_factory=list)
    points: list = field(default_factory=list)
    process_name: str = "Telemetry"
    schema_name: str = None

    def add_point(self, start_ms, end_ms, values):
        self.points.append(GeckoGraphPoint(start_ms, end_ms, dict(values)))


@dataclass
class GeckoContribution:
    """What a plugin adds to the Gecko profile; see the module docstring."""

    tracks: list = field(default_factory=list)
    thread_markers: list = field(default_factory=list)
    graphs: list = field(default_factory=list)
    marker_schemas: list = field(default_factory=list)

    def add_track(self, name, process_name="Telemetry"):
        track = GeckoTrack(name, process_name)
        self.tracks.append(track)
        return track

    def add_thread_marker(self, tid, name, start_ms, end_ms=None, data=None):
        """A marker on the CPU thread with OS thread id ``tid``."""
        marker = GeckoMarker(name, start_ms, end_ms, dict(data or {}), tid)
        self.thread_markers.append(marker)
        return marker

    def add_graph(self, name, series, process_name="Telemetry", schema_name=None):
        graph = GeckoGraph(name, list(series), [], process_name, schema_name)
        self.graphs.append(graph)
        return graph

    def is_empty(self):
        return not (self.tracks or self.thread_markers or self.graphs)
