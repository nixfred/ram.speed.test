"""SMBIOS display metadata; never used to calculate throughput."""
import os
import subprocess


def describe_ram():
    """Name the installed memory from the SMBIOS table udev already exposes,
    so no root is needed. Include makers, module models, configured transfer
    rates and total installed capacity, preserving mixed-module details."""
    try:
        out = subprocess.run(["udevadm", "info", "-e"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    fields = {}
    for line in out.splitlines():
        if not line.startswith("E: MEMORY_DEVICE_"):
            continue
        key, _, value = line[3:].partition("=")
        fields[key] = value
    sticks = []
    for i in range(64):
        size = fields.get(f"MEMORY_DEVICE_{i}_SIZE")
        if size is None:
            continue
        if not size.isdigit() or int(size) == 0:
            continue
        sticks.append({
            "size": int(size),
            "type": fields.get(f"MEMORY_DEVICE_{i}_TYPE", ""),
            "speed": fields.get(f"MEMORY_DEVICE_{i}_CONFIGURED_SPEED_MTS")
                     or fields.get(f"MEMORY_DEVICE_{i}_SPEED_MTS", ""),
            "maker": fields.get(f"MEMORY_DEVICE_{i}_MANUFACTURER", ""),
            "model": fields.get(f"MEMORY_DEVICE_{i}_PART_NUMBER", ""),
        })
    if not sticks:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return f"{round(total / 2**30)} GB RAM"
    def unique(field):
        return list(dict.fromkeys(s[field].strip() for s in sticks
                                 if s[field].strip().lower() not in
                                 ("", "unknown", "not specified")))

    maker = "/".join(unique("maker"))
    models = ", ".join(unique("model"))
    kind = "/".join(unique("type"))
    speeds = "/".join(unique("speed"))
    memory_type = " ".join(p for p in (kind, f"{speeds} MT/s" if speeds else "") if p)
    size = f"{round(sum(s['size'] for s in sticks) / 2**30)} GB total"
    return " · ".join(p for p in (maker, models, memory_type, size) if p)
