"""Telemetry plugin framework for the sampling profiler.

Plugins add an external data source (a GPU, a system counter, a network
stack) next to the CPU profile: a helper process emits events while the
target runs, and the plugin renders them as a live TUI panel, a text summary
or Firefox Profiler tracks. See ``plugin.py`` for the plugin API and
``README.md`` for the architecture.
"""

from .chunks import TelemetryChunkReader, TelemetryChunkWriter, sidecar_path_for_binary
from .gecko import (
    GECKO_FIELD_FORMATS,
    GECKO_GRAPH_COLORS,
    GeckoContribution,
    GeckoGraph,
    GeckoGraphPoint,
    GeckoMarker,
    GeckoSeries,
    GeckoTrack,
    TelemetryClock,
)
from .manager import TelemetryHelperManager
from .panel import Segment, TelemetryPanel, frame_location, iter_thread_frames, line_text
from .plugin import TelemetryPlugin
from .plugin_registry import (
    get_plugin_class,
    is_known_plugin,
    list_plugin_ids,
    make_plugin,
)
from .records import PROTOCOL_VERSION, TelemetryRecord, TelemetryRecordBuffer
from .replay import replay_sidecar_to_sink
from .summary import summary_lines

__all__ = (
    "GECKO_FIELD_FORMATS",
    "GECKO_GRAPH_COLORS",
    "PROTOCOL_VERSION",
    "GeckoContribution",
    "GeckoGraph",
    "GeckoGraphPoint",
    "GeckoMarker",
    "GeckoSeries",
    "GeckoTrack",
    "Segment",
    "TelemetryChunkReader",
    "TelemetryChunkWriter",
    "TelemetryClock",
    "TelemetryHelperManager",
    "TelemetryPanel",
    "TelemetryPlugin",
    "TelemetryRecord",
    "TelemetryRecordBuffer",
    "frame_location",
    "get_plugin_class",
    "is_known_plugin",
    "iter_thread_frames",
    "line_text",
    "list_plugin_ids",
    "make_plugin",
    "replay_sidecar_to_sink",
    "sidecar_path_for_binary",
    "summary_lines",
)
