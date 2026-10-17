"""Telemetry plugin base class.

A telemetry plugin adds an external data source to the sampling profiler:
a helper process that emits events while the target runs, and renderers
that turn those events into a live TUI panel, a text summary, or Firefox
Profiler tracks. A plugin module exposes its plugin as a ``TelemetryPlugin``
subclass named ``PLUGIN``::

    class MyPlugin(TelemetryPlugin):
        plugin_id = "my_plugin"

        @classmethod
        def default_config(cls):
            return {"interval_ms": 250}

        def helper_command(self):
            return ["my-helper", "--pid", str(self.config["target_pid"])]

        def create_panel(self):
            return MyPanel()

    PLUGIN = MyPlugin

Every method has a default, so a plugin implements only what it provides.
Configuration is resolved at class level (``default_config`` merged with
command line options from ``extract_cli_config`` and any caller overrides)
because the command line parser needs ``register_cli_flags`` before any
instance exists; the profiler then instantiates the class with the resolved
config, adding ``target_pid`` once the profiled process is known.
"""

import json
import sys


class TelemetryPlugin:
    """Base class for telemetry plugins; subclasses set ``plugin_id``."""

    plugin_id = ""

    def __init__(self, config=None):
        self.config = dict(config or {})

    # ---- configuration ----------------------------------------------------

    @classmethod
    def default_config(cls):
        """Default helper and panel configuration."""
        return {}

    @classmethod
    def register_cli_flags(cls, parser):
        """Add plugin-specific options to a ``run``/``attach`` argument parser.

        Called only when the plugin is named with ``--plugin`` on the command
        line, so the options exist exactly when the plugin is enabled.
        """

    @classmethod
    def extract_cli_config(cls, args):
        """Config overrides taken from parsed command line options."""
        return {}

    # ---- helper process -----------------------------------------------------

    def helper_command(self):
        """Argv of a native helper speaking the JSON-lines event protocol.

        Return ``None`` to run ``run_helper`` in a Python helper process
        instead.
        """
        return None

    def run_helper(self, emit):
        """In-Python helper body; ``emit(plugin_id, event_type, payload)``.

        Return when finished. The default reports that the plugin has no
        helper.
        """
        emit(self.plugin_id, "health",
             {"helper": "error", "notes": ["plugin provides no helper"]})

    def target_environment(self):
        """Environment variables to set on a target started by ``run``."""
        return {}

    def helper_argv(self):
        """The command the profiler runs as this plugin's helper process.

        ``helper_command`` when the plugin has a native helper, otherwise a
        Python helper process that runs ``run_helper`` with this config. Not
        meant to be overridden.
        """
        command = self.helper_command()
        if command:
            return list(command)
        return [
            sys.executable, "-m", "profiling.sampling.telemetry",
            "--plugin", self.plugin_id,
            "--config-json", json.dumps(self.config, separators=(",", ":"), default=str),
        ]

    # ---- outputs ------------------------------------------------------------

    def create_panel(self):
        """A ``TelemetryPanel`` for the live TUI, or ``None``."""
        return None

    def summarize(self, records):
        """Text lines summarising ``records`` (``TelemetryRecord``), or ``None``.

        Used for output formats without a live panel or sidecar and for
        ``replay`` into them. ``None`` selects a generic event count.
        """
        return None

    def gecko_contribution(self, records, clock):
        """A ``GeckoContribution`` for the Firefox Profiler export, or ``None``.

        ``clock`` is a ``TelemetryClock`` converting the plugin's wall-clock
        nanoseconds to profile milliseconds.
        """
        return None

