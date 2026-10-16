"""Generic in-Python helper-process entrypoint.

Used when a plugin has no native ``helper_command``. Runs the plugin's
``run_helper`` and exits when it returns; the manager reports the exit to the
collector as a ``health`` event.
"""

from __future__ import annotations

import argparse
import json
import sys

from .plugin_registry import run_helper_plugin


def _emit(plugin_id, event_type, payload):
    print(
        json.dumps(
            {
                "plugin": plugin_id,
                "event_type": event_type,
                "payload": payload,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Telemetry helper process")
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--config-json", default="{}")
    args = parser.parse_args()

    try:
        config = json.loads(args.config_json)
    except json.JSONDecodeError:
        config = {}

    try:
        run_helper_plugin(args.plugin, config, _emit)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # report, do not spin
        _emit(args.plugin, "health", {"helper": "error", "notes": [f"{type(exc).__name__}: {exc}"]})
        sys.exit(1)
    _emit(args.plugin, "health", {"helper": "finished"})


if __name__ == "__main__":
    main()
