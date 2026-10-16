"""Replay of a binary profile's telemetry sidecar into a collector."""

from .chunks import TelemetryChunkReader


def replay_sidecar_to_sink(binary_filename, sink):
    """Feed the sidecar's records to ``sink`` if it accepts plugin events.

    Returns the number of records delivered.
    """
    if not getattr(sink, "accepts_plugin_events", False):
        return 0
    reader = TelemetryChunkReader(binary_filename)
    if not reader.exists():
        return 0
    seen_plugins = set()
    delivered = 0
    for record in reader.iter_records():
        plugin_id = record.get("plugin")
        event_type = record.get("event_type")
        if not plugin_id or not event_type:
            continue
        if plugin_id not in seen_plugins:
            sink.set_plugin_enabled(plugin_id)
            seen_plugins.add(plugin_id)
        sink.collect_plugin_event(plugin_id, event_type, record.get("payload", {}))
        delivered += 1
    return delivered
