"""Text summary of telemetry events.

Collectors without plugin event handling of their own (pstats, flamegraph,
collapsed stacks, heatmap, JSONL) produce a CPU-only artifact, so the
sampler gathers plugin events in a ``TelemetryRecordBuffer`` and the CLI
prints ``summary_lines`` after the CPU output. ``replay`` into one of those
formats summarises the binary profile's sidecar the same way. Each plugin
turns its own records into lines through ``TelemetryPlugin.summarize``;
plugins that return ``None`` get a generic event count.
"""

import collections

from .plugin_registry import make_plugin


def _generic_summary(records):
    counts = collections.Counter(record.event_type for record in records)
    detail = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items()))
    return [f"{len(records)} events ({detail})"]


def summary_lines(buffer):
    """Summary of a ``TelemetryRecordBuffer``, one section per plugin."""
    if buffer is None or not len(buffer):
        return []
    lines = []
    for plugin_id, records in buffer.by_plugin().items():
        if not records:
            continue
        plugin = make_plugin(plugin_id)
        body = plugin.summarize(records) if plugin is not None else None
        if body is None:
            body = _generic_summary(records)
        lines.append(f"Telemetry [{plugin_id}]")
        lines.extend("  " + line for line in body)
    if buffer.dropped:
        lines.append(f"  {buffer.dropped} events dropped (buffer full)")
    return lines
