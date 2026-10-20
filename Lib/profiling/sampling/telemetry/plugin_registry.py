"""Telemetry plugin lookup.

A plugin is named on the command line with ``--plugin <id>``. The id is
resolved in this order:

1. a bundled plugin under ``profiling.sampling.telemetry.plugins``, by its
   ``plugin_id``;
2. an installed package that registers the id in the
   ``profiling.sampling.telemetry`` entry point group, for example
   ``my_plugin = my_plugin_package:PLUGIN`` in its project metadata;
3. the dotted path of any importable module (``--plugin
   my_company.tachyon_plugin``), which provides its plugin as a
   ``TelemetryPlugin`` subclass named ``PLUGIN``; the path must contain a
   dot, so a top-level module is not taken for a plugin id.

An id that matches an entry point or looks like a module path but fails to
load is unknown; ``plugin_load_error`` keeps the exception text so the
command line can say why.

Event records carry only the ``plugin_id``, so a plugin that should work for
``replay`` and in the Python helper process registers an entry point; a
dotted path is resolved only in the process that was given it.

Naming a dotted module or installing a plugin package imports and runs its
code, like ``python -m``; only use plugins from sources you trust.
"""

import importlib
import importlib.metadata
import pkgutil

ENTRY_POINT_GROUP = "profiling.sampling.telemetry"

from . import plugins as plugins_pkg
from .plugin import TelemetryPlugin

_BUNDLED = None
_EXTERNAL = {}
_LOAD_ERRORS = {}  # plugin id -> text of the exception that stopped its load


def _plugin_class_from_module(module, fallback_id):
    """The module's ``PLUGIN`` class, given the module's name as id if it has none."""
    plugin_class = getattr(module, "PLUGIN", None)
    if not (isinstance(plugin_class, type) and issubclass(plugin_class, TelemetryPlugin)):
        return None
    if not plugin_class.plugin_id:
        plugin_class.plugin_id = fallback_id
    return plugin_class


def _discover_bundled():
    found = {}
    for module_info in pkgutil.iter_modules(plugins_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{plugins_pkg.__name__}.{module_info.name}")
        except Exception as exc:
            # The id is unknown until the module imports; keep the cause under
            # the module name so `--plugin <name>` can report it.
            _LOAD_ERRORS[module_info.name] = repr(exc)
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


def _entry_points():
    return importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)


def _from_entry_point(plugin_id):
    for entry_point in _entry_points():
        if entry_point.name != plugin_id:
            continue
        try:
            target = entry_point.load()
        except Exception as exc:
            target_name = getattr(entry_point, "value", entry_point.name)
            _LOAD_ERRORS[plugin_id] = f"entry point {target_name!r}: {exc!r}"
            return None
        if isinstance(target, type) and issubclass(target, TelemetryPlugin):
            plugin_class = target
        else:
            plugin_class = _plugin_class_from_module(target, plugin_id)
        if plugin_class is not None and not plugin_class.plugin_id:
            plugin_class.plugin_id = plugin_id
        return plugin_class
    return None


def _from_dotted_path(plugin_id):
    if "." not in plugin_id:
        return None
    try:
        module = importlib.import_module(plugin_id)
    except ModuleNotFoundError as exc:
        if exc.name in (plugin_id, plugin_id.split(".")[0]):
            return None  # no such module: an ordinary unknown id
        _LOAD_ERRORS[plugin_id] = repr(exc)
        return None
    except Exception as exc:  # the module exists and raised while importing
        _LOAD_ERRORS[plugin_id] = repr(exc)
        return None
    return _plugin_class_from_module(module, plugin_id)


def get_plugin_class(plugin_id):
    """The ``TelemetryPlugin`` subclass for ``plugin_id``, or ``None``."""
    if not plugin_id:
        return None
    plugin_class = _bundled().get(plugin_id) or _EXTERNAL.get(plugin_id)
    if plugin_class is None:
        plugin_class = _from_entry_point(plugin_id) or _from_dotted_path(plugin_id)
        if plugin_class is not None:
            # Remember the class under the name given and under its own id,
            # so events it emits resolve back to it in this process.
            _EXTERNAL[plugin_id] = plugin_class
            _EXTERNAL.setdefault(plugin_class.plugin_id, plugin_class)
    return plugin_class


def is_known_plugin(plugin_id):
    return get_plugin_class(plugin_id) is not None


def plugin_load_error(plugin_id):
    """Why ``plugin_id`` failed to load (exception text), or ``None``."""
    return _LOAD_ERRORS.get(plugin_id)


def list_plugin_ids():
    """Ids of the bundled plugins and of installed entry point plugins."""
    return sorted(set(_bundled()) | {entry_point.name for entry_point in _entry_points()})


def make_plugin(plugin_id, overrides=None):
    """Instantiate ``plugin_id`` with its resolved config, or return ``None``."""
    plugin_class = get_plugin_class(plugin_id)
    if plugin_class is None:
        return None
    config = dict(plugin_class.default_config())
    config.update(overrides or {})
    return plugin_class(config)
