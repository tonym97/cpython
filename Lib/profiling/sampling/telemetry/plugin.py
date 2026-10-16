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

Modules written against the older module-level hook convention (a
``PLUGIN_ID`` constant plus functions such as ``create_live_plugin`` and
``run_helper``) are wrapped by ``ModuleHookPlugin`` so they keep working.
"""


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
    def resolve_config(cls, overrides=None):
        """``default_config`` with ``overrides`` applied."""
        config = dict(cls.default_config())
        if overrides:
            config.update(overrides)
        return config

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


class ModuleHookPlugin(TelemetryPlugin):
    """Adapter presenting a module-level hook plugin as a ``TelemetryPlugin``.

    ``module`` provides any of ``resolve_helper_config``, ``helper_command``,
    ``run_helper``, ``target_environment``, ``register_cli_flags``,
    ``extract_cli_config``, ``create_live_plugin``, ``summarize_records`` and
    ``gecko_contribution`` as functions taking the config or records
    explicitly. Record-taking hooks receive ``(plugin_id, event_type,
    payload)`` tuples, the convention those modules were written for.
    """

    module = None

    @classmethod
    def for_module(cls, module, plugin_id):
        return type(f"{cls.__name__}[{plugin_id}]", (cls,),
                    {"module": module, "plugin_id": plugin_id})

    @classmethod
    def _hook(cls, name):
        hook = getattr(cls.module, name, None)
        return hook if callable(hook) else None

    @classmethod
    def default_config(cls):
        hook = cls._hook("resolve_helper_config")
        return dict(hook(None) or {}) if hook else {}

    @classmethod
    def register_cli_flags(cls, parser):
        hook = cls._hook("register_cli_flags")
        if hook:
            hook(parser)

    @classmethod
    def extract_cli_config(cls, args):
        hook = cls._hook("extract_cli_config")
        return dict(hook(args) or {}) if hook else {}

    def helper_command(self):
        hook = self._hook("helper_command")
        return hook(self.config) if hook else None

    def run_helper(self, emit):
        hook = self._hook("run_helper")
        if hook is None:
            return super().run_helper(emit)
        return hook(self.config, emit)

    def target_environment(self):
        hook = self._hook("target_environment")
        return dict(hook(self.config) or {}) if hook else {}

    def create_panel(self):
        hook = self._hook("create_live_plugin")
        if hook is None:
            return None
        try:
            return hook(self.config)
        except TypeError:
            return hook()

    def summarize(self, records):
        hook = self._hook("summarize_records")
        return hook(_as_tuples(records)) if hook else None

    def gecko_contribution(self, records, clock):
        hook = self._hook("gecko_contribution")
        return hook(_as_tuples(records), clock) if hook else None


def _as_tuples(records):
    return [(r.plugin_id, r.event_type, r.payload) for r in records]
