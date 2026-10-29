"""Portable best-effort network interface byte counters."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def read_interface_bytes() -> tuple[int, int] | None:
    """Return aggregate RX/TX bytes for visible interfaces, excluding loopback."""
    if os.name == "nt":
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if not executable:
            return None
        command = (
            "$s=Get-NetAdapterStatistics -ErrorAction Stop; "
            "[pscustomobject]@{rx=($s|Measure-Object -Property ReceivedBytes -Sum).Sum; "
            "tx=($s|Measure-Object -Property SentBytes -Sum).Sum}|ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, timeout=3, check=True)
            values = json.loads(result.stdout)
            return int(values["rx"]), int(values["tx"])
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
    path = Path("/proc/net/dev")
    if not path.is_file():
        return None
    try:
        rx_total = tx_total = 0
        for line in path.read_text(encoding="utf-8").splitlines()[2:]:
            interface, counters = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            values = counters.split()
            rx_total += int(values[0])
            tx_total += int(values[8])
        return rx_total, tx_total
    except (OSError, ValueError, IndexError):
        return None
