"""Telemetry plugin lookup.

A plugin is named on the command line with ``--plugin <id>``. The id is
either a bundled plugin, discovered under ``profiling.sampling.telemetry
.plugins`` by its ``plugin_id``, or the dotted path of any importable module
(``--plugin my_company.tachyon_plugin``). A module provides its plugin as a
``TelemetryPlugin`` subclass named ``PLUGIN``; a module that instead defines
``PLUGIN_ID`` and module-level hook functions is wrapped by
``ModuleHookPlugin``.

Naming a dotted module imports and runs it, like ``python -m``; only use
plugins from sources you trust.
"""

import importlib
import json
import pkgutil
import sys

from . import plugins as plugins_pkg
from .plugin import ModuleHookPlugin, TelemetryPlugin

_BUNDLED = None
_EXTERNAL = {}


def _plugin_class_from_module(module, fallback_id):
    plugin_class = getattr(module, "PLUGIN", None)
    if isinstance(plugin_class, type) and issubclass(plugin_class, TelemetryPlugin):
        if not plugin_class.plugin_id:
            plugin_class.plugin_id = fallback_id
        return plugin_class
    plugin_id = getattr(module, "PLUGIN_ID", None)
    if plugin_id:
        return ModuleHookPlugin.for_module(module, plugin_id)
    return None


def _discover_bundled():
    found = {}
    for module_info in pkgutil.iter_modules(plugins_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{plugins_pkg.__name__}.{module_info.name}")
        except Exception:
            continue
        plugin_class = _plugin_class_from_module(module, module_info.name)
        if plugin_class is not None:
            found[plugin_class.plugin_id] = plugin_class
    return found


def _bundled():
    global _BUNDLED
    if _BUNDLED is None:
        _BUNDLED = _discover_bundled()
    return _BUNDLED


def get_plugin_class(plugin_id):
    """The ``TelemetryPlugin`` subclass for ``plugin_id``, or ``None``."""
    if not plugin_id:
        return None
    plugin_class = _bundled().get(plugin_id) or _EXTERNAL.get(plugin_id)
    if plugin_class is not None:
        return plugin_class
    if "." not in plugin_id:
        return None
    try:
        module = importlib.import_module(plugin_id)
    except ImportError:
        return None
    plugin_class = _plugin_class_from_module(module, plugin_id)
    if plugin_class is not None:
        _EXTERNAL[plugin_id] = plugin_class
    return plugin_class


def is_known_plugin(plugin_id):
    return get_plugin_class(plugin_id) is not None


def list_plugin_ids():
    """Ids of the bundled plugins."""
    return sorted(_bundled())


def make_plugin(plugin_id, overrides=None):
    """Instantiate ``plugin_id`` with its resolved config, or return ``None``."""
    plugin_class = get_plugin_class(plugin_id)
    if plugin_class is None:
        return None
    return plugin_class(plugin_class.resolve_config(overrides))


def helper_command(plugin):
    """Argv for the plugin's helper process.

    The plugin's own ``helper_command`` when it has one; otherwise a Python
    helper process that runs the plugin's ``run_helper``.
    """
    command = plugin.helper_command()
    if command:
        return list(command)
    return [
        sys.executable,
        "-m",
        "profiling.sampling.telemetry",
        "--plugin",
        plugin.plugin_id,
        "--config-json",
        json.dumps(plugin.config, separators=(",", ":"), default=str),
    ]
