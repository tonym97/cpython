"""Telemetry events for collectors that have no plugin event API.

Live and binary collectors receive plugin events directly. Every other
collector (pstats, flamegraph, collapsed stacks, gecko, heatmap) produces a
CPU-only artifact, so plugin events are gathered in a ``TelemetryRecordBuffer``
instead and summarised as text when the profile is written: each plugin turns
its own records into lines through its ``summarize_records`` hook, and plugins
without the hook get a generic event count.

The same path serves ``replay`` of a binary profile into a non-live format:
the sidecar's records are read into a buffer and summarised after the CPU
output.
"""

from __future__ import annotations

import collections
import sys

from .plugin_registry import summarize_records


class TelemetryRecordBuffer:
    """In-memory sink implementing the collector plugin event API."""

    def __init__(self, max_records=2_000_000):
        self.records = []
        self.plugin_ids = []
        self.dropped = 0
        self._max = max_records

    def set_plugin_enabled(self, plugin_id):
        if plugin_id and plugin_id not in self.plugin_ids:
            self.plugin_ids.append(plugin_id)

    def set_plugin_metadata(self, plugin_id, metadata):
        self.collect_plugin_event(plugin_id, "metadata", metadata)

    def collect_plugin_event(self, plugin_id, event_type, payload):
        self.set_plugin_enabled(plugin_id)
        if len(self.records) >= self._max:
            self.dropped += 1
            return
        self.records.append((plugin_id, event_type, payload))

    def __len__(self):
        return len(self.records)


def _generic_summary(plugin_id, records):
    counts = collections.Counter(event_type for _, event_type, _ in records)
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    return [f"{len(records)} events ({detail})"]


def summary_lines(buffer):
    """Text summary of buffered plugin events, one section per plugin."""
    if buffer is None or not buffer.records:
        return []
    by_plugin = collections.defaultdict(list)
    for plugin_id, event_type, payload in buffer.records:
        by_plugin[plugin_id].append((plugin_id, event_type, payload))
    lines = []
    for plugin_id in buffer.plugin_ids or sorted(by_plugin):
        records = by_plugin.get(plugin_id)
        if not records:
            continue
        lines.append(f"Telemetry [{plugin_id}]")
        body = summarize_records(plugin_id, records)
        if body is None:
            body = _generic_summary(plugin_id, records)
        lines.extend("  " + line for line in body)
        if buffer.dropped:
            lines.append(f"  {buffer.dropped} events dropped (buffer full)")
    return lines


def print_summary(buffer, file=None):
    lines = summary_lines(buffer)
    if lines:
        print(file=file or sys.stdout)
        print("\n".join(lines), file=file or sys.stdout)
    return bool(lines)
