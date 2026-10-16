"""Telemetry event records and the in-memory record buffer."""

import collections
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TelemetryRecord:
    """One event from a telemetry plugin's helper.

    ``event_type`` and the shape of ``payload`` are defined by the plugin;
    the framework only reserves the ``health`` event type and the keys it
    writes into it (``helper``, ``returncode``, ``stderr_tail``, ``command``,
    ``notes``).
    """

    plugin_id: str
    event_type: str
    payload: dict = field(default_factory=dict)


class TelemetryRecordBuffer:
    """In-memory sink implementing the collector plugin event API.

    Used for collectors that have no plugin event handling of their own
    (every non-live, non-binary output format) and for summarising a binary
    profile's sidecar on replay. The buffer is bounded; records beyond the
    cap are counted in ``dropped``.
    """

    accepts_plugin_events = True
    accepts_sample_timestamp = False

    def __init__(self, max_records=2_000_000):
        self.records = []
        self.plugin_ids = []
        self.dropped = 0
        self._max_records = max_records

    def set_plugin_enabled(self, plugin_id):
        if plugin_id and plugin_id not in self.plugin_ids:
            self.plugin_ids.append(plugin_id)

    def set_plugin_metadata(self, plugin_id, metadata):
        self.collect_plugin_event(plugin_id, "metadata", metadata)

    def collect_plugin_event(self, plugin_id, event_type, payload):
        self.set_plugin_enabled(plugin_id)
        if len(self.records) >= self._max_records:
            self.dropped += 1
            return
        self.records.append(TelemetryRecord(plugin_id, event_type, payload))

    def by_plugin(self):
        """Records grouped by plugin id, in first-seen order."""
        groups = collections.OrderedDict()
        for plugin_id in self.plugin_ids:
            groups[plugin_id] = []
        for record in self.records:
            groups.setdefault(record.plugin_id, []).append(record)
        return groups

    def __len__(self):
        return len(self.records)

    def __iter__(self):
        return iter(self.records)
