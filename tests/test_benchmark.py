import json
import fcntl
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import benchmark as b


class AdmissionTests(unittest.TestCase):
    def test_cgroup_discovery_keeps_cpu_only_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            child = mount / "child"
            child.mkdir()
            (mount / "cpu.max").write_text("150000 100000")
            for name, value in [("memory.current", "1000"), ("memory.max", "7000"),
                                ("memory.high", "max"), ("memory.events", "high 0\n")]:
                (child / name).write_text(value)
            original = Path.read_text
            def read(path, *args, **kwargs):
                if str(path) == "/proc/self/cgroup":
                    return "0::/child\n"
                if str(path) == "/proc/self/mountinfo":
                    return f"1 0 0:1 / {mount} rw - cgroup2 cgroup rw\n"
                return original(path, *args, **kwargs)
            with patch.object(Path, "read_text", read):
                groups = b.cgroup_dirs()
            self.assertEqual(groups, [child, mount])
            self.assertEqual(b.cpu_capacity(groups), 1.5)
            with patch.object(b, "fields", return_value={"MemAvailable": 100000}):
                self.assertEqual(b.memory_state(groups), (102400000, 6000))
            guard = b.PressureGuard(groups)
            self.assertEqual(guard.read_events(), {str(child): {"high": 0}})

    def test_cgroup_path_outside_mount_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            def read(path):
                if str(path) == "/proc/self/cgroup":
                    return "0::/../outside\n"
                return f"1 0 0:1 / {tmp} rw - cgroup2 cgroup rw\n"
            with patch.object(Path, "read_text", read):
                with self.assertRaisesRegex(RuntimeError, "escapes"):
                    b.cgroup_dirs()

    def test_budget_covers_all_arrays_and_thread_overhead(self):
        for available in (4, 8, 32):
            available *= 1024 * b.MIB
            size = b.memory_plan(available, available, 16 * b.MIB, 8)
            self.assertLessEqual(size + 192 * b.MIB, available // 4)
            self.assertGreaterEqual(size, 12 * 16 * b.MIB)

    def test_low_memory_is_refused_instead_of_using_cache_sized_buffers(self):
        with self.assertRaisesRegex(RuntimeError, "Insufficient memory"):
            b.memory_plan(512 * b.MIB, 512 * b.MIB, 16 * b.MIB, 2)

    def test_explicit_size_cannot_bypass_budget(self):
        with self.assertRaisesRegex(RuntimeError, "safety budget"):
            b.memory_plan(4 * 1024 * b.MIB, 4 * 1024 * b.MIB,
                          16 * b.MIB, 2, 2 * 1024 * b.MIB)

    def test_size_divides_into_whole_pages_at_every_scaling_count(self):
        size = b.memory_plan(32 * 1024 * b.MIB, 32 * 1024 * b.MIB, 33 * b.MIB, 24)
        for count in b.scaling_counts(24):
            self.assertEqual(size % (3 * count * os.sysconf("SC_PAGE_SIZE")), 0)

    def test_uneven_numa_selection_increases_required_size(self):
        caches = {(3, (0, 1, 2, 3)): 16 * b.MIB, (3, (4, 5, 6, 7)): 16 * b.MIB}
        self.assertEqual(b.cache_requirement([0, 1, 2, 4], caches), 64 * b.MIB)
        self.assertEqual(b.cache_requirement([0, 1, 4, 5], caches), 32 * b.MIB)

    def test_ancestor_memory_high_and_max_both_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = [Path(tmp) / "parent", Path(tmp) / "child"]
            for g, maximum, high, used in [(groups[0], "10000", "7000", "4000"),
                                           (groups[1], "max", "max", "1000")]:
                g.mkdir()
                for name, value in [("memory.max", maximum), ("memory.high", high),
                                    ("memory.current", used)]:
                    (g / name).write_text(value)
            with patch.object(b, "fields", return_value={"MemAvailable": 100000}):
                self.assertEqual(b.memory_state(groups), (102400000, 3000))

    def test_cpu_quota_uses_tightest_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = [Path(tmp) / "parent", Path(tmp) / "child"]
            for g, value in zip(groups, ["150000 100000", "max 100000"]):
                g.mkdir()
                (g / "cpu.max").write_text(value)
            self.assertEqual(b.cpu_capacity(groups), 1.5)

    def test_weighted_mean_not_average_of_interval_rates(self):
        samples = [{"bytes": 1000000, "seconds": .1}, {"bytes": 1000000, "seconds": .9}]
        result = b.statistics(samples)
        self.assertEqual(result["mean_MBps"], 2)
        self.assertAlmostEqual(result["median_MBps"], 1 / .9)

    def test_pressure_and_swap_abort(self):
        with patch.object(b, "memory_state", return_value=(8 * 1024 * b.MIB,) * 2), \
                patch.object(b, "fields", return_value={"pswpout": 0}):
            guard = b.PressureGuard([])
            guard.check()
            with patch.object(b, "memory_state", return_value=(10 * b.MIB,) * 2):
                with self.assertRaisesRegex(RuntimeError, "Memory pressure"):
                    guard.check()
            with patch.object(b, "fields", return_value={"pswpout": 1}):
                with self.assertRaisesRegex(RuntimeError, "swap-out"):
                    guard.check()

    def test_memory_event_and_cpu_throttling_abort(self):
        with patch.object(b, "memory_state", return_value=(8 * 1024 * b.MIB,) * 2), \
                patch.object(b, "fields", return_value={"pswpout": 0}), \
                patch.object(b.PressureGuard, "read_events", return_value={"group": {"high": 0}}), \
                patch.object(b.PressureGuard, "read_throttled", return_value={"group": 0}):
            guard = b.PressureGuard([])
            with patch.object(guard, "read_events", return_value={"group": {"high": 1}}):
                with self.assertRaisesRegex(RuntimeError, "cgroup memory pressure"):
                    guard.check()
            with patch.object(guard, "read_throttled", return_value={"group": 1}):
                with self.assertRaisesRegex(RuntimeError, "CPU throttling"):
                    guard.check()


class LauncherTests(unittest.TestCase):
    def test_first_launch_does_not_write_into_watched_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp)
            source = Path(b.__file__).parent
            for name in ("ram-speedtest", "benchmark.py", "hardware.py"):
                shutil.copy2(source / name, plugin / name)
            result = subprocess.run([sys.executable, str(plugin / "ram-speedtest"), "--help"],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((plugin / "__pycache__").exists())

    def test_second_run_does_not_overwrite_saved_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ram-speedtest"
            cache.mkdir()
            report = Path(tmp) / "result.json"
            previous = '{"valid": true, "preserve": "previous run"}\n'
            report.write_text(previous)
            with (cache / "run.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [sys.executable, str(Path(b.__file__).parent / "ram-speedtest"),
                     "--report", str(report)],
                    env={**os.environ, "XDG_CACHE_HOME": tmp},
                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already running", result.stderr)
            self.assertEqual(report.read_text(), previous)


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.binary, _ = b.build_native(Path(cls.tmp.name))
        cls.cpus = sorted(os.sched_getaffinity(0))[:2]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def command(self, mode, seconds="0.12"):
        return [str(self.binary), mode, str(6 * b.MIB), ",".join(map(str, self.cpus)), seconds, "0.02"]

    def test_copy_and_triad_bytes_validation_and_time(self):
        for mode, streams in [("copy", 2), ("triad", 3)]:
            with self.subTest(mode=mode):
                run = subprocess.run(self.command(mode), capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                records = list(map(json.loads, run.stdout.splitlines()))
                result, samples = records[-1], records[:-1]
                self.assertTrue(result["validated"])
                self.assertEqual(result["major_faults"], 0)
                self.assertGreaterEqual(result["seconds"], .12)
                self.assertEqual(sum(s["bytes"] for s in samples), result["bytes"])
                self.assertAlmostEqual(sum(s["seconds"] for s in samples), result["seconds"], places=7)
                per_pass = result["allocated_bytes"] // 3 * streams
                self.assertEqual(result["bytes"] % per_pass, 0)

    def test_invalid_native_inputs(self):
        cases = []
        for index, value in [(1, "bad"), (2, "-1"), (3, "999999"),
                             (3, f"{self.cpus[0]},{self.cpus[0]}"), (4, "nan"), (4, "0")]:
            cmd = self.command("copy")
            cmd[index] = value
            cases.append(cmd)
        for cmd in cases:
            with self.subTest(cmd=cmd):
                run = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(run.returncode, 0)

    def test_supervisor_cleans_up_after_pressure_abort(self):
        children = []
        original = subprocess.Popen
        def track(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        class Guard:
            def check(self, pid=None):
                raise RuntimeError("simulated pressure")
        with patch.object(b.subprocess, "Popen", side_effect=track):
            with self.assertRaisesRegex(RuntimeError, "simulated pressure"):
                b.run_native(self.binary, "copy", 6 * b.MIB, self.cpus,
                             10, .1, Guard(), lambda s: None)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_native_stops_if_supervisor_is_killed(self):
        # A disposable parent spawns the real native process, then is killed.
        script = "import subprocess,sys,time; p=subprocess.Popen(sys.argv[1:],stdout=subprocess.DEVNULL); print(p.pid,flush=True); time.sleep(30)"
        parent = subprocess.Popen([sys.executable, "-c", script, *self.command("copy", "20")],
                                  stdout=subprocess.PIPE, text=True)
        pid = int(parent.stdout.readline())
        try:
            time.sleep(.15)  # Allow prctl initialization.
            parent.kill()
            parent.wait(timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{pid}/stat").read_text().split()[2]
                    if state == "Z":
                        return
                except FileNotFoundError:
                    return
                time.sleep(.03)
            self.fail("Native workers survived their parent")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            parent.stdout.close()
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
