# Telemetry Plugin Architecture (`profiling.sampling`)

The telemetry plugin system lets external data sources render alongside the
sampling profiler's CPU profile: live in the TUI, persisted next to binary
captures, and replayed from them. The core sampler (`SampleProfiler`) knows
nothing about any provider; plugins supply a helper process that emits events
and a live adapter that renders them.

---

## Flow

1. The CLI enables plugins with repeatable `--plugin <id>`; `<id>` is a bundled
   plugin name or an importable dotted module path.
2. `SampleProfiler` creates one `TelemetryHelperManager` per plugin and passes
   the plugin config, always including `target_pid`.
3. The manager starts the plugin's helper: its native `helper_command` when it
   has one, otherwise `python -m profiling.sampling.telemetry --plugin <id>`,
   which runs the plugin's `run_helper`.
4. The helper writes JSON lines `{"plugin", "event_type", "payload"}` on
   stdout. A reader thread parses them into a bounded queue; the sampling loop
   drains the queue after each sample without blocking.
5. Events reach the collector through `collect_plugin_event(plugin_id,
   event_type, payload)`.
6. In live mode `LiveStatsCollector` routes events to the plugin's
   `LiveTelemetryPlugin.ingest` and renders `render_lines` in the telemetry
   panel. It also calls `observe_sample(stack_frames, timestamp_ns)` for every
   CPU sample so a plugin can attribute its events to Python frames.
7. In binary mode `BinaryCollector` appends events to
   `<binary>.telemetrychunks`; `replay_sidecar_to_sink` reads them back for
   sinks that implement the plugin event API.
   In every other non-live format the events go to a `TelemetryRecordBuffer`
   and are printed as text after the CPU output, each plugin summarising its
   own records through `summarize_records`; `replay` into a non-live format
   does the same with the sidecar.
8. For `run`, the CLI merges each plugin's `target_environment` into the
   target's environment before launching it.

---

## Modules

- `plugin_registry.py`: plugin resolution (bundled directory scan by
  `PLUGIN_ID`, then dotted-module import) and hook dispatch.
- `manager.py`: one helper subprocess per plugin, reader threads, bounded event
  queue, helper exit reported as a `health` event with the stderr tail.
- `helper.py`: in-Python helper entrypoint; runs `run_helper` and exits.
- `chunks.py`: sidecar writer/reader (`.telemetrychunks`, `TCHUNK1` header,
  JSON lines).
- `replay.py`: replays sidecar records into a sink.
- `interfaces.py`: `LiveTelemetryPlugin`.
- `summary.py`: `TelemetryRecordBuffer` and the text summary for outputs
  without a live panel or sidecar.
- `plugins/`: bundled plugins, discovered by `PLUGIN_ID`.

---

## Plugin contract

A plugin module defines `PLUGIN_ID` and any of these hooks:

| hook | purpose |
|---|---|
| `create_live_plugin(config=None) -> LiveTelemetryPlugin \| None` | live TUI adapter; receives the resolved helper config if it takes an argument |
| `resolve_helper_config(config) -> dict` | fill helper config defaults |
| `helper_command(config) -> list[str] \| None` | argv of a native helper speaking the event protocol |
| `run_helper(config, emit)` | in-Python helper body; return when finished |
| `target_environment(config) -> dict` | environment for targets started by `run` |
| `register_cli_flags(parser)` | plugin options on `run` and `attach`, registered when the plugin is named with `--plugin` |
| `extract_cli_config(args) -> dict` | parsed plugin options merged over the helper config |
| `summarize_records(records) -> list[str] \| None` | text summary for non-live formats and `replay` |

`LiveTelemetryPlugin`:

- `panel_modes`: tuple of panel names cycled with `m`.
- `ingest(event_type, payload)`.
- `render_lines(mode, width)`: one entry per line, either a string or a list
  of `(text, style)` segments. Styles map onto the TUI colour scheme so panels
  match the CPU table: `header` and `sorted` (column headers), `good`, `warn`,
  `hot` (green, yellow, red for values to watch), `cyan`, `magenta`, `func`,
  `file`, `samples`, `trend_up`, `trend_down`, `bold`, `dim`, `normal`.
  Unknown styles render normally. The TUI sets `plugin.trends_enabled` from
  its `x` toggle before each render; `self.trend_style(key, metric, value,
  fallback)` on the base class tracks a numeric cell across renders and
  returns the trend style when it moved and trends are on, so any plugin
  colours changes like the CPU table without its own bookkeeping.
- `observe_sample(stack_frames, timestamp_ns)` (optional). `stack_frames` is
  the unwinder result; each thread carries `thread_id` (the Linux tid) and
  `frame_info`. `timestamp_ns` is `time.perf_counter_ns()`.

Event payload schemas are plugin-owned. Reserved `health` payload keys written
by the framework: `helper` (`failed`, `exited`, `overflow`, `finished`,
`error`), `returncode`, `stderr_tail`, `command`, `notes`.

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
- Plugins work with every output format: live panel, binary sidecar, or a
  text summary after the CPU output.

---

## Adding a plugin

1. Create a module (bundled under `telemetry/plugins/`, or anywhere
   importable) with `PLUGIN_ID` and the hooks above.
2. Implement a `LiveTelemetryPlugin` subclass for the TUI.
3. Enable it: `--plugin my_plugin` or `--plugin my_package.my_module`.

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
| Replay of plugin events into non-live collectors | built: text summary via `summarize_records` | profiler |
| Non-live capture in formats other than binary | built: events buffered and summarised after the CPU output | profiler |
