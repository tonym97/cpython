"""Compatibility names for the telemetry panel API.

``LiveTelemetryPlugin`` is a compatibility alias for ``TelemetryPanel``; plugins
written against it keep working.
"""

from .panel import Segment, TelemetryPanel

LiveTelemetryPlugin = TelemetryPanel

__all__ = ("LiveTelemetryPlugin", "Segment", "TelemetryPanel")
