"""Telemetry plugin registry and dispatch.

A plugin is a module that declares ``PLUGIN_ID``. Plugins are found in two
ways:

* bundled modules under ``profiling.sampling.telemetry.plugins`` are
  discovered by their ``PLUGIN_ID`` (``--plugin my_plugin``);
* any importable module can be named by dotted path
  (``--plugin my_company.tachyon_plugin``). Its ``PLUGIN_ID`` is used if
  present, otherwise the dotted path itself.

Optional module hooks, all looked up by name:

``create_live_plugin(config=None) -> LiveTelemetryPlugin | None``
    Live TUI adapter. If the factory accepts an argument it receives the
    resolved helper config, so panel settings can live beside helper settings.
``resolve_helper_config(config: dict | None) -> dict``
    Fill in defaults for the helper configuration. The profiler always adds
    ``target_pid`` before a helper is started.
``helper_command(config: dict) -> list[str] | None``
    Argv for a native helper process that speaks the JSON-lines event
    protocol on stdout. When absent or ``None`` the helper is
    ``python -m profiling.sampling.telemetry --plugin <id>``, which calls
    ``run_helper``.
``run_helper(config: dict, emit) -> None``
    In-Python helper body; return when done.
``target_environment(config: dict) -> dict[str, str]``
    Environment variables to set on a target launched by ``run``.
``register_cli_flags(parser)``
    Add plugin-specific command line options to the ``run`` and ``attach``
    parsers; called only for plugins named with ``--plugin`` on the command
    line, so a plugin's options exist exactly when the plugin is enabled.
``extract_cli_config(args) -> dict``
    Turn parsed plugin options into helper config overrides, merged over
    ``resolve_helper_config``.
``summarize_records(records) -> list[str] | None``
    Text summary of this plugin's events for outputs with no live panel
    (non-binary capture formats and ``replay``). ``records`` are
    ``(plugin_id, event_type, payload)`` tuples.
"""

from __future__ import annotations

import importlib
import pkgutil

from . import plugins as plugins_pkg


_PLUGIN_MODULES = None
_EXTERNAL_MODULES = {}


def _discover_plugin_modules():
    modules = {}
    for module_info in pkgutil.iter_modules(plugins_pkg.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{plugins_pkg.__name__}.{name}")
        except Exception:
            continue
        plugin_id = getattr(module, "PLUGIN_ID", None)
        if plugin_id:
            modules[plugin_id] = module
    return modules


def _get_plugin_modules():
    global _PLUGIN_MODULES
    if _PLUGIN_MODULES is None:
        _PLUGIN_MODULES = _discover_plugin_modules()
    return _PLUGIN_MODULES


def _get_plugin_module(plugin_id):
    if not plugin_id:
        return None
    module = _get_plugin_modules().get(plugin_id)
    if module is not None:
        return module
    module = _EXTERNAL_MODULES.get(plugin_id)
    if module is not None:
        return module
    if "." in plugin_id:
        try:
            module = importlib.import_module(plugin_id)
        except ImportError:
            return None
        _EXTERNAL_MODULES[plugin_id] = module
        return module
    return None


def is_known_plugin(plugin_id):
    return _get_plugin_module(plugin_id) is not None


def list_plugin_ids():
    return sorted(_get_plugin_modules())


def create_live_plugin(plugin_id):
    module = _get_plugin_module(plugin_id)
    if module is None:
        return None
    factory = getattr(module, "create_live_plugin", None)
    if callable(factory):
        try:
            plugin = factory(resolve_helper_config(plugin_id))
        except TypeError:
            plugin = factory()
        if plugin is not None and getattr(plugin, "plugin_id", None) in (None, "base"):
            plugin.plugin_id = plugin_id
        return plugin
    return None


def resolve_helper_config(plugin_id, config=None):
    module = _get_plugin_module(plugin_id)
    if module is None:
        return dict(config or {})
    resolver = getattr(module, "resolve_helper_config", None)
    if callable(resolver):
        return resolver(config)
    return dict(config or {})


def resolve_helper_command(plugin_id, config):
    module = _get_plugin_module(plugin_id)
    if module is None:
        return None
    hook = getattr(module, "helper_command", None)
    if callable(hook):
        return hook(config)
    return None


def resolve_target_environment(plugin_id, config):
    module = _get_plugin_module(plugin_id)
    if module is None:
        return {}
    hook = getattr(module, "target_environment", None)
    if callable(hook):
        return dict(hook(config) or {})
    return {}


def register_cli_flags(plugin_id, parser):
    module = _get_plugin_module(plugin_id)
    hook = getattr(module, "register_cli_flags", None) if module else None
    if callable(hook):
        hook(parser)
        return True
    return False


def extract_cli_config(plugin_id, args):
    module = _get_plugin_module(plugin_id)
    hook = getattr(module, "extract_cli_config", None) if module else None
    if callable(hook):
        return dict(hook(args) or {})
    return {}


def summarize_records(plugin_id, records):
    module = _get_plugin_module(plugin_id)
    hook = getattr(module, "summarize_records", None) if module else None
    if callable(hook):
        return hook(records)
    return None


def run_helper_plugin(plugin_id, config, emit):
    module = _get_plugin_module(plugin_id)
    if module is None:
        emit(plugin_id, "health", {"helper": "error", "notes": ["unknown plugin"]})
        return
    runner = getattr(module, "run_helper", None)
    if callable(runner):
        runner(config, emit)
        return
    emit(plugin_id, "health", {"helper": "error", "notes": ["plugin has no run_helper and no helper_command"]})
