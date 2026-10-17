"""Sidecar file for plugin events captured alongside a binary profile.

``<binary>.telemetrychunks`` holds a magic header line followed by one JSON
record per line in the helper wire format:
``{"v": 1, "plugin": id, "event_type": str, "payload": {...}}``.
"""

import json
import os

from .records import PROTOCOL_VERSION

_MAGIC = "TCHUNK1"


def sidecar_path_for_binary(binary_filename):
    return f"{binary_filename}.telemetrychunks"


class TelemetryChunkWriter:
    def __init__(self, binary_filename):
        self.sidecar_path = sidecar_path_for_binary(binary_filename)
        self._fp = open(self.sidecar_path, "w", encoding="utf-8")
        self._fp.write(_MAGIC + "\n")

    def write_record(self, plugin_id, event_type, payload):
        record = {
            "v": PROTOCOL_VERSION,
            "plugin": plugin_id,
            "event_type": event_type,
            "payload": payload,
        }
        self._fp.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self):
        if self._fp is not None:
            self._fp.close()
            self._fp = None


class TelemetryChunkReader:
    def __init__(self, binary_filename):
        self.sidecar_path = sidecar_path_for_binary(binary_filename)

    def exists(self):
        return os.path.exists(self.sidecar_path)

    def iter_records(self):
        if not self.exists():
            return
        with open(self.sidecar_path, "r", encoding="utf-8") as fp:
            first = fp.readline().rstrip("\n")
            if first != _MAGIC:
                return
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
