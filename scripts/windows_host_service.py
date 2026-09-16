#!/usr/bin/env python3
"""
Anro LLM System Information Service (Windows host, port 8088).

Canonical source: this file in the llm-management repo (scripts/windows_host_service.py).
Provides GPU/NPU/CPU/memory metrics to Docker/WSL and native Windows via HTTP.

Usage:
    python scripts/windows_host_service.py [--port 8088] [--host 127.0.0.1]
"""

import sys
import json
import subprocess
import re
import os
import time
import copy
import ctypes
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import argparse
from functools import lru_cache
import asyncio
from threading import Lock, Thread

try:
    from fastapi import FastAPI, HTTPException, BackgroundTasks
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError:
    print("ERROR: Required packages not installed. Please install:")
    print("  pip install fastapi uvicorn")
    sys.exit(1)

# Host-service version. Not required to match the llm-management image.
__version__ = "1.0.6"

app = FastAPI(
    title="Windows Host System Info Service",
    description="Provides Windows system information (GPU, NPU, CPU, Memory) via HTTP API",
    version=__version__,
)

# Identity cache only (CIM). Usage is the in-process sampler, not a cache in
# front of powershell.exe. TTL >= 30s so a Dashboard tick never re-runs CIM.
_cache = {}
_cache_lock = Lock()
_cache_ttl = {
    'gpu_info': 30.0,
    'npu_info': 30.0,
    'memory_info': 30.0,
    'storage_info': 30.0,
    'chassis_info': 30.0,
    'power_info': 30.0,
}
_refresh_intervals = {}
_refresh_tasks = {}
_refresh_last_time = {}

_METRICS_CLIENT_WINDOW_SEC = 30.0
_SAMPLER_HOT_SEC = 1.0
_SAMPLER_IDLE_SEC = 10.0
_last_metrics_client = 0.0
_snapshot_lock = Lock()
_snapshot: Dict[str, Any] = {
    "cpu_usage": {
        "utilization": 0.0,
        "frequency": 0,
        "physical_cores": 0,
        "logical_cores": 0,
    },
    "memory_usage": {"total": 0, "used": 0, "available": 0},
    "gpu_usage": {"gpus": []},
    "npu_usage": {"npu_utilization": 0.0, "npu_model": None},
    "storage_live": {"storage_free": 0.0, "storage_size_live": 0.0},
}
_cpu_cores: Dict[str, int] = {"physical": 0, "logical": 0}
# Previous GetSystemTimes sample plus wall clock. Owned by the sampler only.
# A short window (PDH open, a few hundred ms) reads as a spike vs Task Manager.
_cpu_times_prev: Optional[Tuple[int, int, int, float]] = None
_CPU_MIN_WINDOW_SEC = 0.8
_pdh = None  # type: ignore
_LUID_RE = re.compile(r"luid_0x([0-9a-f]+)_0x([0-9a-f]+)", re.I)
_PHYS_RE = re.compile(r"phys_(\d+)", re.I)


def _touch_metrics_client() -> None:
    """Metrics routes only. /health and /api/version must not keep the sampler hot."""
    global _last_metrics_client
    _last_metrics_client = time.time()


def _metrics_client_active() -> bool:
    return (time.time() - _last_metrics_client) <= _METRICS_CLIENT_WINDOW_SEC


def _copy_snapshot() -> Dict[str, Any]:
    with _snapshot_lock:
        return copy.deepcopy(_snapshot)


def _cached_identity(key: str, default: Any) -> Any:
    """Last identity only. Never computes (that would spawn powershell.exe)."""
    with _cache_lock:
        item = _cache.get(key)
        value = item[0] if item else default
    return copy.deepcopy(value)


def _get_cached(key: str, func, *args, **kwargs):
    """Get value from cache or compute and cache it."""
    now = time.time()
    
    with _cache_lock:
        if key in _cache:
            value, timestamp = _cache[key]
            ttl = _cache_ttl.get(key, 1.0)
            if now - timestamp < ttl:
                return value
    
    # Compute new value (cache miss or expired)
    value = func(*args, **kwargs)
    with _cache_lock:
        _cache[key] = (value, now)
        _refresh_last_time[key] = now
    return value


def _infer_vendor(name: str) -> str:
    low = (name or "").lower()
    if any(x in low for x in ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla")):
        return "NVIDIA"
    if any(x in low for x in ("amd", "radeon", " rdna", "vega")) or "rx " in low or low.startswith("rx"):
        return "AMD"
    if any(x in low for x in ("intel", "arc", "iris", "uhd graphics")):
        return "Intel"
    return "Unknown"


def _instance_name(counter_path: str) -> str:
    match = re.search(r"\((.*)\)", counter_path)
    return match.group(1) if match else counter_path


def _luid_key(instance: str) -> Optional[str]:
    match = _LUID_RE.search(instance or "")
    if not match:
        return None
    return f"{match.group(1)}_{match.group(2)}".lower()


def _phys_index(instance: str) -> Optional[int]:
    match = _PHYS_RE.search(instance or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class _PdhUsageQuery:
    """One PDH query, opened once. Each tick calls CollectQueryData once.

    Do not take two samples with a wait inside one collect. That is the
    Get-Counter -MaxSamples 2 hang on GPU Engine(*).
    """

    def __init__(self) -> None:
        self.query = None
        self.counters: List[Tuple[str, str, Any]] = []
        self._expanded = 0
        self._last_expand = 0.0

    def close(self) -> None:
        if self.query is not None:
            try:
                import win32pdh
                win32pdh.CloseQuery(self.query)
            except Exception:
                pass
        self.query = None
        self.counters = []

    def open(self) -> None:
        import win32pdh

        self.close()
        query = win32pdh.OpenQuery()
        add = getattr(win32pdh, "AddEnglishCounter", win32pdh.AddCounter)
        counters: List[Tuple[str, str, Any]] = []

        def _add_wild(kind: str, pattern: str) -> int:
            try:
                paths = win32pdh.ExpandCounterPath(pattern) or []
            except Exception:
                return 0
            added = 0
            for path in paths:
                try:
                    counters.append((kind, path, add(query, path)))
                    added += 1
                except Exception:
                    continue
            return added

        gpu_n = _add_wild("util", r"\GPU Engine(*)\Utilization Percentage")
        ded_n = _add_wild("ded", r"\GPU Adapter Memory(*)\Dedicated Usage")
        shr_n = _add_wild("shr", r"\GPU Adapter Memory(*)\Shared Usage")
        if ded_n == 0 and shr_n == 0:
            # Process counters are per PID; sum them. Adapter counters are one
            # value per GPU and stay a max (kind ded/shr).
            _add_wild("ded_sum", r"\GPU Process Memory(*)\Dedicated Usage")
            _add_wild("shr_sum", r"\GPU Process Memory(*)\Shared Usage")
        npu_n = 0
        for pattern in (
            r"\NPU Engine(*)\Utilization Percentage",
            r"\AMD XDNA Engine(*)\Utilization Percentage",
            r"\Compute Accelerator(*)\Utilization Percentage",
            r"\NPU(*)\Utilization Percentage",
        ):
            npu_n += _add_wild("npu", pattern)
        self.query = query
        self.counters = counters
        self._expanded = gpu_n + ded_n + shr_n + npu_n
        self._last_expand = time.time()
        try:
            win32pdh.CollectQueryData(query)
        except Exception as exc:
            print(f"WARNING: PDH prime collect failed: {exc}", file=sys.stderr)

    def maybe_reopen(self) -> None:
        """Rebuild only if the wildcard set changed, and not more than every 30s."""
        if self.query is None or (time.time() - self._last_expand) < 30.0:
            return
        try:
            import win32pdh
            eng = win32pdh.ExpandCounterPath(r"\GPU Engine(*)\Utilization Percentage") or []
        except Exception:
            return
        if len(eng) != sum(1 for kind, _p, _h in self.counters if kind == "util"):
            try:
                self.open()
            except Exception as exc:
                print(f"WARNING: PDH reopen failed: {exc}", file=sys.stderr)

    def collect(self) -> List[Tuple[str, str, float]]:
        import win32pdh

        if self.query is None:
            self.open()
        win32pdh.CollectQueryData(self.query)
        rows: List[Tuple[str, str, float]] = []
        for kind, path, handle in self.counters:
            try:
                _typ, value = win32pdh.GetFormattedCounterValue(handle, win32pdh.PDH_FMT_DOUBLE)
            except Exception:
                continue
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            rows.append((kind, _instance_name(path), number))
        return rows


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


def _filetime_int(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def _read_system_times() -> Tuple[int, int, int]:
    """(idle, kernel, user). Kernel already includes idle. Same source as Task Manager overall %."""
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise OSError("GetSystemTimes failed")
    return (_filetime_int(idle), _filetime_int(kernel), _filetime_int(user))


def _percent_from_system_times(
    prev: Tuple[int, int, int], curr: Tuple[int, int, int], wall_sec: float = 1.0
) -> Optional[float]:
    """Busy time / elapsed, matching \\Processor(_Total)\\% Processor Time.

    Not % Processor Utility (that tracks boost and runs hot vs Task Manager).
    wall_sec must be a real tick (~1s). A setup slice is not a reading.
    """
    if wall_sec < _CPU_MIN_WINDOW_SEC:
        return None
    idle = curr[0] - prev[0]
    kernel = curr[1] - prev[1]
    user = curr[2] - prev[2]
    if idle < 0 or kernel < 0 or user < 0:
        return None
    busy = (kernel - idle) + user
    total = kernel + user
    if total <= 0 or busy < 0:
        return None
    return 100.0 * busy / total


def _clamp_percent(value: float) -> float:
    number = float(value or 0.0)
    if number < 0:
        return 0.0
    if number > 100:
        return 100.0
    return round(number, 1)


def _sample_cpu_ram() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    import psutil

    global _cpu_times_prev
    util: Optional[float] = None
    try:
        now_times = _read_system_times()
        now_wall = time.time()
        if _cpu_times_prev is not None:
            util = _percent_from_system_times(
                _cpu_times_prev[:3], now_times, now_wall - _cpu_times_prev[3]
            )
        if util is not None:
            _cpu_times_prev = (now_times[0], now_times[1], now_times[2], now_wall)
    except OSError as exc:
        print(f"WARNING: GetSystemTimes failed: {exc}", file=sys.stderr)
    if util is None:
        previous = _copy_snapshot()["cpu_usage"].get("utilization")
        util = float(previous) if isinstance(previous, (int, float)) else 0.0
    util = _clamp_percent(util)
    freq = _cpu_cores.get("frequency", 0)
    try:
        info = psutil.cpu_freq()
        if info and info.current:
            freq = int(info.current)
            _cpu_cores["frequency"] = freq
    except Exception:
        pass
    if not _cpu_cores.get("logical"):
        _cpu_cores["physical"] = int(psutil.cpu_count(logical=False) or 0)
        _cpu_cores["logical"] = int(psutil.cpu_count(logical=True) or 0)
    cpu = {
        "utilization": util,
        "frequency": int(freq or 0),
        "physical_cores": int(_cpu_cores.get("physical") or 0),
        "logical_cores": int(_cpu_cores.get("logical") or 0),
    }
    vm = psutil.virtual_memory()
    mem = {
        "total": int(vm.total),
        "used": int(vm.used),
        "available": int(vm.available),
    }
    return cpu, mem


def _sample_disk() -> Dict[str, float]:
    import psutil

    usage = psutil.disk_usage("C:\\")
    return {
        "storage_free": round(usage.free / (1024**3), 2),
        "storage_size_live": round(usage.total / (1024**3), 2),
    }


def _join_gpu_usage(identity_gpus: List[Dict[str, Any]], samples: List[Tuple[str, str, float]]) -> Dict[str, Any]:
    """Match PDH samples to cached identity. No CIM. Utilization is always a number."""
    util_map: Dict[str, float] = {}
    ded_map: Dict[str, float] = {}
    shr_map: Dict[str, float] = {}

    def _bump(store: Dict[str, float], key: Optional[str], value: float, sum_values: bool = False) -> None:
        if not key:
            return
        if sum_values:
            store[key] = store.get(key, 0.0) + value
        elif value > store.get(key, 0.0):
            store[key] = value

    for kind, instance, value in samples:
        if kind == "npu":
            continue
        luid = _luid_key(instance)
        phys = _phys_index(instance)
        idx_key = f"idx_{phys}" if phys is not None else None
        if kind == "util":
            _bump(util_map, luid, value)
            _bump(util_map, idx_key, value)
            _bump(util_map, "global", value)
        elif kind in ("ded", "ded_sum"):
            _bump(ded_map, luid or idx_key, value, sum_values=kind == "ded_sum")
        elif kind in ("shr", "shr_sum"):
            _bump(shr_map, luid or idx_key, value, sum_values=kind == "shr_sum")

    def _mem_mb(key: str) -> Optional[float]:
        if key not in ded_map and key not in shr_map:
            return None
        return round((ded_map.get(key, 0.0) + shr_map.get(key, 0.0)) / (1024**2), 2)

    catalog = []
    for gpu in identity_gpus or []:
        name = gpu.get("gpu_model") or "Unknown GPU"
        vram_gb = float(gpu.get("gpu_dedicated_vram") or 0)
        catalog.append(
            {
                "name": name,
                "memory_total_mb": round(vram_gb * 1024, 2),
                "vendor": _infer_vendor(name),
            }
        )

    luid_keys = sorted(
        {k for k in list(util_map) + list(ded_map) + list(shr_map) if k not in ("global",) and not str(k).startswith("idx_")}
    )
    assignments: Dict[str, Dict[str, Any]] = {}
    if catalog and luid_keys:
        used = set()
        by_vram = sorted(catalog, key=lambda g: g["memory_total_mb"], reverse=True)
        by_mem = sorted(luid_keys, key=lambda k: _mem_mb(k) or 0.0, reverse=True)
        for gpu in by_vram:
            cap = gpu["memory_total_mb"] * 1.1 if gpu["memory_total_mb"] else None
            picked = None
            for key in by_mem:
                if key in used:
                    continue
                mem_mb = _mem_mb(key) or 0.0
                if cap is None or mem_mb <= cap:
                    picked = key
                    break
            if picked is None:
                for key in luid_keys:
                    if key not in used:
                        picked = key
                        break
            if picked:
                used.add(picked)
                assignments[gpu["name"]] = {
                    "utilization": _clamp_percent(util_map.get(picked, 0.0)),
                    "memory_used_mb": _mem_mb(picked) or 0.0,
                    "adapter_index": picked,
                }
    elif len(catalog) == 1 and util_map:
        key = "global" if "global" in util_map else max(util_map, key=util_map.get)
        assignments[catalog[0]["name"]] = {
            "utilization": _clamp_percent(util_map.get(key, 0.0)),
            "memory_used_mb": _mem_mb(key) or 0.0,
            "adapter_index": None if key == "global" else key,
        }

    rows = []
    if catalog:
        for gpu in catalog:
            assigned = assignments.get(gpu["name"], {})
            used_mb = float(assigned.get("memory_used_mb") or 0.0)
            total_mb = float(gpu["memory_total_mb"] or 0.0)
            if total_mb > 0 and used_mb > total_mb * 1.05:
                used_mb = 0.0
            row = {
                "name": gpu["name"],
                "utilization": float(assigned.get("utilization") or 0.0),
                "memory_used_mb": used_mb,
                "memory_total_mb": total_mb,
                "vendor": gpu["vendor"],
            }
            if assigned.get("adapter_index") is not None:
                row["adapter_index"] = assigned["adapter_index"]
            rows.append(row)
    elif luid_keys:
        for key in luid_keys:
            rows.append(
                {
                    "name": f"GPU {key}",
                    "utilization": _clamp_percent(util_map.get(key, 0.0)),
                    "memory_used_mb": _mem_mb(key) or 0.0,
                    "memory_total_mb": 0.0,
                    "vendor": "Unknown",
                    "adapter_index": key,
                }
            )
    return {"gpus": rows}


def _npu_from_samples(samples: List[Tuple[str, str, float]], model: Optional[str]) -> Dict[str, Any]:
    util = 0.0
    for kind, _instance, value in samples:
        if kind == "npu" and value > util:
            util = value
    return {"npu_utilization": _clamp_percent(util), "npu_model": model}


def _publish_cpu_ram() -> None:
    """~1s busy/elapsed, same window as Task Manager overall. Not the GPU period."""
    cpu, mem = _sample_cpu_ram()
    with _snapshot_lock:
        _snapshot["cpu_usage"] = cpu
        _snapshot["memory_usage"] = mem


def _sample_gpu_disk() -> None:
    global _pdh
    disk = _sample_disk()
    samples: List[Tuple[str, str, float]] = []
    pdh_ok = False
    if _pdh is not None:
        try:
            _pdh.maybe_reopen()
            samples = _pdh.collect()
            pdh_ok = True
        except Exception as exc:
            print(f"WARNING: PDH collect failed, reopening: {exc}", file=sys.stderr)
            try:
                _pdh.open()
            except Exception as reopen_exc:
                print(f"WARNING: PDH reopen failed: {reopen_exc}", file=sys.stderr)
    info = _cached_identity("gpu_info", {})
    npu_info = _cached_identity("npu_info", {})
    if pdh_ok:
        gpu_usage = _join_gpu_usage((info or {}).get("all_gpus") or [], samples)
        npu_usage = _npu_from_samples(samples, (npu_info or {}).get("npu_model"))
    else:
        # Keep the last numbers. Do not publish zeros because a collect failed.
        previous = _copy_snapshot()
        gpu_usage = previous["gpu_usage"]
        npu_usage = previous["npu_usage"]
        if npu_usage.get("npu_model") is None and (npu_info or {}).get("npu_model"):
            npu_usage = dict(npu_usage)
            npu_usage["npu_model"] = npu_info.get("npu_model")
    with _snapshot_lock:
        _snapshot["gpu_usage"] = gpu_usage
        _snapshot["npu_usage"] = npu_usage
        _snapshot["storage_live"] = disk


def _sample_once() -> None:
    _publish_cpu_ram()
    _sample_gpu_disk()


def _identity_warmup() -> None:
    """CIM once at start, then only while a metrics client is active and TTL expired."""
    jobs = (
        ("gpu_info", _get_gpu_info),
        ("npu_info", _get_npu_info),
        ("memory_info", _get_memory_info),
        ("storage_info", _get_storage_info),
        ("chassis_info", _get_chassis_info),
        ("power_info", _get_power_info),
    )
    primed = False
    while True:
        try:
            if not primed or _metrics_client_active():
                for key, func in jobs:
                    with _cache_lock:
                        item = _cache.get(key)
                        fresh = item is not None and (time.time() - item[1]) < _cache_ttl.get(key, 30.0)
                    if fresh:
                        continue
                    _get_cached(key, func)
            primed = True
        except Exception as exc:
            print(f"WARNING: Identity warm-up failed: {exc}", file=sys.stderr)
        time.sleep(5.0)


def _prime_cpu_times() -> None:
    """Baseline after PDH is open, so setup cost is not the first CPU reading."""
    global _cpu_times_prev
    times = _read_system_times()
    _cpu_times_prev = (times[0], times[1], times[2], time.time())


def _sampler_loop() -> None:
    global _pdh
    _pdh = _PdhUsageQuery()
    try:
        _pdh.open()
    except Exception as exc:
        print(f"WARNING: PDH open failed: {exc}", file=sys.stderr)
    try:
        _prime_cpu_times()
        # Drop the startup second (PDH open / CIM). A short or busy setup
        # slice was being held and read ~50% while Task Manager showed ~10%.
        for _ in range(2):
            time.sleep(1.0)
            _prime_cpu_times()
    except OSError as exc:
        print(f"WARNING: CPU prime failed: {exc}", file=sys.stderr)
    period = None
    elapsed = 0.0
    while True:
        time.sleep(1.0)
        elapsed += 1.0
        # CPU/RAM stay on a 1s window even when GPU/NPU idle at 10s.
        # A 10s average does not match Task Manager's overall %.
        try:
            _publish_cpu_ram()
        except Exception as exc:
            print(f"WARNING: CPU sample failed: {exc}", file=sys.stderr)
        nxt = _SAMPLER_HOT_SEC if _metrics_client_active() else _SAMPLER_IDLE_SEC
        if nxt != period:
            period = nxt
            print(f"INFO: host sampler period {period:.0f}s", file=sys.stderr)
        if elapsed + 1e-6 < period:
            continue
        elapsed = 0.0
        try:
            _sample_gpu_disk()
        except Exception as exc:
            print(f"WARNING: Sampler tick failed: {exc}", file=sys.stderr)


@app.on_event("startup")
async def startup_event():
    """Listen immediately. CIM and PDH run beside the server, not before it."""
    if os.environ.get("ANRO_HOST_SKIP_SAMPLER") == "1":
        return
    Thread(target=_identity_warmup, name="host-identity", daemon=True).start()
    Thread(target=_sampler_loop, name="host-sampler", daemon=True).start()


def _find_powershell() -> Optional[str]:
    """Find PowerShell executable."""
    ps_paths = [
        r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
        r'C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe',
        'powershell.exe',
        'pwsh.exe'
    ]
    
    for path in ps_paths:
        if os.path.exists(path) or path in ['powershell.exe', 'pwsh.exe']:
            return path
    return None


def _execute_powershell(script: str, timeout: int = 10) -> Dict[str, Any]:
    """
    Execute PowerShell script and return result.
    
    Returns:
        Dict with 'success', 'output', 'error', 'returncode'
    """
    ps_cmd = _find_powershell()
    if not ps_cmd:
        return {
            'success': False,
            'error': 'PowerShell not found',
            'output': '',
            'returncode': -1
        }
    
    try:
        proc = subprocess.run(
            [ps_cmd, '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        return {
            'success': proc.returncode == 0,
            'output': proc.stdout.strip(),
            'error': proc.stderr.strip(),
            'returncode': proc.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': 'PowerShell execution timed out',
            'output': '',
            'returncode': -1
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'output': '',
            'returncode': -1
        }


def _get_gpu_info() -> Dict[str, Any]:
    """Get GPU information from Windows via PowerShell."""
    gpu_script = '''
    try {
        Get-CimInstance Win32_VideoController -ErrorAction Stop | Where-Object { 
            $_.Name -and $_.Name.Trim() -ne ""
        } | Where-Object {
            $name = $_.Name.ToLower()
            $name -notlike "*sharing monitor*" -and
            $name -notlike "*microsoft basic display*" -and
            $name -notlike "*microsoft corporation device*" -and
            $name -notlike "*remote desktop*" -and
            $name -notlike "*virtual display*" -and
            $name -notlike "*microsoft remote*" -and
            $name -notlike "*npu*" -and
            $name -notlike "*compute accelerator*" -and
            $name -notlike "*ai boost*"
        } | ForEach-Object {
            $dedicatedRAM = $_.AdapterRAM
            if ($dedicatedRAM -lt 0) {
                $dedicatedRAM = [uint64]($dedicatedRAM + [Math]::Pow(2, 64))
            }
            
            # Try to get VRAM from registry
            $vramBytes = $null
            try {
                $regPath = "HKLM:\\SYSTEM\\ControlSet001\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\*"
                $regGpus = Get-ItemProperty -Path $regPath -Name "HardwareInformation.QwMemorySize","DriverDesc" -ErrorAction SilentlyContinue
                foreach ($regGpu in $regGpus) {
                    if ($regGpu.DriverDesc -and $regGpu."HardwareInformation.QwMemorySize") {
                        $desc = $regGpu.DriverDesc.ToLower()
                        $name = $_.Name.ToLower()
                        if ($desc -like "*$name*" -or $name -like "*$desc*") {
                            $vramBytes = $regGpu."HardwareInformation.QwMemorySize"
                            break
                        }
                    }
                }
            } catch {}
            
            if ($vramBytes) {
                $dedicatedRAM = $vramBytes
            }
            
            $nameLower = $_.Name.ToLower()
            $isDiscrete = $false
            $isIntegrated = $false
            
            if ($nameLower -like "*780m*" -or $nameLower -like "*680m*" -or $nameLower -like "*vega*" -or 
                $nameLower -like "*uhd graphics*" -or $nameLower -like "*iris*" -or $nameLower -like "*intel hd*" -or 
                $nameLower -like "*intel uhd*" -or ($nameLower -like "*graphics*" -and $nameLower -notlike "*rx*" -and $nameLower -notlike "*rtx*" -and $nameLower -notlike "*gtx*")) {
                $isIntegrated = $true
            } elseif ($nameLower -like "*rx*" -or $nameLower -like "*rtx*" -or $nameLower -like "*gtx*" -or 
                      $nameLower -like "*geforce*" -or $nameLower -like "*radeon rx*" -or $nameLower -like "*arc*") {
                $isDiscrete = $true
            } else {
                if ($dedicatedRAM -ge 8589934592) {
                    $isDiscrete = $true
                } else {
                    $isIntegrated = $true
                }
            }
            
            [PSCustomObject]@{
                Name = $_.Name
                AdapterRAM = $dedicatedRAM
                DriverVersion = $_.DriverVersion
                Status = $_.Status
                GPUType = if ($isDiscrete) { "Discrete" } else { "Integrated" }
            }
        } | ConvertTo-Json -Depth 3
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(gpu_script, timeout=10)
    
    if not result['success'] or not result['output']:
        return {
            'error': result.get('error', 'Failed to get GPU info'),
            'all_gpus': []
        }
    
    try:
        output = result['output'].strip()
        if output == '[]' or output == 'null':
            return {'all_gpus': []}
        
        gpus_data = json.loads(output)
        if not isinstance(gpus_data, list):
            gpus_data = [gpus_data] if gpus_data else []
        
        if not gpus_data:
            return {'all_gpus': []}
        
        gpu_list = []
        for gpu_data in gpus_data:
            gpu_name = gpu_data.get('Name', 'Unknown GPU')
            adapter_ram = gpu_data.get('AdapterRAM', 0)
            dedicated_vram_gb = round(adapter_ram / (1024**3), 2) if adapter_ram else 0
            gpu_type = gpu_data.get('GPUType', 'Unknown')
            driver_version = gpu_data.get('DriverVersion', 'Unknown')
            
            gpu_info = {
                "gpu_model": gpu_name,
                "gpu_type": gpu_type,
                "gpu_driver_version": driver_version,
                "gpu_dedicated_vram": dedicated_vram_gb,
                "gpu_shared_vram": None,
                "gpu_status": gpu_data.get('Status', 'Unknown'),
                "gpu_error_code": 0,
            }
            gpu_list.append(gpu_info)
        
        # Return primary GPU (discrete first, then integrated) for backward compatibility
        # Also return all GPUs
        primary_gpu = None
        for gpu in gpu_list:
            if gpu["gpu_type"] == "Discrete":
                primary_gpu = gpu
                break
        if not primary_gpu and gpu_list:
            primary_gpu = gpu_list[0]
        
        if primary_gpu:
            primary_gpu["gpu_vram"] = primary_gpu.get("gpu_dedicated_vram", 0)
            return {
                **primary_gpu,
                "all_gpus": gpu_list,
            }
        
        # Fallback: return all GPUs even if no primary found
        return {"all_gpus": gpu_list}
    except json.JSONDecodeError as e:
        return {
            'error': f'Failed to parse GPU data: {e}',
            'all_gpus': []
        }


def _get_npu_info() -> Dict[str, Any]:
    """Get NPU information from Windows via PowerShell."""
    # Get CPU info first to determine NPU TOPS
    cpu_script = '''
    try {
        $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1
        if ($cpu -and $cpu.Name) {
            Write-Output $cpu.Name
        } else {
            Write-Error "No CPU found"
            exit 1
        }
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    cpu_result = _execute_powershell(cpu_script, timeout=5)
    cpu_model = ""
    npu_tops = 16  # Default
    
    if cpu_result['success'] and cpu_result['output']:
        cpu_model = cpu_result['output'].strip().lower()
        # AMD TOPS estimation
        if 'ryzen ai max' in cpu_model or 'ai max' in cpu_model:
            npu_tops = 50
        elif 'hx 370' in cpu_model or 'hx 365' in cpu_model or 'ai 9' in cpu_model:
            npu_tops = 50
        elif re.search(r'ryzen\s*8\d{3}', cpu_model) or any(x in cpu_model for x in ['8845', '8840', '8945', '8940', '8040', '8045']):
            npu_tops = 16
        # Intel TOPS estimation
        elif 'ultra' in cpu_model:
            if ' 2' in cpu_model or 'v' in cpu_model.split()[-1] or '258v' in cpu_model: # Lunar Lake (Ultra 200 series)
                npu_tops = 47
            else: # Meteor Lake (Ultra 100 series)
                npu_tops = 11
    
    npu_script = '''
    try {
        # Search for NPU in PnP entities
        $npu = Get-CimInstance Win32_PnPEntity -ErrorAction Stop | Where-Object { 
            (($_.Name -like "*NPU*" -and $_.Name -like "*Compute Accelerator*") -or 
             $_.Name -like "*XDNA*" -or 
             ($_.Name -like "*AMD*" -and $_.Name -like "*Neural Processing*") -or
             ($_.Name -like "*AMD*" -and $_.Name -like "*AI Engine*") -or
             $_.Name -like "*Intel*AI Boost*") -and
            $_.Status -eq "OK"
        } | Select-Object -First 1
        
        if ($npu) {
            # Try to get driver info
            $driver = Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceID -eq $npu.PNPDeviceID } | Select-Object -First 1
            
            [PSCustomObject]@{
                Name = $npu.Name
                Status = $npu.Status
                DriverVersion = if ($driver) { $driver.DriverVersion } else { "Unknown" }
                DeviceID = $npu.PNPDeviceID
            } | ConvertTo-Json -Depth 3
        } else {
            Write-Output "{}"
        }
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(npu_script, timeout=5)
    
    if not result['success'] or not result['output'] or result['output'] == "{}":
        return {}
    
    try:
        npu_data = json.loads(result['output'].strip())
        if isinstance(npu_data, dict) and npu_data.get('Name'):
            npu_name = npu_data.get('Name', '')
            # Try to extract TOPS from name
            tops_match = re.search(r'(\d+)\s*TOPS?', npu_name, re.IGNORECASE)
            if tops_match:
                npu_tops = int(tops_match.group(1))
            
            # Determine driver and vendor based on name and CPU
            driver_name = "Unknown"
            if 'AMD' in npu_name or 'XDNA' in npu_name:
                driver_name = "AMD XDNA Driver"
            elif 'Intel' in npu_name:
                driver_name = "Intel(R) AI Boost Driver"
            else:
                # Fallback: check CPU model if NPU name is generic (e.g. "NPU Compute Accelerator")
                if 'intel' in cpu_model or 'core' in cpu_model:
                    driver_name = "Intel(R) AI Boost Driver"
                elif 'amd' in cpu_model or 'ryzen' in cpu_model:
                    driver_name = "AMD XDNA Driver"
            
            return {
                "npu_model": npu_name,
                "npu_driver": driver_name,
                "npu_driver_version": npu_data.get('DriverVersion', 'Unknown'),
                "npu_max_tops": npu_tops
            }
    except json.JSONDecodeError:
        pass
    
    return {}


def _get_memory_info() -> Dict[str, Any]:
    """Get memory information from Windows via PowerShell."""
    mem_script = '''
    try {
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        $dimms = @(Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue)
        $speed = 0
        $width = 64
        $count = 0
        $type = "Unknown"
        if ($dimms.Count -gt 0) {
            $pop = @($dimms | Where-Object { $_.Capacity -and $_.Capacity -gt 0 })
            if ($pop.Count -eq 0) { $pop = $dimms }
            $count = $pop.Count
            $first = $pop[0]
            if ($first.ConfiguredClockSpeed) { $speed = [int]$first.ConfiguredClockSpeed }
            elseif ($first.Speed) { $speed = [int]$first.Speed }
            if ($first.DataWidth) { $width = [int]$first.DataWidth }
            $type = "DDR"
        }
        [PSCustomObject]@{
            TotalPhysicalMemory = $cs.TotalPhysicalMemory
            ram_speed = $speed
            ram_type = $type
            module_count = $count
            data_width_bits = $width
        } | ConvertTo-Json -Compress
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(mem_script, timeout=5)
    
    if result['success'] and result['output']:
        try:
            raw = result['output'].strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                total_memory_bytes = int(data.get("TotalPhysicalMemory") or 0)
                total_memory_gb = round(total_memory_bytes / (1024**3), 2) if total_memory_bytes else 0
                return {
                    "ram_size": total_memory_gb,
                    "ram_type": data.get("ram_type") or "Unknown",
                    "ram_speed": int(data.get("ram_speed") or 0),
                    "module_count": int(data.get("module_count") or 0),
                    "data_width_bits": int(data.get("data_width_bits") or 64),
                }
            total_memory_bytes = int(raw)
            total_memory_gb = round(total_memory_bytes / (1024**3), 2)
            return {
                "ram_size": total_memory_gb,
                "ram_type": "Unknown",
                "ram_speed": 0
            }
        except (ValueError, json.JSONDecodeError, TypeError):
            pass
    
    return {
        "ram_size": 0,
        "ram_type": "Unknown",
        "ram_speed": 0
    }


def _get_chassis_info() -> Dict[str, Any]:
    script = r'''
    try {
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        [PSCustomObject]@{
            manufacturer = $cs.Manufacturer
            model = $cs.Model
            system_family = $cs.SystemFamily
        } | ConvertTo-Json -Compress
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    result = _execute_powershell(script, timeout=8)
    if result["success"] and result["output"]:
        try:
            data = json.loads(result["output"].strip())
            return {
                "manufacturer": (data.get("manufacturer") or "").strip() or None,
                "model": (data.get("model") or "").strip() or None,
                "system_family": (data.get("system_family") or "").strip() or None,
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"manufacturer": None, "model": None, "system_family": None}


def _get_power_info() -> Dict[str, Any]:
    script = r'''
    $mode = "unknown"
    $ac = $null
    $plan = $null
    try {
        $batt = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue
        if ($batt) {
            $st = [int]$batt[0].BatteryStatus
            if ($st -eq 2) { $mode = "plugged"; $ac = $true }
            elseif ($st -ge 1) { $mode = "battery"; $ac = $false }
        } else {
            $mode = "plugged"; $ac = $true
        }
    } catch {}
    try {
        $p = Get-CimInstance -Namespace root\cimv2\power -ClassName Win32_PowerPlan -ErrorAction SilentlyContinue |
            Where-Object { $_.IsActive } | Select-Object -First 1
        if ($p) { $plan = $p.ElementName }
    } catch {}
    [PSCustomObject]@{ mode = $mode; ac_powered = $ac; plan_name = $plan } | ConvertTo-Json -Compress
    '''
    result = _execute_powershell(script, timeout=8)
    if result["success"] and result["output"]:
        try:
            data = json.loads(result["output"].strip())
            return {
                "mode": data.get("mode") or "unknown",
                "ac_powered": data.get("ac_powered"),
                "plan_name": data.get("plan_name"),
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"mode": "unknown", "ac_powered": None, "plan_name": None}


def _get_memory_usage() -> Dict[str, Any]:
    """Live RAM from the sampler snapshot. Does not start powershell.exe."""
    snap = _copy_snapshot()["memory_usage"]
    return {
        "total": int(snap.get("total") or 0),
        "used": int(snap.get("used") or 0),
        "available": int(snap.get("available") or 0),
    }


def _get_cpu_usage() -> Dict[str, Any]:
    """Processor-time delta from the sampler. Never % Processor Utility, never a wait."""
    snap = _copy_snapshot()["cpu_usage"]
    util = snap.get("utilization")
    if not isinstance(util, (int, float)):
        util = 0.0
    return {
        "utilization": float(util),
        "frequency": int(snap.get("frequency") or 0),
        "physical_cores": int(snap.get("physical_cores") or 0),
        "logical_cores": int(snap.get("logical_cores") or 0),
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "anro-llm-system-info"}


@app.get("/api/version")
async def get_version():
    """Host-service version. Does not have to match the agent image."""
    return {"version": __version__, "service": "windows-host-service"}


@app.get("/api/cpu/usage")
async def get_cpu_usage():
    """Copy the sampler snapshot. Do not take a fresh sample on this request."""
    _touch_metrics_client()
    return _get_cpu_usage()


def _get_gpu_usage() -> Dict[str, Any]:
    """GPU usage from the sampler snapshot. No CIM and no powershell.exe."""
    snap = _copy_snapshot()["gpu_usage"]
    gpus = []
    for row in snap.get("gpus") or []:
        util = row.get("utilization")
        if not isinstance(util, (int, float)):
            util = 0.0
        cleaned = {
            "name": row.get("name") or "Unknown GPU",
            "utilization": float(util),
            "memory_used_mb": float(row.get("memory_used_mb") or 0.0),
            "memory_total_mb": float(row.get("memory_total_mb") or 0.0),
            "vendor": row.get("vendor") or "Unknown",
        }
        if row.get("adapter_index") is not None:
            cleaned["adapter_index"] = row.get("adapter_index")
        gpus.append(cleaned)
    return {"gpus": gpus}


@app.get("/api/gpu")
async def get_gpu():
    """GPU identity. CIM only on cache miss, never from the usage sampler."""
    _touch_metrics_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached("gpu_info", _get_gpu_info))


@app.get("/api/gpu/usage")
async def get_gpu_usage():
    """Copy the sampler snapshot. Do not collect PDH on this request."""
    _touch_metrics_client()
    return _get_gpu_usage()


def _get_npu_usage() -> Dict[str, Any]:
    """NPU usage from the sampler. Does not call _get_npu_info() or PnP."""
    snap = _copy_snapshot()["npu_usage"]
    util = snap.get("npu_utilization")
    if not isinstance(util, (int, float)):
        util = 0.0
    return {"npu_utilization": float(util), "npu_model": snap.get("npu_model")}


@app.get("/api/npu")
async def get_npu():
    """NPU identity. CIM only on cache miss."""
    _touch_metrics_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached("npu_info", _get_npu_info))


@app.get("/api/npu/usage")
async def get_npu_usage():
    """Copy the sampler snapshot. Do not query PnP or the CPU model."""
    _touch_metrics_client()
    return _get_npu_usage()


@app.get("/api/memory")
async def get_memory():
    """Memory capacity. CIM only on cache miss."""
    _touch_metrics_client()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached('memory_info', _get_memory_info))


@app.get("/api/chassis")
async def get_chassis():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached("chassis_info", _get_chassis_info))


@app.get("/api/power")
async def get_power():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached("power_info", _get_power_info))


@app.get("/api/memory/usage")
async def get_memory_usage():
    """Copy the sampler snapshot. Do not query Win32_OperatingSystem."""
    _touch_metrics_client()
    return _get_memory_usage()


def _get_storage_info() -> Dict[str, Any]:
    """Get storage information from Windows via PowerShell."""
    storage_script = '''
    try {
        # Get C: drive (primary storage)
        $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'" -ErrorAction Stop | Select-Object -First 1
        if ($disk) {
            $sizeGB = [math]::Round($disk.Size / [Math]::Pow(1024, 3), 2)
            $freeGB = [math]::Round($disk.FreeSpace / [Math]::Pow(1024, 3), 2)
            
            # Try to determine if it's SSD or HDD
            $mediaType = "HDD"
            try {
                $physicalDisk = Get-CimInstance Win32_DiskDrive | Where-Object { $_.DeviceID -like "*0" } | Select-Object -First 1
                if ($physicalDisk) {
                    $mediaTypeNum = $physicalDisk.MediaType
                    # MediaType values: 3 = Fixed hard disk, 4 = Removable media, 5 = Optical disk
                    # For SSD detection, check if it's a fixed disk and look for SSD indicators
                    if ($physicalDisk.Model -like "*SSD*" -or $physicalDisk.Model -like "*Solid State*") {
                        $mediaType = "SSD"
                    } elseif ($physicalDisk.MediaType -eq 3) {
                        # Fixed hard disk - assume HDD unless model indicates SSD
                        $mediaType = "HDD"
                    }
                }
            } catch {
                # If we can't determine, default to HDD
            }
            
            [PSCustomObject]@{
                StorageSize = $sizeGB
                StorageFree = $freeGB
                StorageType = $mediaType
            } | ConvertTo-Json -Depth 3
        } else {
            Write-Error "C: drive not found"
            exit 1
        }
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(storage_script, timeout=5)
    
    if result['success'] and result['output']:
        try:
            output = result['output'].strip()
            if output:
                storage_data = json.loads(output)
                storage_size = storage_data.get('StorageSize', 0)
                storage_free = storage_data.get('StorageFree', 0)
                storage_type = storage_data.get('StorageType', 'HDD')
                
                # Only return if we got valid data
                if storage_size > 0:
                    return {
                        "storage_size": storage_size,
                        "storage_free": storage_free,
                        "storage_type": storage_type
                    }
        except (json.JSONDecodeError, ValueError) as e:
            # Log error for debugging
            print(f"ERROR: Failed to parse storage data: {e}, output: {result.get('output', '')}", file=sys.stderr)
    
    # Log error for debugging
    if not result['success']:
        print(f"ERROR: PowerShell script failed: {result.get('error', 'Unknown error')}, output: {result.get('output', '')}", file=sys.stderr)
    
    return {
        "storage_size": 0,
        "storage_free": 0,
        "storage_type": "Unknown"
    }


def _storage_view() -> Dict[str, Any]:
    """Capacity/type from identity cache. Free space from the sampler, not CIM."""
    info = _cached_identity("storage_info", {}) or {}
    live = _copy_snapshot().get("storage_live") or {}
    size = info.get("storage_size") or live.get("storage_size_live") or 0
    free = live.get("storage_free")
    if free is None:
        free = info.get("storage_free") or 0
    return {
        "storage_size": size,
        "storage_free": free,
        "storage_type": info.get("storage_type") or "Unknown",
    }


def _specs_from_cache(snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Last identity plus current usage. A CIM miss must not delay usage fields."""
    if snap is None:
        snap = _copy_snapshot()
    return {
        "gpu_info": _cached_identity("gpu_info", {"all_gpus": []}),
        "gpu_usage": snap["gpu_usage"],
        "npu_info": _cached_identity("npu_info", {}),
        "npu_usage": snap["npu_usage"],
        "memory_info": _cached_identity("memory_info", {}),
        "storage_info": _storage_view(),
        "chassis_info": _cached_identity("chassis_info", {}),
        "power_info": _cached_identity("power_info", {}),
    }


@app.get("/api/storage")
async def get_storage():
    """Storage view. Does not start powershell.exe on this request."""
    _touch_metrics_client()
    return _storage_view()


@app.get("/api/system-specs")
async def get_system_specs():
    """Last identity plus sampler usage. Does not collect counters inline."""
    _touch_metrics_client()
    return _specs_from_cache()


@app.get("/api/metrics/bundle")
async def get_metrics_bundle():
    """Single round-trip. Copies the snapshot. Does not start powershell.exe."""
    _touch_metrics_client()
    snap = _copy_snapshot()
    system_specs = _specs_from_cache(snap)
    return {
        "cpu_usage": snap["cpu_usage"],
        "memory_usage": snap["memory_usage"],
        "system_specs": system_specs,
        "gpu_usage": snap["gpu_usage"],
    }


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Windows Host System Info Service")
    parser.add_argument(
        '--port',
        type=int,
        default=8088,
        help='Port to listen on (default: 8088)'
    )
    parser.add_argument(
        '--host',
        type=str,
        default='127.0.0.1',
        help='Host to bind to (default: 127.0.0.1, use 0.0.0.0 to allow external access)'
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print(f"Anro LLM System Information Service v{__version__}")
    print("=" * 60)
    print(f"Starting service on {args.host}:{args.port}")
    print()
    print("Available endpoints:")
    print("  GET /health - Health check")
    print("  GET /api/version - Service version")
    print("  GET /api/gpu - Get GPU information")
    print("  GET /api/gpu/usage - Get GPU utilization and memory usage")
    print("  GET /api/metrics/bundle - Combined metrics for llm-management")
    print("  GET /api/npu - Get NPU information")
    print("  GET /api/npu/usage - Get NPU utilization")
    print("  GET /api/memory - Get memory information")
    print("  GET /api/system-specs - Get full system specifications")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)
    print()
    
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
