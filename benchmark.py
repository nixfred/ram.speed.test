"""Linux supervisor for sustained native memory-throughput measurements."""
import argparse
import collections
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from hardware import describe_ram

MIB = 1 << 20
SYS_CPU = Path("/sys/devices/system/cpu")


def read_int(path):
    return int(Path(path).read_text().strip())


def fields(path):
    return {p[0].rstrip(":"): int(p[1]) for line in Path(path).read_text().splitlines()
            if len(p := line.split()) >= 2 and p[1].isdigit()}


def cpu_list(value):
    result = set()
    for part in value.strip().split(","):
        ends = part.split("-")
        if len(ends) > 2:
            raise ValueError("Invalid CPU range")
        lo, hi = int(ends[0]), int(ends[-1])
        if lo < 0 or hi < lo or hi >= 1024:
            raise ValueError("CPU IDs must be between 0 and 1023")
        result.update(range(lo, hi + 1))
    return sorted(result)


def physical_cpus(allowed):
    # One logical CPU per physical core, interleaved across sockets.
    packages = collections.defaultdict(list)
    seen = set()
    for cpu in sorted(allowed):
        topology = SYS_CPU / f"cpu{cpu}" / "topology"
        package = read_int(topology / "physical_package_id")
        core = read_int(topology / "core_id")
        if (package, core) not in seen:
            seen.add((package, core))
            packages[package].append(cpu)
    result = []
    while any(packages.values()):
        for package in sorted(packages):
            if packages[package]:
                result.append(packages[package].pop(0))
    return result


def cache_size(text):
    text = text.strip().upper()
    return int(text[:-1]) * {"K": 1024, "M": MIB, "G": 1 << 30}[text[-1]]


def cache_domains(cpus):
    caches = {}
    for cpu in cpus:
        candidates = []
        for entry in (SYS_CPU / f"cpu{cpu}" / "cache").glob("index*"):
            if (entry / "type").read_text().strip() in ("Data", "Unified"):
                candidates.append((read_int(entry / "level"), entry))
        if not candidates:
            raise RuntimeError("Cannot establish last-level cache size; refusing a cache-sized test")
        level, entry = max(candidates)
        shared = tuple(cpu_list((entry / "shared_cpu_list").read_text()))
        caches[(level, shared)] = cache_size((entry / "size").read_text())
    return caches


def cache_requirement(cpus, caches):
    # Equal-sized worker arrays must exceed each active cache domain, even
    # when the selected cores are distributed unevenly between sockets.
    requirement = 0
    for (_, shared), size in caches.items():
        participating = len(set(shared).intersection(cpus))
        if participating:
            requirement = max(requirement, math.ceil(size * len(cpus) / participating))
    return requirement


def cgroup_dirs():
    """Resolve cgroup v2 and all visible ancestor limits, including mount roots."""
    lines = Path("/proc/self/cgroup").read_text().splitlines()
    unified = next((line[3:] for line in lines if line.startswith("0::")), None)
    if unified is None:
        raise RuntimeError("cgroup v2 is required for reliable memory-limit checks")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        if right.split()[0] != "cgroup2":
            continue
        parts = left.split()
        def unescape(s):
            return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), s)
        root, mount = Path(unescape(parts[3])), Path(unescape(parts[4]))
        try:
            relative = Path(unified).relative_to(root)
        except ValueError:
            continue
        current = (mount / relative).resolve()
        mount = mount.resolve()
        if not current.is_relative_to(mount):
            raise RuntimeError("Cgroup path escapes its visible mount")
        result = []
        while True:
            if not current.is_dir():
                raise RuntimeError("Cannot resolve cgroup memory limits")
            # CPU and memory controllers can be enabled at different levels.
            result.append(current)
            if current == mount:
                return result
            current = current.parent
    raise RuntimeError("Cannot find the cgroup v2 mount")


def memory_state(groups):
    available = fields("/proc/meminfo")["MemAvailable"] * 1024
    limits = []
    for group in groups:
        if not (group / "memory.current").exists():
            continue
        used = read_int(group / "memory.current")
        for name in ("memory.max", "memory.high"):
            value = (group / name).read_text().strip()
            if value != "max":
                limits.append(max(0, int(value) - used))
    return available, min(limits, default=available)


def cpu_capacity(groups):
    limits = []
    for group in groups:
        path = group / "cpu.max"
        if path.exists():
            quota, period = path.read_text().split()
            if quota != "max":
                limits.append(int(quota) / int(period))
    return min(limits, default=math.inf)


def memory_plan(available, headroom, llc, workers, requested=None):
    # All three arrays combined, with allowance for stacks and the runtime.
    overhead = 128 * MIB + workers * 8 * MIB
    safe = max(0, min(available, headroom) // 4 - overhead)
    # Every scaling stage must divide into whole-page worker arrays without
    # rounding a just-large-enough dataset back below the cache requirement.
    alignment = 3 * math.lcm(*scaling_counts(workers)) * os.sysconf("SC_PAGE_SIZE")
    minimum = max(192 * MIB, 12 * llc)
    minimum = (minimum + alignment - 1) // alignment * alignment
    target = requested if requested is not None else max(768 * MIB, minimum)
    if requested is None:
        target = min(target, 2 * 1024 * MIB, safe)
    target = target // alignment * alignment
    if target < minimum:
        raise RuntimeError("Insufficient memory within the safety budget to exceed caches "
                           f"(need at least {math.ceil(minimum / MIB)} MiB of arrays)")
    if target > safe:
        raise RuntimeError("Requested memory exceeds the safety budget (one quarter of headroom)")
    return target


class PressureGuard:
    def __init__(self, groups):
        self.groups = groups
        available, headroom = memory_state(groups)
        self.reserve = min(512 * MIB, available // 8)
        self.group_reserve = min(256 * MIB, headroom // 8)
        self.events = self.read_events()
        self.swapout = fields("/proc/vmstat").get("pswpout", 0)
        self.throttled = self.read_throttled()

    def read_events(self):
        return {str(g): fields(g / "memory.events") for g in self.groups
                if (g / "memory.current").exists()}

    def read_throttled(self):
        return {str(g): fields(g / "cpu.stat").get("nr_throttled", 0)
                for g in self.groups if (g / "cpu.stat").exists()}

    def check(self, pid=None):
        available, headroom = memory_state(self.groups)
        if available < self.reserve or headroom < self.group_reserve:
            raise RuntimeError("Memory pressure detected; benchmark stopped and results discarded")
        for group, values in self.read_events().items():
            if any(values.get(k, 0) > self.events[group].get(k, 0)
                   for k in ("high", "max", "oom", "oom_kill")):
                raise RuntimeError("cgroup memory pressure detected; results discarded")
        # System-wide swap-out is recorded, not fatal: on zram desktops the
        # kernel routinely moves other processes' cold pages while the arrays
        # are allocated. The benchmark's own VmSwap and major faults below are
        # what actually contaminate a measurement.
        if any(value > self.throttled[group] for group, value in self.read_throttled().items()):
            raise RuntimeError("cgroup CPU throttling detected; results discarded")
        if pid:
            try:
                if fields(f"/proc/{pid}/status").get("VmSwap", 0):
                    raise RuntimeError("Benchmark pages were swapped; results discarded")
            except FileNotFoundError:
                pass


def build_native(cache):
    compiler = shutil.which("cc")
    if compiler is None:
        raise RuntimeError("A C compiler is required; install your distribution's C build tools")
    source = Path(__file__).resolve().parent / "native"
    flags = ["-O3", "-std=c11", "-pthread", "-fno-lto", "-ffp-contract=off",
             "-Wall", "-Wextra", "-Werror"]
    if platform.machine() in ("x86_64", "i686"):
        flags.append("-march=native")
    elif platform.machine() == "aarch64":
        flags.append("-mcpu=native")
    version = subprocess.check_output([compiler, "--version"], text=True)
    # CPU capability changes invalidate the build; fluctuating frequency does not.
    capabilities = "\n".join(line for line in Path("/proc/cpuinfo").read_text().splitlines()
                             if not line.startswith(("cpu MHz", "bogomips")))
    digest = hashlib.sha256((version + repr(flags) + capabilities).encode())
    sources = [source / "benchmark.c", source / "kernels.c"]
    for path in sources:
        digest.update(path.read_bytes())
    binary = cache / ("native-" + digest.hexdigest()[:20])
    if not binary.exists():
        with tempfile.TemporaryDirectory(dir=cache) as tmp:
            output = Path(tmp) / "benchmark"
            compiled = subprocess.run([compiler, *flags, *map(str, sources), "-o", str(output)],
                                      capture_output=True, text=True, timeout=120)
            if compiled.returncode:
                raise RuntimeError("Native compilation failed:\n" + compiled.stderr)
            os.replace(output, binary)
    return binary, {"compiler": version.splitlines()[0], "flags": flags,
                    "source_build_hash": digest.hexdigest()}


def statistics(samples):
    elapsed = sum(s["seconds"] for s in samples)
    mean = sum(s["bytes"] for s in samples) / elapsed / 1e6
    rates = sorted((s["bytes"] / s["seconds"] / 1e6, s["seconds"]) for s in samples)
    def quantile(fraction):
        accumulated = 0
        for rate, seconds in rates:
            accumulated += seconds
            if accumulated >= elapsed * fraction:
                return rate
        return rates[-1][0]
    variance = sum(seconds * (rate - mean) ** 2 for rate, seconds in rates) / elapsed
    return {"mean_MBps": mean, "p10_MBps": quantile(.1), "median_MBps": quantile(.5),
            "p90_MBps": quantile(.9), "min_MBps": rates[0][0], "max_MBps": rates[-1][0],
            "coefficient_of_variation": math.sqrt(variance) / mean}


def run_native(binary, mode, size, cpus, seconds, warmup, guard, on_sample):
    command = [str(binary), mode, str(size), ",".join(map(str, cpus)), str(seconds), str(warmup)]
    samples, result = [], None
    with tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        pending = b""
        deadline = time.monotonic() + seconds + warmup + 90
        try:
            while selector.get_map():
                guard.check(child.pid)
                if time.monotonic() > deadline:
                    raise RuntimeError("Native benchmark timed out")
                for key, _ in selector.select(.25):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    pending += data
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        record = json.loads(line)
                        if record["type"] == "sample":
                            samples.append(record)
                            on_sample(record)
                        elif record["type"] == "result":
                            result = record
            code = child.wait(timeout=5)
            guard.check()
            if code or result is None:
                errors.seek(0)
                raise RuntimeError(errors.read().decode().strip() or "Native benchmark failed")
            if result["major_faults"]:
                raise RuntimeError("Major page faults occurred during measurement; results discarded")
            if not result["validated"] or not samples:
                raise RuntimeError("Missing validation or samples")
            if sum(s["bytes"] for s in samples) != result["bytes"]:
                raise RuntimeError("Native byte accounting mismatch")
            result.update(statistics(samples))
            result.update(kernel=mode, cpus=cpus, threads=len(cpus), warmup_seconds=warmup,
                          samples=samples)
            return result
        finally:
            selector.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            child.stdout.close()


def scaling_counts(count):
    result, n = [], 1
    while n < count:
        result.append(n)
        n *= 2
    return result + [count]


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            json.dump(report, f, indent=2, allow_nan=False)
            f.write("\n")
            f.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60,
                        help="total measured seconds, excluding warmup/setup (12–3600; default 60)")
    parser.add_argument("--cpus", help="CPU list, e.g. 0-7; default one logical CPU per allowed core")
    parser.add_argument("--memory-mib", type=int, help="combined array size; safety checks still apply")
    parser.add_argument("--json", action="store_true", help="emit JSON Lines instead of panel protocol")
    parser.add_argument("--report", type=Path, help="JSON report path; default XDG state/ram-speedtest/last-run.json")
    parser.add_argument("--dry-run", action="store_true", help="show plan without allocating or running")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or not 12 <= args.duration <= 3600:
        parser.error("--duration must be between 12 and 3600 seconds")
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ram-speedtest"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ram-speedtest"
    report_path = args.report or state / "last-run.json"
    report = {"schema_version": 2, "valid": False,
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "metric": "useful application bytes (reads + writes), decimal MB/s",
              "physical_dram_traffic_measured": False, "stages": []}
    def emit(kind, **values):
        if args.json:
            print(json.dumps({"type": kind, **values}), flush=True)
        elif kind in ("copy", "triad"):
            print(f"{kind} {values['MBps']:.0f}", flush=True)
        elif kind == "status":
            print("status " + values["message"], flush=True)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    lock = None
    owns_lock = False
    try:
        if not args.dry_run:
            cache.mkdir(parents=True, exist_ok=True)
            lock = (cache / "run.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another RAM benchmark is already running for this user") from None
            owns_lock = True
        allowed = os.sched_getaffinity(0)
        cpus = cpu_list(args.cpus) if args.cpus else physical_cpus(allowed)
        if not cpus or not set(cpus) <= allowed or max(cpus) >= 1024:
            raise RuntimeError("Selected CPUs must be in this process's allowed affinity")
        groups = cgroup_dirs()
        capacity = cpu_capacity(groups)
        if capacity < 1:
            raise RuntimeError("CPU quota is below one core; cannot measure sustained bandwidth reliably")
        if len(cpus) > capacity:
            if args.cpus:
                raise RuntimeError("Selected CPU count exceeds the cgroup CPU quota")
            cpus = cpus[:int(capacity)]
        counts = scaling_counts(len(cpus))
        caches = cache_domains(cpus)
        llc = sum(caches.values())
        cache_budget = max(cache_requirement(cpus[:n], caches) for n in counts)
        available, headroom = memory_state(groups)
        size = memory_plan(available, headroom, cache_budget, len(cpus),
                           None if args.memory_mib is None else args.memory_mib * MIB)
        report.update(cpus=cpus, last_level_cache_bytes=llc, planned_array_bytes=size,
                      cache_budget_bytes=cache_budget,
                      cpu_quota_cores=None if math.isinf(capacity) else capacity,
                      measured_duration_seconds=args.duration, kernel_release=platform.release(),
                      numa_policy="worker-pinned first touch under inherited OS memory policy",
                      cgroup_paths=list(map(str, groups)))
        report["cpu_topology"] = [
            {"cpu": cpu,
             "package": read_int(SYS_CPU / f"cpu{cpu}/topology/physical_package_id"),
             "core": read_int(SYS_CPU / f"cpu{cpu}/topology/core_id")}
            for cpu in cpus]
        cpuinfo = Path("/proc/cpuinfo").read_text()
        report["cpu_model"] = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines()
                                    if line.startswith("model name")), platform.machine())
        if args.dry_run:
            print(json.dumps(report, indent=2))
            return
        emit("status", message="Preparing native benchmark")
        binary, build = build_native(cache)
        report["build"] = build
        ram = describe_ram()
        report["ram"] = ram
        if not args.json:
            print("ram " + ram, flush=True)
        guard = PressureGuard(groups)
        memory_plan(*memory_state(groups), cache_budget, len(cpus), size)
        stages = [("scaling", "copy", cpus[:n], args.duration * .2 / len(counts), .5)
                  for n in counts]
        stages += [("sustained", mode, cpus, args.duration * .4, 2.0)
                   for mode in ("copy", "triad")]
        for stage, mode, selected, seconds, warmup in stages:
            emit("status", message=f"{stage.capitalize()} {mode} · {len(selected)} threads · {size / MIB:.0f} MiB")
            def on_sample(sample):
                rate = sample["bytes"] / sample["seconds"] / 1e6
                if args.json:
                    emit("sample", stage=stage, kernel=mode, threads=len(selected),
                         seconds=sample["seconds"], bytes=sample["bytes"], MBps=rate)
                elif stage == "sustained":
                    emit(mode, MBps=rate)
            result = run_native(binary, mode, size, selected, seconds, warmup, guard, on_sample)
            result["stage"] = stage
            report["stages"].append(result)
            if args.json:
                emit("stage_result", result=result)
            elif stage == "sustained":
                emit(mode, MBps=result["mean_MBps"])
        scaling = report["stages"][:len(counts)]
        report["scaling_speedup"] = scaling[-1]["mean_MBps"] / scaling[0]["mean_MBps"]
        best_scaling = max(scaling, key=lambda result: result["mean_MBps"])
        report["best_short_scaling_threads"] = best_scaling["threads"]
        report["full_core_vs_best_short_scaling_ratio"] = scaling[-1]["mean_MBps"] / best_scaling["mean_MBps"]
        report["valid"] = True
        report["system_swapout_pages"] = fields("/proc/vmstat").get("pswpout", 0) - guard.swapout
        report["limitations"] = [
            "Synthetic sequential kernels, not a prediction for every application",
            "No per-channel counters, channel-count inference, or guaranteed saturation",
            "NUMA locality follows inherited memory policy; physical placement is not verified",
            "Results include synchronization/scheduling; CPU, caches and copy implementation matter",
            "Scaling stages are short diagnostics; sustained stages are the primary results",
            "Pressure monitoring reduces risk but cannot guarantee no OOM under external pressure"]
        write_report(report_path, report)
        copy, triad = report["stages"][-2:]
        emit("status", message=f"{len(cpus)} threads · p10–p90 MB/s: COPY "
             f"{copy['p10_MBps']:.0f}–{copy['p90_MBps']:.0f} · TRIAD "
             f"{triad['p10_MBps']:.0f}–{triad['p90_MBps']:.0f}")
        if args.json:
            emit("complete", report=str(report_path), valid=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        report["valid"] = False
        report["error"] = str(error) or "Benchmark cancelled"
        if owns_lock:
            try:
                write_report(report_path, report)
            except OSError as write_error:
                print(f"Could not save report: {write_error}", file=sys.stderr)
        print(report["error"], file=sys.stderr)
        raise SystemExit(130 if isinstance(error, KeyboardInterrupt) else 1)
    finally:
        if lock:
            lock.close()
