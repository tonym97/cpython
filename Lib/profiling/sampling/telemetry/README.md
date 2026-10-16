# Telemetry Plugin Architecture (`profiling.sampling`)

The telemetry plugin system lets external data sources render alongside the
sampling profiler's CPU profile: live in the TUI, persisted next to binary
captures, replayed from them, summarised as text, and exported to the Firefox
Profiler. The core sampler (`SampleProfiler`) knows nothing about any
provider; a plugin supplies a helper process that emits events and renderers
that turn those events into output.

---

## Flow

1. The CLI enables plugins with repeatable `--plugin <id>`; `<id>` is a bundled
   plugin name or an importable dotted module path. It instantiates each
   plugin class with its resolved configuration (`default_config` merged with
   the plugin's own command line options).
2. `SampleProfiler` adds `target_pid` to each plugin's config and creates one
   `TelemetryHelperManager` per plugin with the plugin's helper command: its
   native `helper_command` when it has one, otherwise
   `python -m profiling.sampling.telemetry --plugin <id>`, which runs the
   plugin's `run_helper`.
3. The helper writes JSON lines `{"plugin", "event_type", "payload"}` on
   stdout. A reader thread parses them into a bounded queue; the sampling loop
   drains the queue after each sample without blocking.
4. Events reach the collector through `collect_plugin_event(plugin_id,
   event_type, payload)`. Every collector has this method; those that store or
   render events set `accepts_plugin_events = True`. For the others the
   sampler buffers events in a `TelemetryRecordBuffer` and the CLI prints each
   plugin's `summarize` lines after the CPU output.
5. In live mode `LiveStatsCollector` keeps one `TelemetryPanel` per plugin
   (from `create_panel`), routes events to `panel.ingest` and renders
   `render_lines` in the telemetry panel. It calls `observe_sample(stack_frames,
   timestamp_ns)` for every CPU sample, with the sampler's unwind-start time,
   so a panel can attribute its events to Python frames.
6. In binary mode `BinaryCollector` appends events to
   `<binary>.telemetrychunks`; `replay_sidecar_to_sink` reads them back into a
   collector that accepts plugin events, and `replay` into any other format
   prints the summary instead.
7. The gecko (Firefox Profiler) collector stores events and, at export, asks
   each plugin for a `GeckoContribution`: marker-only tracks, markers on
   existing CPU threads, and graph tracks, which it turns into Firefox
   Profiler threads, markers and marker schemas on the CPU timeline. Plugin
   times are wall-clock nanoseconds; a `TelemetryClock` maps them to profile
   time from a `{utc_ns, monotonic_ns}` pair read at one instant, taken from
   the plugin's `metadata` record or measured by the collector.
8. For `run`, the CLI merges each plugin's `target_environment` into the
   target's environment before launching it.

---

## Modules

- `plugin.py`: `TelemetryPlugin` base class; `ModuleHookPlugin` adapter for
  modules written as `PLUGIN_ID` plus module-level hook functions.
- `panel.py`: `TelemetryPanel` base class for live panels and `Segment`.
- `records.py`: `TelemetryRecord` and `TelemetryRecordBuffer`.
- `plugin_registry.py`: plugin lookup (bundled directory scan, then dotted
  module import), `make_plugin`, `helper_command`.
- `manager.py`: one helper subprocess per plugin, reader threads, bounded
  event queue, helper exit reported as a `health` event with the stderr tail.
- `helper.py`: in-Python helper entrypoint; runs `run_helper` and exits.
- `chunks.py`: sidecar writer/reader (`.telemetrychunks`, `TCHUNK1` header,
  JSON lines).
- `replay.py`: replays sidecar records into a collector.
- `summary.py`: `summary_lines` for outputs without a live panel or sidecar.
- `gecko.py`: `TelemetryClock` and the `GeckoContribution` dataclasses.
- `interfaces.py`: `LiveTelemetryPlugin`, the earlier name of `TelemetryPanel`.
- `plugins/`: bundled plugins.

---

## Plugin API

A plugin module exposes a `TelemetryPlugin` subclass as `PLUGIN`. Every
method has a default, so a plugin implements only what it provides.

```python
from profiling.sampling.telemetry import TelemetryPlugin, TelemetryPanel

class CounterPanel(TelemetryPanel):
    panel_modes = ("counters",)

    def __init__(self):
        super().__init__()
        self.latest = {}

    def ingest(self, event_type, payload):
        if event_type == "counters":
            self.latest = payload

    def render_lines(self, mode, width):
        return [f"{name}: {value}" for name, value in self.latest.items()]

class CounterPlugin(TelemetryPlugin):
    plugin_id = "counters"

    @classmethod
    def default_config(cls):
        return {"interval_ms": 250}

    def run_helper(self, emit):
        pid = self.config["target_pid"]
        while True:
            emit(self.plugin_id, "counters", read_counters(pid))
            time.sleep(self.config["interval_ms"] / 1000)

    def create_panel(self):
        return CounterPanel()

PLUGIN = CounterPlugin
```

`TelemetryPlugin`:

| member | purpose |
|---|---|
| `plugin_id` | the id named with `--plugin`; a bundled plugin is found by it |
| `default_config()` (classmethod) | default helper and panel configuration |
| `resolve_config(overrides)` (classmethod) | `default_config` with overrides applied |
| `register_cli_flags(parser)` (classmethod) | plugin options on `run` and `attach`, registered when the plugin is named with `--plugin` |
| `extract_cli_config(args)` (classmethod) | config overrides taken from the parsed options |
| `config` | the resolved configuration; the sampler adds `target_pid` |
| `helper_command()` | argv of a native helper speaking the event protocol, or `None` to run `run_helper` in a Python helper process |
| `run_helper(emit)` | in-Python helper body; `emit(plugin_id, event_type, payload)`; return when finished |
| `target_environment()` | environment for targets started by `run` |
| `create_panel()` | a `TelemetryPanel` for the live TUI, or `None` |
| `summarize(records)` | text lines for non-live formats and `replay`, or `None` for a generic event count |
| `gecko_contribution(records, clock)` | a `GeckoContribution` for the Firefox Profiler export, or `None` |

`records` are `TelemetryRecord(plugin_id, event_type, payload)` instances.

`TelemetryPanel`:

- `panel_modes`: tuple of panel names cycled with `m`.
- `ingest(event_type, payload)`.
- `render_lines(mode, width)`: one entry per line, either a string or a list
  of `Segment(text, style)`. Styles map onto the TUI colour scheme so panels
  match the CPU table: `header` and `sorted` (column headers), `good`, `warn`,
  `hot` (green, yellow, red), `cyan`, `magenta`, `func`, `file`, `samples`,
  `trend_up`, `trend_down`, `bold`, `dim`, `normal`. Unknown styles render
  normally.
- `set_trends_enabled(enabled)`: called by the TUI from its `x` toggle before
  each render. `trend_style(key, metric, value, fallback, version)` tracks a
  numeric cell across renders and returns the trend style when it moved and
  trends are on; `version` is a counter the panel bumps when new data lands,
  so the colour holds between data arrivals like the CPU table's.
- `observe_sample(stack_frames, timestamp_ns)`: called for every CPU sample.
  `stack_frames` is the unwinder result; each thread carries `thread_id` (the
  OS thread id) and `frame_info`. `timestamp_ns` is `time.perf_counter_ns()`
  when the unwind started.

`GeckoContribution` (see `gecko.py`): `add_track(name, process_name)` returns
a `GeckoTrack` with `add_marker(name, start_ms, end_ms, data)`;
`add_thread_marker(tid, name, start_ms, end_ms, data)` places a marker on a
sampled CPU thread; `add_graph(name, series, process_name, schema_name)`
returns a `GeckoGraph` of `GeckoSeries` with `add_point(start_ms, end_ms,
values)`; `marker_schemas` holds Firefox marker schema dicts. The collector
replaces field formats and graph colours the profiler does not accept.

Event payload schemas are plugin-owned. Reserved `health` payload keys written
by the framework: `helper` (`failed`, `exited`, `overflow`, `finished`,
`error`), `returncode`, `stderr_tail`, `command`, `notes`.

Modules written as `PLUGIN_ID` plus module-level functions
(`resolve_helper_config(config)`, `helper_command(config)`,
`run_helper(config, emit)`, `target_environment(config)`,
`register_cli_flags(parser)`, `extract_cli_config(args)`,
`create_live_plugin(config)`, `summarize_records(records)`,
`gecko_contribution(records, clock)`, with records as `(plugin_id,
event_type, payload)` tuples) load through `ModuleHookPlugin`.

---

## Collector API

Every `Collector` has `set_plugin_enabled(plugin_id)`,
`set_plugin_metadata(plugin_id, metadata)` and `collect_plugin_event(plugin_id,
event_type, payload)`, no-ops by default, plus two class flags:
`accepts_plugin_events` (the collector stores or renders events; otherwise the
sampler buffers them for the text summary) and `accepts_sample_timestamp`
(`collect` takes `timestamp_us`, the sampler's unwind-start time).

---

## Live UI

- `g` toggles the telemetry panel, `n` cycles plugins, `m` cycles the current
  plugin's panels.
- The panel is `TELEMETRY_PANEL_HEIGHT` lines (title plus plugin lines).

---

## CLI validation

- Unknown plugin ids are rejected at argument parsing.
- Plugins named with `--plugin` contribute their own options to `run` and
  `attach`; `--help` with `--plugin <id>` shows them.
- Plugins work with every output format: live panel, binary sidecar, Firefox
  Profiler tracks, or a text summary after the CPU output.

Naming a dotted module with `--plugin` imports and runs it, like `python -m`;
only use plugins from sources you trust.

---

## Adding a plugin

1. Create a module (bundled under `telemetry/plugins/`, or anywhere
   importable) with a `TelemetryPlugin` subclass exposed as `PLUGIN`.
2. Implement a `TelemetryPanel` subclass for the TUI if the plugin has a live
   view, `summarize` for text output and `gecko_contribution` for the Firefox
   Profiler.
3. Enable it: `--plugin my_plugin` or `--plugin my_package.my_module`.

Tests: `Lib/test/test_profiling/test_sampling_profiler/test_telemetry.py`
covers the registry, the adapter, the helper manager, the buffer and summary,
the sidecar, the sampler integration, the panel and widget, the gecko export
and the CLI with plugins defined in the test module.

---

## Status register

| Item | State | Owner |
|---|---|---|
| Helper lifecycle, reader thread, exit reporting | built | profiler |
| Dotted-module plugin resolution | built | profiler |
| Target environment injection for `run` | built | profiler |
| Per-sample `observe_sample` hook | built | profiler |
| Plugin-specific CLI flags (`register_cli_flags`, `extract_cli_config`) | built | profiler |
| Sidecar format | built: JSON lines with a magic header, readable without the profiler | profiler |
| Replay of plugin events into non-live collectors | built: text summary via `summarize` | profiler |
| Non-live capture in formats other than binary | built: events buffered and summarised after the CPU output | profiler |
| Plugin data in the Firefox Profiler (gecko) export | built: tracks, thread markers and graphs through `gecko_contribution`, for live capture and `replay --gecko` | profiler |
| Framework unit tests | built: `test_telemetry.py`, no hardware needed | profiler |
