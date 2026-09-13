# RAM Speed Test

Sustained **multi-core application memory throughput** for Omarchy, using native
copy and numerical array kernels. The panel shows COPY and TRIAD in decimal MB/s;
a JSON report retains the thread-scaling sweep, individual time samples and
variability. This is a synthetic large-array workload, not a prediction for every
application or a direct measurement of physical DRAM-bus traffic.

## What runs

The default run performs 60 seconds of measured work, plus about 7 seconds of
warmup and additional allocation, validation and first-build time:

1. **Thread scaling:** 20% of the measured time is divided between 1, 2, 4, … and
   the full selected core count, running copy. Each stage has a separate 0.5-second
   warmup. These short stages diagnose scaling; they are not peak-bandwidth claims.
2. **Sustained copy:** 40% of the measured time (24 seconds by default), after a
   separate two-second warmup.
3. **Sustained triad:** 40% of the measured time (24 seconds by default), after a
   separate two-second warmup.

Each stage uses persistent native pthread workers, pinned to the selected logical
CPUs. By default there is one worker per allowed physical core, distributed across
CPU sockets. CPU affinity restrictions and visible cgroup CPU quotas are respected.
Each worker allocates and initializes its own three double-precision arrays after
pinning. This encourages local NUMA allocation under the inherited OS memory
policy; the tool does not override that policy or verify physical page placement.

The complete working set is scanned every pass. Each array is sized to at least
four times the relevant last-level cache capacity, accounting for uneven worker
counts across cache domains and every stage of the scaling sweep. Cache-sized
measurements are refused. Default total array allocation is normally 768 MiB,
can grow to meet cache requirements, and is capped at 2 GiB. Explicit larger sizes
must still pass admission checks.

## Precisely what MB/s means

| Kernel | Operation on each element | Counted application traffic |
| --- | --- | --- |
| COPY | `a[i] = b[i]`, implemented with native `memcpy` | 8-byte read + 8-byte write = **16 bytes** |
| TRIAD | `a[i] = b[i] + 3.0 * c[i]` | Two 8-byte reads + one 8-byte write = **24 bytes** |

```text
MB/s = completed passes × elements across all workers × bytes per element
       / elapsed wall-clock seconds / 1,000,000
```

Workers synchronize at pass boundaries. A single `CLOCK_MONOTONIC` interval covers
the work of **all** workers; independently timed per-thread rates are not added.
Every counted pass has completed before the timer is read. Synchronization,
scheduling and intervening sample-output costs are included. Initial allocation,
page touching, warmup and final full-array output validation are excluded. A stage
ends at a completed pass, so it can slightly exceed its requested duration.

The final dial is total counted bytes divided by total measured time, rather than
a peak or an unweighted average. Approximately one-second intervals (plus the
shorter final interval) are retained. P10, median, P90 and coefficient of variation
are **weighted by interval duration**. Percentiles describe the distribution of
interval throughput, not memory-access latency or a confidence interval.

COPY's count includes both reads and writes. To obtain copied payload MB/s, divide
its reported bandwidth by two. These results are not numerically comparable with
version 1's READ/WRITE dials, which counted a copied block once and used asymmetric
Python copy operations. No Python code runs inside the native copy/triad loops.
The kernels are compiled separately without LTO so repeated passes cannot be
removed; all output elements are checked after each stage.

The kernels use the same simple operations commonly used to study sustained
memory bandwidth. This implementation is **not the official STREAM benchmark**
and its results must not be presented as standard STREAM results. See the
[STREAM measurement and cache-size guidance](https://www.cs.virginia.edu/stream/ref.html).

## Memory and process safeguards

- Combined arrays **plus an allowance of 128 MiB + 8 MiB per worker** must fit
  within one quarter of the smaller of host `MemAvailable` and visible cgroup
  memory headroom. Both `memory.high` and `memory.max` are checked at every visible
  ancestor. Insufficient safe space means refusal, not a smaller cache benchmark.
- The supervisor checks memory headroom, cgroup memory events, the benchmark's
  own swapped pages and cgroup CPU throttling about four times per second.
  Detected pressure or throttling stops the run and marks the report invalid.
  System-wide swap-out by other processes (routine on zram desktops) does not
  stop the run; it is recorded as `system_swapout_pages` in the report.
- Major page faults during the measured interval invalidate the result. Minor
  faults are reported; these can include automatic NUMA balancing or transparent
  huge-page activity and are not automatically treated as swapping.
- A per-user lock prevents overlapping runs with the same cache directory.
  A rejected overlapping run leaves the saved report intact.
- Closing the panel or interrupting the supervisor terminates the native process
  and its threads. Linux's parent-death signal also stops it if the supervisor is
  killed. Timeouts bound stalled native stages.
- No root access, memory locking, governor changes, swap changes or other system
  tuning is performed. All selected cores are exercised, so the test consumes CPU
  and memory bandwidth and can temporarily affect desktop responsiveness.

These checks reduce risk; they cannot guarantee immunity from an OOM race, hidden
container-ancestor limits, or arbitrary competing allocations. Read-only cgroup v2,
CPU/cache sysfs and procfs information is required; unsupported configurations fail
rather than silently guessing a safe benchmark size.

## Multi-channel interpretation and limits

Using many cores allows the test to exercise aggregate bandwidth across the memory
channels used by the OS's physical address placement. The scaling sweep shows how
much additional concurrency helps. A plateau is evidence about this workload, not
proof that every channel is saturated. More threads can also reduce throughput.

The tool does **not** infer channel count from DIMM count, verify channel population,
measure traffic separately on each channel, or promise the hardware's theoretical
maximum. On a multi-socket system the default result aggregates the selected
sockets, including their controllers. Use `--cpus` to restrict a comparison to a
socket or a specific set of cores; note the inherited memory policy can still
influence placement.

Cache write allocation, dirty-line eviction, prefetching and a libc implementation's
possible non-temporal stores affect physical traffic. No end-of-test cache flush
is included. Therefore application byte counts cannot be converted to DRAM-bus
traffic with a universal multiplier. Direct channel measurements require
platform-specific memory-controller performance counters, which this tool does
not collect.

CPU type/frequency, SIMD instruction selection, libc, NUMA policy, page size,
background activity and thermal/power limits remain part of the observed result.
The report records build flags and selected CPUs for reproducibility. Longer runs
expose changes over time but cannot guarantee thermal equilibrium. Compare the
same workload, duration, array size, CPU selection and software stack. This is not
a random-access latency test, application trace replay, or comprehensive RAM
integrity/stability test.

## Run locally

Requirements: Linux with cgroup v2, Python 3.9+, and a C11 compiler (`cc`) with
pthread support. The panel additionally needs Omarchy's shared `SpeedTestOverlay`.
The native executable builds on first use into `$XDG_CACHE_HOME/ram-speedtest`
(default `~/.cache/ram-speedtest`). Source, compiler, flags and CPU capabilities
form its cache key. `udevadm` is optional and only supplies display metadata.

```bash
# Inspect CPU selection, cache requirement and memory admission without running.
python3 ram-speedtest --dry-run

# About 67+ seconds, including warmup/setup; live panel-compatible text output.
python3 ram-speedtest

# Three measured minutes for longer-term behaviour, with machine-readable output.
python3 ram-speedtest --duration 180 --json --report ./result.json

# Quick diagnostic; fewer samples means less information about variability.
python3 ram-speedtest --duration 12

# Explicit CPU set and COMBINED array allocation (safety checks still apply).
python3 ram-speedtest --cpus 0-7 --memory-mib 1024 --json
```

The latest report is saved atomically to
`$XDG_STATE_HOME/ram-speedtest/last-run.json` (default
`~/.local/state/ram-speedtest/last-run.json`). Use `--report` to retain separate runs.
Always check the top-level `valid` field; partial stages in a failed/cancelled
report are diagnostic only. A valid report includes every time interval, stage
means and percentiles, scaling speedup, allocation size, CPU selection, faults,
compiler identity and measurement limitations. The panel clears both dials on
failure and shows P10–P90 ranges after a successful run.

## Omarchy panel

The plugin entry point remains `nixfred.ram-speedtest`. After installing this
version through Omarchy's plugin workflow, summon it with:

```bash
omarchy-shell shell summon nixfred.ram-speedtest
```

`Panel.qml` uses the shared speed-test overlay and consumes `ram`, `status`, `copy`
and `triad` protocol lines. It does not calculate bandwidth or show the scaling
sweep as a final dial result. Earlier README screenshots showing READ/WRITE are
historical and do not represent this version.

After updating an already-loaded version 1 plugin, if the dials still say
READ/WRITE and remain at zero, run `omarchy restart shell` before reopening it.
The shell can retain the old QML panel after a plugin rescan; the updated panel
must show COPY/TRIAD to consume this benchmark's live readings.

## Development checks

```bash
python3 -m unittest discover -s tests -v
python3 ram-speedtest --duration 12 --json --report /tmp/ram-speedtest-check.json
```

Tests exercise conservative allocation, cache-domain sizing, memory/CPU limits,
weighted accounting, real native kernel validation, malformed arguments and worker
cleanup when the supervisor exits or detects pressure. Small test arrays verify
correctness only; they are deliberately not performance measurements.

## License

[MIT](LICENSE) © Fred Nix
