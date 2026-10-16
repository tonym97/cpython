"""Tests for the telemetry plugin framework (profiling.sampling.telemetry).

No real plugin helper is needed: the tests use plugins defined here and a
helper process that is plain Python printing JSON lines.
"""

import argparse
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

from test.support import os_helper

try:
    import _remote_debugging  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Test only runs when _remote_debugging is available")

from profiling.sampling import cli
from profiling.sampling.binary_collector import BinaryCollector
from profiling.sampling.collector import Collector
from profiling.sampling.gecko_collector import GeckoCollector
from profiling.sampling.live_collector import LiveStatsCollector
from profiling.sampling.pstats_collector import PstatsCollector
from profiling.sampling.sample import SampleProfiler
from profiling.sampling.telemetry import (
    GeckoContribution,
    GeckoSeries,
    ModuleHookPlugin,
    Segment,
    TelemetryChunkReader,
    TelemetryChunkWriter,
    TelemetryClock,
    TelemetryHelperManager,
    TelemetryPanel,
    TelemetryPlugin,
    TelemetryRecord,
    TelemetryRecordBuffer,
    get_plugin_class,
    helper_command,
    is_known_plugin,
    make_plugin,
    replay_sidecar_to_sink,
    summary_lines,
)
from profiling.sampling.telemetry import plugin_registry
from profiling.sampling.telemetry.panel import line_text

from .mocks import MockFrameInfo, MockInterpreterInfo, MockThreadInfo


# ---- plugins used by the tests ---------------------------------------------


class FakePanel(TelemetryPanel):
    panel_modes = ("counts", "health")

    def __init__(self):
        super().__init__()
        self.events = []
        self.samples = []
        self.version = 0

    def ingest(self, event_type, payload):
        self.events.append((event_type, payload))
        self.version += 1

    def observe_sample(self, stack_frames, timestamp_ns):
        self.samples.append(timestamp_ns)

    def render_lines(self, mode, width):
        if mode == "health":
            return ["healthy"]
        return [
            [Segment("events ", "header"), Segment(str(len(self.events)), "samples")],
            f"mode={mode} width={width}",
        ]


class FakePlugin(TelemetryPlugin):
    plugin_id = "fake_plugin"

    @classmethod
    def default_config(cls):
        return {"rate": 5, "flag": False}

    @classmethod
    def register_cli_flags(cls, parser):
        parser.add_argument("--fake-plugin-rate", type=int, default=argparse.SUPPRESS)
        parser.add_argument("--fake-plugin-flag", action="store_true", default=False)

    @classmethod
    def extract_cli_config(cls, args):
        config = {}
        if getattr(args, "fake_plugin_rate", None) is not None:
            config["rate"] = args.fake_plugin_rate
        if getattr(args, "fake_plugin_flag", False):
            config["flag"] = True
        return config

    def run_helper(self, emit):
        emit(self.plugin_id, "tick", {"rate": self.config["rate"]})

    def target_environment(self):
        return {"FAKE_PLUGIN": "1"}

    def create_panel(self):
        return FakePanel()

    def summarize(self, records):
        return [f"{len(records)} records, first {records[0].event_type}"]

    def gecko_contribution(self, records, clock):
        contribution = GeckoContribution()
        track = contribution.add_track("fake events", process_name="Fake")
        graph = contribution.add_graph(
            "fake rate", [GeckoSeries("rate", "Rate", "green", "decimal")],
            process_name="Fake", schema_name="FakeRate")
        for record in records:
            if record.event_type != "tick":
                continue
            start = clock.to_ms(record.payload["t_utc_ns"])
            track.add_marker("tick", start, start + 1.0, {"type": "FakeTick"})
            graph.add_point(start - 1.0, start, {"rate": record.payload["rate"]})
            contribution.add_thread_marker(
                record.payload["tid"], "tick on thread", start, None, {"type": "FakeTick"})
        contribution.marker_schemas.append({
            "name": "FakeTick",
            "display": ["marker-chart"],
            "fields": [{"key": "note", "label": "Note", "format": "number"}],
            "graphs": [{"key": "rate", "color": "lightblue"}],
        })
        return contribution


FAKES_PACKAGE = "test_telemetry_fakes"
FAKE_ID = FAKES_PACKAGE + ".fake"


def _install_module(name, **attrs):
    """Register ``name`` (dotted, under FAKES_PACKAGE) as an importable module."""
    if FAKES_PACKAGE not in sys.modules:
        package = types.ModuleType(FAKES_PACKAGE)
        package.__path__ = []
        sys.modules[FAKES_PACKAGE] = package
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    plugin_registry._EXTERNAL.pop(name, None)
    return module


def _records(plugin_id, *events):
    return [TelemetryRecord(plugin_id, event_type, payload) for event_type, payload in events]


class TelemetryTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_modules = dict(sys.modules)
        self._saved_external = dict(plugin_registry._EXTERNAL)
        self.plugin_module = _install_module(FAKE_ID, PLUGIN=FakePlugin)
        self.addCleanup(self._restore)

    def _restore(self):
        for name in set(sys.modules) - set(self._saved_modules):
            del sys.modules[name]
        plugin_registry._EXTERNAL.clear()
        plugin_registry._EXTERNAL.update(self._saved_external)


# ---- registry and plugin base -----------------------------------------------


class TestPluginRegistry(TelemetryTestCase):
    def test_dotted_module_with_plugin_class(self):
        cls = get_plugin_class(FAKE_ID)
        self.assertIs(cls, FakePlugin)
        self.assertTrue(is_known_plugin(FAKE_ID))
        plugin = make_plugin(FAKE_ID, {"rate": 9})
        self.assertIsInstance(plugin, FakePlugin)
        self.assertEqual(plugin.config, {"rate": 9, "flag": False})

    def test_unknown_plugin(self):
        self.assertIsNone(get_plugin_class("no_such_plugin"))
        self.assertIsNone(get_plugin_class("no.such.module_for_telemetry"))
        self.assertIsNone(make_plugin(""))
        self.assertFalse(is_known_plugin("no_such_plugin"))

    def test_plugin_class_without_id_gets_module_name(self):
        class Anonymous(TelemetryPlugin):
            pass
        _install_module(FAKES_PACKAGE + ".test_anonymous_telemetry_plugin", PLUGIN=Anonymous)
        cls = get_plugin_class(FAKES_PACKAGE + ".test_anonymous_telemetry_plugin")
        self.assertEqual(cls.plugin_id, FAKES_PACKAGE + ".test_anonymous_telemetry_plugin")

    def test_module_hook_plugin_adapter(self):
        seen = {}

        def create_live_plugin(config=None):
            seen["panel_config"] = config
            return FakePanel()

        module = _install_module(
            FAKES_PACKAGE + ".test_hook_telemetry_plugin",
            PLUGIN_ID="hooked",
            resolve_helper_config=lambda config=None: {"interval_ms": 7, **(config or {})},
            helper_command=lambda config: ["helper", "--pid", str(config["target_pid"])],
            target_environment=lambda config: {"HOOKED": str(config["interval_ms"])},
            register_cli_flags=lambda parser: parser.add_argument("--hooked-x", default=argparse.SUPPRESS),
            extract_cli_config=lambda args: {"x": args.hooked_x} if hasattr(args, "hooked_x") else {},
            create_live_plugin=create_live_plugin,
            summarize_records=lambda records: [f"{len(records)} tuples {records[0][1]}"],
            gecko_contribution=lambda records, clock: seen.setdefault("gecko", records),
        )
        cls = get_plugin_class(FAKES_PACKAGE + ".test_hook_telemetry_plugin")
        self.assertTrue(issubclass(cls, ModuleHookPlugin))
        self.assertEqual(cls.plugin_id, "hooked")
        self.assertEqual(cls.default_config(), {"interval_ms": 7})

        parser = argparse.ArgumentParser()
        cls.register_cli_flags(parser)
        args = parser.parse_args(["--hooked-x", "1"])
        self.assertEqual(cls.extract_cli_config(args), {"x": "1"})

        plugin = cls(cls.resolve_config({"target_pid": 42}))
        self.assertEqual(plugin.helper_command(), ["helper", "--pid", "42"])
        self.assertEqual(plugin.target_environment(), {"HOOKED": "7"})
        self.assertIsInstance(plugin.create_panel(), FakePanel)
        self.assertEqual(seen["panel_config"], plugin.config)

        records = _records("hooked", ("a", {}), ("b", {}))
        self.assertEqual(plugin.summarize(records), ["2 tuples a"])
        plugin.gecko_contribution(records, None)
        self.assertEqual(seen["gecko"], [("hooked", "a", {}), ("hooked", "b", {})])

    def test_module_hook_plugin_without_hooks(self):
        _install_module(FAKES_PACKAGE + ".test_bare_hook_plugin", PLUGIN_ID="bare")
        plugin = make_plugin(FAKES_PACKAGE + ".test_bare_hook_plugin")
        self.assertEqual(plugin.config, {})
        self.assertIsNone(plugin.helper_command())
        self.assertIsNone(plugin.create_panel())
        self.assertIsNone(plugin.summarize([]))
        self.assertEqual(plugin.target_environment(), {})
        emitted = []
        plugin.run_helper(lambda *event: emitted.append(event))
        self.assertEqual(emitted[0][:2], ("bare", "health"))

    def test_helper_command_defaults_to_python_helper(self):
        plugin = make_plugin(FAKE_ID, {"target_pid": 3})
        command = helper_command(plugin)
        self.assertEqual(command[:3], [sys.executable, "-m", "profiling.sampling.telemetry"])
        self.assertIn("--plugin", command)
        self.assertEqual(json.loads(command[-1]), plugin.config)

    def test_helper_command_uses_native_helper(self):
        class Native(FakePlugin):
            def helper_command(self):
                return ("native-helper", "--pid", str(self.config["target_pid"]))
        plugin = Native({"target_pid": 5})
        self.assertEqual(helper_command(plugin), ["native-helper", "--pid", "5"])

    def test_base_plugin_defaults(self):
        plugin = TelemetryPlugin()
        self.assertEqual(plugin.config, {})
        self.assertIsNone(plugin.helper_command())
        self.assertIsNone(plugin.create_panel())
        self.assertIsNone(plugin.summarize([]))
        self.assertIsNone(plugin.gecko_contribution([], None))
        self.assertEqual(TelemetryPlugin.resolve_config({"a": 1}), {"a": 1})


# ---- helper manager ---------------------------------------------------------


HELPER_SCRIPT = textwrap.dedent("""
    import json, sys, time
    for i in range(3):
        print(json.dumps({"plugin": "p", "event_type": "tick", "payload": {"i": i}}), flush=True)
    print("not json", flush=True)
    print("", flush=True)
    sys.stderr.write("helper warning\\n")
    sys.stderr.flush()
    sys.exit(int(sys.argv[1]))
""")


class TestHelperManager(unittest.TestCase):
    def _run_helper(self, exit_code):
        manager = TelemetryHelperManager("p", [sys.executable, "-c", HELPER_SCRIPT, str(exit_code)])
        manager.start()
        deadline = time.monotonic() + 30
        while manager.alive and time.monotonic() < deadline:
            time.sleep(0.01)
        manager.stop()
        return manager.poll()

    def test_events_and_exit_report(self):
        events = self._run_helper(3)
        ticks = [event for event in events if event["event_type"] == "tick"]
        self.assertEqual([event["payload"]["i"] for event in ticks], [0, 1, 2])
        health = [event for event in events if event["event_type"] == "health"]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["payload"]["helper"], "exited")
        self.assertEqual(health[0]["payload"]["returncode"], 3)
        self.assertEqual(health[0]["payload"]["stderr_tail"], ["helper warning"])
        self.assertEqual(health[0]["plugin"], "p")

    def test_poll_after_stop_is_idempotent(self):
        manager = TelemetryHelperManager("p", [sys.executable, "-c", HELPER_SCRIPT, "0"])
        manager.start()
        manager.stop()
        first = manager.poll()
        self.assertTrue(any(event["event_type"] == "health" for event in first))
        self.assertEqual(manager.poll(), [])
        self.assertFalse(manager.alive)

    def test_missing_executable(self):
        manager = TelemetryHelperManager("p", ["/nonexistent/telemetry-helper"])
        manager.start()
        events = manager.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["helper"], "failed")
        manager.stop()
        self.assertEqual(manager.poll(), [])

    def test_queue_overflow_is_reported(self):
        manager = TelemetryHelperManager("p", ["unused"])
        for i in range(manager._events.maxlen + 5):
            manager._push({"plugin": "p", "event_type": "tick", "payload": {"i": i}})
        events = manager.poll()
        self.assertEqual(events[-1]["payload"]["helper"], "overflow")
        self.assertEqual(len(events), manager._events.maxlen + 1)


# ---- records, summary and sidecar -------------------------------------------


class TestRecordsAndSummary(TelemetryTestCase):
    def test_buffer_groups_and_bounds(self):
        buffer = TelemetryRecordBuffer(max_records=2)
        self.assertTrue(buffer.accepts_plugin_events)
        self.assertFalse(buffer.accepts_sample_timestamp)
        buffer.set_plugin_enabled("b")
        buffer.collect_plugin_event("a", "x", {"n": 1})
        buffer.set_plugin_metadata("a", {"provider": "test"})
        buffer.collect_plugin_event("a", "x", {"n": 2})
        self.assertEqual(len(buffer), 2)
        self.assertEqual(buffer.dropped, 1)
        groups = buffer.by_plugin()
        self.assertEqual(list(groups), ["b", "a"])
        self.assertEqual(groups["b"], [])
        self.assertEqual([r.event_type for r in groups["a"]], ["x", "metadata"])
        self.assertEqual(list(buffer)[0], TelemetryRecord("a", "x", {"n": 1}))

    def test_summary_lines_use_plugin_and_generic_fallback(self):
        buffer = TelemetryRecordBuffer()
        buffer.collect_plugin_event(FAKE_ID, "tick", {})
        buffer.collect_plugin_event(FAKE_ID, "tick", {})
        buffer.collect_plugin_event("unknown_plugin", "a", {})
        buffer.collect_plugin_event("unknown_plugin", "b", {})
        buffer.collect_plugin_event("unknown_plugin", "b", {})
        buffer.dropped = 4
        self.assertEqual(summary_lines(buffer), [
            f"Telemetry [{FAKE_ID}]",
            "  2 records, first tick",
            "Telemetry [unknown_plugin]",
            "  3 events (a: 1, b: 2)",
            "  4 events dropped (buffer full)",
        ])

    def test_summary_lines_empty(self):
        self.assertEqual(summary_lines(None), [])
        self.assertEqual(summary_lines(TelemetryRecordBuffer()), [])

    def test_sidecar_roundtrip_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "profile.bin")
            writer = TelemetryChunkWriter(binary)
            writer.write_record("p", "metadata", {"provider": "test"})
            writer.write_record("p", "tick", {"i": 1})
            writer.write_record("", "ignored", {})
            writer.close()
            reader = TelemetryChunkReader(binary)
            self.assertTrue(reader.exists())
            with open(reader.path if hasattr(reader, "path") else binary + ".telemetrychunks") as f:
                self.assertEqual(f.readline().strip(), "TCHUNK1")

            buffer = TelemetryRecordBuffer()
            self.assertEqual(replay_sidecar_to_sink(binary, buffer), 2)
            self.assertEqual([r.event_type for r in buffer], ["metadata", "tick"])
            self.assertEqual(buffer.plugin_ids, ["p"])

            # Collectors that do not accept plugin events are left alone.
            self.assertEqual(replay_sidecar_to_sink(binary, PstatsCollector(1000)), 0)
            self.assertEqual(replay_sidecar_to_sink(os.path.join(tmp, "none.bin"), buffer), 0)

    def test_binary_collector_writes_sidecar(self):
        self.assertTrue(BinaryCollector.accepts_plugin_events)
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "profile.bin")
            collector = BinaryCollector(binary, 1000)
            collector.set_plugin_enabled("p")
            collector.set_plugin_metadata("p", {"provider": "test"})
            collector.collect_plugin_event("p", "tick", {"i": 1})
            collector.export(binary)
            records = list(TelemetryChunkReader(binary).iter_records())
            self.assertEqual([r["event_type"] for r in records], ["metadata", "tick"])

    def test_base_collector_ignores_plugin_events(self):
        self.assertFalse(Collector.accepts_plugin_events)
        self.assertFalse(Collector.accepts_sample_timestamp)
        collector = PstatsCollector(1000)
        collector.set_plugin_enabled("p")
        collector.set_plugin_metadata("p", {})
        collector.collect_plugin_event("p", "tick", {})  # no-ops


# ---- sampler integration ----------------------------------------------------


class FakeManager:
    def __init__(self, plugin_id, events):
        self.plugin_id = plugin_id
        self._events = list(events)
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def poll(self):
        events, self._events = self._events, []
        return events


class TestSamplerIntegration(TelemetryTestCase):
    def _profiler(self, collector, manager):
        profiler = SampleProfiler.__new__(SampleProfiler)
        profiler.telemetry_managers = [manager]
        profiler.telemetry_buffer = None
        return profiler

    def test_poll_routes_to_collector_or_buffer(self):
        events = [
            {"plugin": "p", "event_type": "tick", "payload": {"i": 1}},
            {"event_type": "tick", "payload": {"i": 2}},  # plugin defaults to the manager's
            {"payload": {}},  # no event type: dropped
        ]
        gecko = GeckoCollector(1000)
        profiler = self._profiler(gecko, FakeManager("p", events))
        profiler._poll_telemetry_records(gecko)
        self.assertEqual([r.payload["i"] for r in gecko._telemetry], [1, 2])

        pstats = PstatsCollector(1000)
        profiler = self._profiler(pstats, FakeManager("p", events))
        profiler.telemetry_buffer = TelemetryRecordBuffer()
        profiler._poll_telemetry_records(pstats)
        self.assertEqual(len(profiler.telemetry_buffer), 2)

    def test_constructor_builds_managers_with_target_pid(self):
        plugin = FakePlugin(FakePlugin.resolve_config())
        with mock.patch("profiling.sampling.sample._resolve_python_pid", return_value=1234), \
             mock.patch("profiling.sampling.sample._remote_debugging.RemoteUnwinder"):
            profiler = SampleProfiler(1234, 1000, all_threads=False, telemetry_plugins=[plugin])
        self.assertEqual(plugin.config["target_pid"], 1234)
        self.assertEqual(len(profiler.telemetry_managers), 1)
        manager = profiler.telemetry_managers[0]
        self.assertEqual(manager.plugin_id, "fake_plugin")
        self.assertEqual(manager.command[:2], [sys.executable, "-m"])


# ---- live panel and widget --------------------------------------------------


class TestPanel(unittest.TestCase):
    def test_trend_style_respects_toggle_and_version(self):
        panel = FakePanel()
        self.assertEqual(panel.trend_style("k", "m", 1.0, "normal", version=1), "normal")
        panel.set_trends_enabled(True)
        self.assertEqual(panel.trend_style("k", "m", 2.0, "normal", version=2), "trend_up")
        # Same data version: held, even though the value is unchanged now.
        self.assertEqual(panel.trend_style("k", "m", 2.0, "normal", version=2), "trend_up")
        self.assertEqual(panel.trend_style("k", "m", 2.0, "normal", version=3), "normal")
        self.assertEqual(panel.trend_style("k", "m", 1.5, "cyan", version=4), "trend_down")
        panel.set_trends_enabled(False)
        self.assertEqual(panel.trend_style("k", "m", 9.0, "cyan", version=5), "cyan")

    def test_line_text(self):
        self.assertEqual(line_text("plain"), "plain")
        self.assertEqual(line_text([Segment("a ", "header"), ("b", "dim")]), "a b")
        self.assertEqual(Segment("x").style, "normal")

    def test_base_panel_requires_ingest_and_render(self):
        panel = TelemetryPanel()
        with self.assertRaises(NotImplementedError):
            panel.ingest("x", {})
        with self.assertRaises(NotImplementedError):
            panel.render_lines("default", 80)
        panel.observe_sample([], 0)  # default is a no-op


class TestLiveCollector(TelemetryTestCase):
    def _collector(self, plugins):
        return LiveStatsCollector(1000, telemetry_plugins=plugins)

    def test_panels_from_plugin_instances(self):
        plugin = FakePlugin({})
        collector = self._collector([plugin])
        self.assertTrue(collector.accepts_plugin_events)
        self.assertTrue(collector.accepts_sample_timestamp)
        self.assertTrue(collector.show_telemetry_panel)
        self.assertEqual(collector.telemetry_plugins_for_sampler, [plugin])
        plugin_id, panel = collector._get_current_telemetry_plugin()
        self.assertEqual(plugin_id, "fake_plugin")
        self.assertIsInstance(panel, FakePanel)
        collector.collect_plugin_event("fake_plugin", "tick", {"i": 1})
        self.assertEqual(panel.events, [("tick", {"i": 1})])
        self.assertEqual(collector.current_telemetry_mode_idx, 0)
        collector._cycle_telemetry_mode()
        self.assertEqual(collector.current_telemetry_mode_idx, 1)
        collector._cycle_telemetry_mode()
        self.assertEqual(collector.current_telemetry_mode_idx, 0)

    def test_panel_created_for_events_from_unlisted_plugin(self):
        collector = self._collector(None)
        self.assertFalse(collector.show_telemetry_panel)
        collector.collect_plugin_event(FAKE_ID, "tick", {})
        plugin_id, panel = collector._get_current_telemetry_plugin()
        self.assertEqual(plugin_id, FAKE_ID)
        self.assertEqual(len(panel.events), 1)
        collector.collect_plugin_event("unknown_plugin", "tick", {})
        self.assertEqual(collector.telemetry_plugin_order, [FAKE_ID])

    def test_observe_sample_gets_unwind_timestamp(self):
        collector = self._collector([FakePlugin({})])
        _, panel = collector._get_current_telemetry_plugin()
        frames = [MockInterpreterInfo(0, [MockThreadInfo(1, [MockFrameInfo("f.py", 1, "func")])])]
        collector.collect(frames, timestamp_us=123_456)
        self.assertEqual(panel.samples, [123_456_000])
        collector.collect(frames)
        self.assertEqual(len(panel.samples), 2)
        self.assertGreater(panel.samples[1], panel.samples[0])

    def test_widget_renders_panel_lines(self):
        from profiling.sampling.live_collector.widgets import TelemetryPanelWidget

        class Display:
            def __init__(self):
                self.rows = {}

            def get_attr(self, name):
                return 0

            def add_str(self, line, col, text, attr=0):
                self.rows.setdefault(line, []).append((col, text, attr))

        collector = self._collector([FakePlugin({})])
        collector._trend_tracker = types.SimpleNamespace(enabled=True)
        _, panel = collector._get_current_telemetry_plugin()
        panel.ingest("tick", {})
        display = Display()
        widget = TelemetryPanelWidget(display, {"normal_header": 7, "color_samples": 3}, collector)
        end = widget.render(0, 60)
        self.assertTrue(panel.trends_enabled)
        self.assertEqual(end, 3)  # title + two panel lines
        self.assertIn("Telemetry [fake_plugin] (COUNTS)", display.rows[0][0][1])
        self.assertEqual([(c, s) for c, s, _ in display.rows[1]][:2], [(0, "events "), (7, "1")])
        self.assertEqual(display.rows[1][0][2], 7)  # header style
        self.assertEqual(display.rows[1][1][2], 3)  # samples style
        self.assertEqual(display.rows[2][0][1], "mode=counts width=60")

        collector._cycle_telemetry_mode()
        display.rows.clear()
        widget.render(0, 60)
        self.assertEqual(display.rows[1][0][1], "healthy")

        empty = self._collector(None)
        widget = TelemetryPanelWidget(display, {}, empty)
        display.rows.clear()
        self.assertEqual(widget.render(0, 60), 1)
        self.assertEqual(display.rows[0][0][1], "No telemetry plugins active.")


# ---- gecko export -----------------------------------------------------------


class TestGeckoExport(TelemetryTestCase):
    def test_clock(self):
        clock = TelemetryClock(base_monotonic_us=1_000_000, utc_ns=5_000_000_000, monotonic_ns=1_000_000_000)
        self.assertEqual(clock.to_ms(5_000_000_000), 0.0)
        self.assertEqual(clock.to_ms(5_010_000_000), 10.0)
        self.assertIsNone(clock.to_ms(None))
        self.assertIsNone(clock.to_ms(0))
        records = _records("p", ("tick", {}), ("metadata", {"clock": {"utc_ns": 7, "monotonic_ns": 2}}))
        self.assertEqual(TelemetryClock.from_records(0, records).utc_minus_monotonic_ns, 5)
        self.assertEqual(TelemetryClock.from_records(0, [], (9, 4)).utc_minus_monotonic_ns, 5)
        self.assertIsNone(TelemetryClock.from_records(0, []))

    def test_contribution_dataclasses(self):
        contribution = GeckoContribution()
        self.assertTrue(contribution.is_empty())
        track = contribution.add_track("t")
        marker = track.add_marker("m", 1.0, 2.0, {"a": 1})
        self.assertEqual((marker.name, marker.start_ms, marker.end_ms, marker.data, marker.tid),
                         ("m", 1.0, 2.0, {"a": 1}, None))
        thread_marker = contribution.add_thread_marker(77, "n", 3.0)
        self.assertEqual((thread_marker.tid, thread_marker.end_ms), (77, None))
        graph = contribution.add_graph("g", [GeckoSeries("k")], schema_name="G")
        graph.add_point(0.0, 1.0, {"k": 2})
        self.assertEqual(graph.points[0].values, {"k": 2})
        self.assertEqual(graph.series[0].format, "decimal")
        self.assertFalse(contribution.is_empty())

    def test_export_adds_tracks_markers_graphs_and_schemas(self):
        collector = GeckoCollector(1000)
        utc0, mono0 = collector._telemetry_clock_pair
        start_mono_ns = int(collector.start_time * 1_000_000)
        plugin_id = FAKE_ID
        collector.set_plugin_enabled(plugin_id)
        collector.set_plugin_metadata(plugin_id, {"clock": {"utc_ns": utc0, "monotonic_ns": mono0}})
        # Sample one Python thread so a thread marker can land on it.
        frames = [MockInterpreterInfo(0, [MockThreadInfo(555, [MockFrameInfo("f.py", 1, "func")])])]
        collector.collect(frames)
        for i in range(3):
            t_utc = utc0 + (start_mono_ns + (i + 1) * 10_000_000 - mono0)
            tid = 555 if i < 2 else 999  # last one on an unsampled thread
            collector.collect_plugin_event(plugin_id, "tick", {"t_utc_ns": t_utc, "rate": i, "tid": tid})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profile.json")
            collector.export(path)
            with open(path) as f:
                profile = json.load(f)
        threads = {thread["name"]: thread for thread in profile["threads"]}
        self.assertIn("fake events", threads)
        self.assertIn("fake rate", threads)
        self.assertIn(f"{FAKE_ID} (other threads)", threads)
        self.assertEqual(threads["fake events"]["processName"], "Fake")
        self.assertEqual(threads["fake events"]["markers"]["length"], 3)
        self.assertEqual(threads["fake rate"]["markers"]["length"], 3)
        self.assertEqual(threads[f"{FAKE_ID} (other threads)"]["markers"]["length"], 1)
        python_threads = [t for t in profile["threads"] if t.get("tid") == 555]
        self.assertEqual(len(python_threads), 1)
        python_ticks = [d for d in python_threads[0]["markers"]["data"] if d.get("type") == "FakeTick"]
        self.assertEqual(len(python_ticks), 2)
        starts = threads["fake events"]["markers"]["startTime"]
        self.assertEqual([round(s, 3) for s in starts], [10.0, 20.0, 30.0])
        ends = threads["fake rate"]["markers"]["endTime"]
        self.assertEqual([round(e, 3) for e in ends], [10.0, 20.0, 30.0])
        self.assertIsNone(threads[f"{FAKE_ID} (other threads)"]["markers"]["endTime"][0])

        schemas = {schema["name"]: schema for schema in profile["meta"]["markerSchema"]}
        self.assertIn("FakeTick", schemas)
        self.assertIn("FakeRate", schemas)
        # Invalid formats and colours are replaced with ones the profiler accepts.
        self.assertNotEqual(schemas["FakeTick"]["fields"][0]["format"], "number")
        self.assertNotEqual(schemas["FakeTick"]["graphs"][0]["color"], "lightblue")
        self.assertEqual(schemas["FakeRate"]["display"], [])
        self.assertEqual(schemas["FakeRate"]["graphs"][0]["key"], "rate")

    def test_export_without_contribution(self):
        collector = GeckoCollector(1000)
        collector.collect_plugin_event("unknown_plugin", "tick", {})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "profile.json")
            collector.export(path)
            with open(path) as f:
                profile = json.load(f)
        self.assertEqual([t for t in profile["threads"] if t["processName"] == "Telemetry"], [])


# ---- CLI --------------------------------------------------------------------


class TestCli(TelemetryTestCase):
    def test_plugins_named_on_command_line(self):
        argv = ["run", "--plugin", "a", "--plugin=b", "--plugin", "a", "x.py"]
        self.assertEqual(cli._plugins_named_on_command_line(argv), ["a", "b"])

    def test_plugin_flags_registered_when_named(self):
        argv = ["run", "--plugin", FAKE_ID, "--fake-plugin-rate", "11",
                "--fake-plugin-flag", "x.py"]
        with mock.patch.object(sys, "argv", ["profiling"] + argv):
            parser = argparse.ArgumentParser()
            cli._add_plugin_cli_flags(parser)
            args = parser.parse_args(["--fake-plugin-rate", "11", "--fake-plugin-flag"])
        args.plugins = [FAKE_ID]
        plugins = cli._build_telemetry_plugins(args)
        self.assertEqual(len(plugins), 1)
        self.assertEqual(plugins[0].config, {"rate": 11, "flag": True})
        self.assertEqual(cli._telemetry_target_environment(args), {"FAKE_PLUGIN": "1"})

    def test_unknown_plugin_flags_not_registered(self):
        with mock.patch.object(sys, "argv", ["profiling", "run", "--plugin", "no_such_plugin", "x.py"]):
            parser = argparse.ArgumentParser()
            cli._add_plugin_cli_flags(parser)
        self.assertEqual(cli._build_telemetry_plugins(types.SimpleNamespace(plugins=["no_such_plugin"])), None)

    def test_unknown_plugin_rejected(self):
        with mock.patch.object(sys, "argv", ["profiling", "run", "--plugin", "no_such_plugin", "x.py"]), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, \
             self.assertRaises(SystemExit):
            cli.main()
        self.assertIn("no_such_plugin", stderr.getvalue())

    def test_summary_printing(self):
        buffer = TelemetryRecordBuffer()
        buffer.collect_plugin_event(FAKE_ID, "tick", {})
        collector = types.SimpleNamespace(telemetry_records=buffer)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            cli._print_telemetry_summary(collector)
            cli._print_telemetry_summary(types.SimpleNamespace())
        self.assertIn(f"Telemetry [{FAKE_ID}]", stdout.getvalue())
        self.assertIn("1 records, first tick", stdout.getvalue())

    def test_sidecar_summary_only_for_collectors_without_plugin_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = os.path.join(tmp, "profile.bin")
            writer = TelemetryChunkWriter(binary)
            writer.write_record(FAKE_ID, "tick", {})
            writer.close()
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                cli._print_sidecar_summary(binary, PstatsCollector(1000))
                self.assertIn("1 records", stdout.getvalue())
            with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                cli._print_sidecar_summary(binary, GeckoCollector(1000))
                self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
