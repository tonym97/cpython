"""Python helper process for plugins without a native helper.

Started as ``python -m profiling.sampling.telemetry --plugin <id>
--config-json <json>``. It instantiates the plugin with that config, runs
its ``run_helper`` and exits when it returns; the manager reports the exit
to the collector as a ``health`` event.
"""

import argparse
import json
import sys

from .plugin_registry import make_plugin


def _emit(plugin_id, event_type, payload):
    record = {"plugin": plugin_id, "event_type": event_type, "payload": payload}
    print(json.dumps(record, separators=(",", ":")), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Telemetry helper process")
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--config-json", default="{}")
    args = parser.parse_args()

    try:
        config = json.loads(args.config_json)
    except json.JSONDecodeError:
        config = {}

    plugin = make_plugin(args.plugin, config)
    if plugin is None:
        _emit(args.plugin, "health", {"helper": "error", "notes": ["unknown plugin"]})
        sys.exit(2)
    try:
        plugin.run_helper(_emit)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _emit(args.plugin, "health",
              {"helper": "error", "notes": [f"{type(exc).__name__}: {exc}"]})
        sys.exit(1)
    _emit(args.plugin, "health", {"helper": "finished"})


if __name__ == "__main__":
    main()
