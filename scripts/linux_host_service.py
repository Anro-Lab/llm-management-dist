#!/usr/bin/env python3
"""
Anro LLM System Information Service (Linux host, port 8088).

Machine-wide metrics for Docker-on-Ubuntu / EdgeExpert / DGX.
API-compatible with scripts/windows_host_service.py so llm-management can use
HOST_SYSTEM_SERVICE_URL / WINDOWS_HOST_SERVICE_URL unchanged.

Usage:
    python3 scripts/linux_host_service.py [--port 8088] [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

try:
    from fastapi import FastAPI
    import uvicorn
except ImportError:
    print("ERROR: Required packages not installed. Please install:")
    print("  pip install fastapi uvicorn psutil")
    sys.exit(1)

try:
    import psutil
except ImportError:
    print("ERROR: psutil is required: pip install psutil")
    sys.exit(1)

# Keep in sync with src/__init__.py
__version__ = "1.0.0"

app = FastAPI(
    title="Linux Host System Info Service",
    description="Provides machine-wide Linux system information (GPU, NPU, CPU, Memory, chassis, power) via HTTP API",
    version=__version__,
)

_cache: Dict[str, Tuple[Any, float]] = {}
_cache_lock = Lock()
_cache_ttl = {
    "gpu_usage": 2.0,
    "npu_usage": 2.0,
    "gpu_info": 30.0,
    "npu_info": 30.0,
    "memory_info": 30.0,
    "storage_info": 5.0,
    "chassis_info": 300.0,
    "power_info": 30.0,
}
_refresh_intervals = {
    "gpu_usage": 1.5,
    "npu_usage": 1.5,
}
_refresh_tasks: Dict[str, Any] = {}
_refresh_last_time: Dict[str, float] = {}


def _get_cached(key: str, func, *args, **kwargs):
    now = time.time()
    with _cache_lock:
        if key in _cache:
            value, timestamp = _cache[key]
            ttl = _cache_ttl.get(key, 1.0)
            if now - timestamp < ttl:
                refresh_interval = _refresh_intervals.get(key)
                if refresh_interval and (now - _refresh_last_time.get(key, 0)) >= refresh_interval:
                    _trigger_background_refresh(key, func, *args, **kwargs)
                return value
    value = func(*args, **kwargs)
    with _cache_lock:
        _cache[key] = (value, now)
        _refresh_last_time[key] = now
    return value


def _trigger_background_refresh(key: str, func, *args, **kwargs):
    now = time.time()
    last_refresh = _refresh_last_time.get(key, 0)
    refresh_interval = _refresh_intervals.get(key)
    if not refresh_interval or (now - last_refresh) < refresh_interval:
        return
    if key in _refresh_tasks and not _refresh_tasks[key].done():
        return

    async def refresh_task():
        try:
            loop = asyncio.get_running_loop()
            value = await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            with _cache_lock:
                _cache[key] = (value, time.time())
                _refresh_last_time[key] = time.time()
        except Exception as e:
            print(f"WARNING: Background refresh failed for {key}: {e}", file=sys.stderr)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        value = func(*args, **kwargs)
        with _cache_lock:
            _cache[key] = (value, time.time())
            _refresh_last_time[key] = time.time()
        return
    _refresh_tasks[key] = loop.create_task(refresh_task())


async def _background_refresh_loop():
    while True:
        try:
            await asyncio.sleep(1.0)
            now = time.time()
            with _cache_lock:
                cache_keys = list(_cache.keys())
            for key in cache_keys:
                refresh_interval = _refresh_intervals.get(key)
                if not refresh_interval:
                    continue
                if (now - _refresh_last_time.get(key, 0)) >= refresh_interval:
                    func_map = {
                        "gpu_usage": _get_gpu_usage,
                        "npu_usage": _get_npu_usage,
                        "memory_info": _get_memory_info,
                    }
                    func = func_map.get(key)
                    if func:
                        _trigger_background_refresh(key, func)
        except Exception as e:
            print(f"WARNING: Background refresh loop error: {e}", file=sys.stderr)
            await asyncio.sleep(5.0)


@app.on_event("startup")
async def startup_event():
    try:
        _get_cached("gpu_info", _get_gpu_info)
        _get_cached("npu_info", _get_npu_info)
    except Exception as e:
        print(f"WARNING: Cache warm-up failed: {e}", file=sys.stderr)
    asyncio.create_task(_background_refresh_loop())


def _run(cmd: List[str], timeout: float = 8.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def _is_phantom_gpu_name(name: str) -> bool:
    n = (name or "").lower().strip()
    if n.startswith(":"):
        n = n.lstrip(":").strip()
    if not n:
        return True
    if any(x in n for x in ("llvmpipe", "virgl", "swrast", "softpipe", "lavapipe", "sharing monitor")):
        return True
    if "microsoft" in n and (
        any(x in n for x in ("basic", "render", "remote", "indirect", "warp", "virtual display", "corporation device"))
        or re.search(r"\bdevice\s+[0-9a-f]{4}\b", n)
    ):
        return True
    return False


def _classify_gpu_type(name: str, vram_gb: float) -> str:
    n = (name or "").lower()
    if any(
        x in n
        for x in (
            "780m",
            "760m",
            "680m",
            "890m",
            "8060s",
            "uhd",
            "iris",
            "xe graphics",
            "radeon(tm) graphics",
            "radeon graphics",
        )
    ):
        return "Integrated"
    if re.search(r"\brtx\b|\bgtx\b|\brx\s*\d{3,4}|\barc\s*[ab]?\d|quadro|tesla|radeon\s*ai\s*pro|\br\d{4}\b", n):
        return "Discrete"
    if "graphics" in n and "rx" not in n and "rtx" not in n:
        return "Integrated"
    if vram_gb >= 6:
        return "Discrete"
    return "Integrated"


def _infer_vendor(name: str) -> str:
    u = (name or "").upper()
    if "NVIDIA" in u or "GEFORCE" in u or "RTX" in u or "GTX" in u:
        return "NVIDIA"
    if "AMD" in u or "RADEON" in u or "ATI" in u:
        return "AMD"
    if "INTEL" in u or "ARC" in u or "UHD" in u or "IRIS" in u:
        return "Intel"
    return "Unknown"


@lru_cache(maxsize=1)
def _read_os_release() -> Dict[str, str]:
    out: Dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.is_file():
        return out
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def _cpu_brand() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine() or "Unknown CPU"


def _get_cpu_usage() -> Dict[str, Any]:
    # First call may return 0.0; brief interval improves accuracy.
    util = psutil.cpu_percent(interval=0.2)
    freq = 0
    try:
        f = psutil.cpu_freq()
        if f and f.current:
            freq = int(f.current)
    except Exception:
        pass
    return {
        "utilization": float(util),
        "frequency": freq,
        "physical_cores": int(psutil.cpu_count(logical=False) or 0),
        "logical_cores": int(psutil.cpu_count(logical=True) or 0),
        "brand": _cpu_brand(),
    }


def _read_dmi_id(key: str) -> Optional[str]:
    path = Path(f"/sys/class/dmi/id/{key}")
    if not path.is_file():
        return None
    try:
        val = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    return val or None


def _get_chassis_info() -> Dict[str, Any]:
    """TTD5: OEM / SKU fields for brand classification (parity with Windows /api/chassis)."""
    return {
        "manufacturer": _read_dmi_id("sys_vendor"),
        "model": _read_dmi_id("product_name"),
        "system_family": _read_dmi_id("product_family"),
    }


def _get_power_info() -> Dict[str, Any]:
    """TTD5: plugged | battery | unknown (parity with Windows /api/power)."""
    mode: str = "unknown"
    ac_powered: Optional[bool] = None
    plan_name: Optional[str] = None
    try:
        psy = Path("/sys/class/power_supply")
        if psy.is_dir():
            saw_battery = False
            for entry in psy.iterdir():
                type_path = entry / "type"
                if not type_path.is_file():
                    continue
                kind = type_path.read_text(encoding="utf-8", errors="ignore").strip()
                if kind == "Mains":
                    online_path = entry / "online"
                    if online_path.is_file():
                        online = online_path.read_text(encoding="utf-8", errors="ignore").strip() == "1"
                        ac_powered = online
                        mode = "plugged" if online else "battery"
                        break
                elif kind == "Battery":
                    saw_battery = True
                    status_path = entry / "status"
                    if status_path.is_file():
                        st = status_path.read_text(encoding="utf-8", errors="ignore").strip().lower()
                        if st == "charging":
                            mode, ac_powered = "plugged", True
                            break
                        if st in ("discharging", "not charging"):
                            mode, ac_powered = "battery", False
            if mode == "unknown" and not saw_battery:
                # Desktop / server without battery → treat as AC.
                mode, ac_powered = "plugged", True
    except OSError:
        pass
    return {"mode": mode, "ac_powered": ac_powered, "plan_name": plan_name}


def _dmidecode_memory_details() -> Tuple[str, int, int, int]:
    """
    Parse `dmidecode -t memory` for type / speed / module count / data width.
    Returns (ram_type, ram_speed_mhz, module_count, data_width_bits).
    Requires root on many distros; returns defaults when unavailable.
    """
    ram_type = "Unknown"
    ram_speed = 0
    module_count = 0
    data_width = 64
    code, out, _ = _run(["dmidecode", "-t", "memory"], timeout=6)
    if code != 0 or not out:
        return ram_type, ram_speed, module_count, data_width

    blocks = re.split(r"\n(?=Memory Device\b)", out)
    for block in blocks:
        if "Memory Device" not in block:
            continue
        size_m = re.search(r"^\s*Size:\s*(.+)$", block, re.MULTILINE)
        if not size_m:
            continue
        size_val = size_m.group(1).strip().lower()
        if "no module" in size_val or size_val in ("0", "unknown", "not installed"):
            continue
        module_count += 1

        type_m = re.search(r"^\s*Type:\s*(.+)$", block, re.MULTILINE)
        if type_m:
            t = type_m.group(1).strip()
            if t and t.lower() not in ("unknown", "<out of spec>"):
                ram_type = t

        speed_m = re.search(
            r"^\s*Configured Memory Speed:\s*(\d+)\s*MT/s",
            block,
            re.MULTILINE | re.IGNORECASE,
        ) or re.search(
            r"^\s*Configured Clock Speed:\s*(\d+)\s*MHz",
            block,
            re.MULTILINE | re.IGNORECASE,
        ) or re.search(
            r"^\s*Speed:\s*(\d+)\s*(?:MT/s|MHz)",
            block,
            re.MULTILINE | re.IGNORECASE,
        )
        if speed_m and not ram_speed:
            try:
                ram_speed = int(speed_m.group(1))
            except ValueError:
                pass

        width_m = re.search(
            r"^\s*Data Width:\s*(\d+)\s*bits",
            block,
            re.MULTILINE | re.IGNORECASE,
        ) or re.search(
            r"^\s*Total Width:\s*(\d+)\s*bits",
            block,
            re.MULTILINE | re.IGNORECASE,
        )
        if width_m:
            try:
                data_width = int(width_m.group(1))
            except ValueError:
                pass

    return ram_type, ram_speed, module_count, data_width


def _get_memory_info() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    ram_type, ram_speed, module_count, data_width = _dmidecode_memory_details()
    return {
        "ram_size": round(vm.total / (1024**3), 2),
        "ram_type": ram_type or "Unknown",
        "ram_speed": int(ram_speed or 0),
        "module_count": int(module_count or 0),
        "data_width_bits": int(data_width or 64),
    }


def _get_memory_usage() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    return {
        "total": int(vm.total),
        "used": int(vm.used),
        "available": int(vm.available),
    }


def _get_storage_info() -> Dict[str, Any]:
    """Primary root filesystem size (machine-visible mount)."""
    try:
        usage = psutil.disk_usage("/")
        media = "SSD"
        # Best-effort rotational flag for root device
        try:
            code, out, _ = _run(["lsblk", "-ndo", "ROTA,TYPE,SIZE,NAME", "-b"], timeout=3)
            if code == 0:
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "disk":
                        if parts[0].strip() == "1":
                            media = "HDD"
                        break
        except Exception:
            pass
        return {
            "storage_size": round(usage.total / (1024**3), 2),
            "storage_free": round(usage.free / (1024**3), 2),
            "storage_type": media,
        }
    except Exception:
        return {"storage_size": 0, "storage_free": 0, "storage_type": "Unknown"}


def _nvidia_gpus_from_smi() -> List[Dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    code, out, _ = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version,uuid",
            "--format=csv,noheader,nounits",
        ],
        timeout=8,
    )
    if code != 0 or not out.strip():
        return []
    gpus: List[Dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        name = parts[0]
        if _is_phantom_gpu_name(name):
            continue
        try:
            mem_mib = float(parts[1])
        except ValueError:
            mem_mib = 0.0
        vram_gb = round(mem_mib / 1024.0, 2)
        driver = parts[2] if len(parts) > 2 else "Unknown"
        gpus.append(
            {
                "gpu_model": name,
                "gpu_type": _classify_gpu_type(name, vram_gb),
                "gpu_driver_version": driver,
                "gpu_dedicated_vram": vram_gb,
                "gpu_shared_vram": None,
                "gpu_status": "OK",
                "gpu_error_code": 0,
                "gpu_vram": vram_gb,
                "_vendor": "NVIDIA",
            }
        )
    return gpus


def _nvidia_usage_from_smi() -> List[Dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    code, out, _ = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout=8,
    )
    if code != 0 or not out.strip():
        return []
    rows: List[Dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        name = parts[0]
        if _is_phantom_gpu_name(name):
            continue
        try:
            util = float(parts[1])
            mem_used = float(parts[2])
            mem_total = float(parts[3])
        except ValueError:
            util, mem_used, mem_total = 0.0, 0.0, 0.0
        rows.append(
            {
                "name": name,
                "utilization": util,
                "memory_used_mb": mem_used,
                "memory_total_mb": mem_total,
                "vendor": "NVIDIA",
            }
        )
    return rows


def _amd_gpus_from_rocm_or_sysfs() -> List[Dict[str, Any]]:
    gpus: List[Dict[str, Any]] = []
    if shutil.which("rocm-smi"):
        code, out, _ = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"], timeout=8)
        if code == 0 and out.strip():
            try:
                data = json.loads(out)
                # rocm-smi JSON varies by version; keep best-effort
                if isinstance(data, dict):
                    for _card, info in data.items():
                        if not isinstance(info, dict):
                            continue
                        name = (
                            info.get("Card series")
                            or info.get("Card model")
                            or info.get("Device Name")
                            or "AMD GPU"
                        )
                        if _is_phantom_gpu_name(str(name)):
                            continue
                        # Memory often in bytes under nested keys
                        vram_gb = 0.0
                        for k, v in info.items():
                            if "vram" in str(k).lower() and "total" in str(k).lower():
                                try:
                                    vram_gb = round(float(v) / (1024**3), 2)
                                except (TypeError, ValueError):
                                    pass
                        gpus.append(
                            {
                                "gpu_model": str(name),
                                "gpu_type": _classify_gpu_type(str(name), vram_gb),
                                "gpu_driver_version": "ROCm",
                                "gpu_dedicated_vram": vram_gb,
                                "gpu_shared_vram": None,
                                "gpu_status": "OK",
                                "gpu_error_code": 0,
                                "gpu_vram": vram_gb,
                                "_vendor": "AMD",
                            }
                        )
                if gpus:
                    return gpus
            except json.JSONDecodeError:
                pass

    # lspci fallback (catalog only; VRAM often unknown)
    if shutil.which("lspci"):
        code, out, _ = _run(["lspci", "-nn"], timeout=5)
        if code == 0:
            for line in out.splitlines():
                low = line.lower()
                if "microsoft corporation" in low:
                    continue
                if not any(x in low for x in ("vga compatible", "3d controller", "display controller")):
                    continue
                if not any(x in low for x in ("amd", "ati", "radeon", "advanced micro devices")):
                    continue
                # "01:00.0 VGA ...: Vendor Device [1002:...]"
                name = line.split(":", 2)[-1].strip() if line.count(":") >= 2 else line
                name = re.sub(r"\[....:....\]", "", name).strip()
                if _is_phantom_gpu_name(name):
                    continue
                gpus.append(
                    {
                        "gpu_model": name or "AMD GPU",
                        "gpu_type": _classify_gpu_type(name, 0),
                        "gpu_driver_version": "Unknown",
                        "gpu_dedicated_vram": 0,
                        "gpu_shared_vram": None,
                        "gpu_status": "OK",
                        "gpu_error_code": 0,
                        "gpu_vram": 0,
                        "_vendor": "AMD",
                    }
                )
    return gpus


def _get_gpu_info() -> Dict[str, Any]:
    gpu_list: List[Dict[str, Any]] = []
    for g in _nvidia_gpus_from_smi() + _amd_gpus_from_rocm_or_sysfs():
        g = dict(g)
        g.pop("_vendor", None)
        if _is_phantom_gpu_name(g.get("gpu_model", "")):
            continue
        # de-dupe by model name
        if any(x.get("gpu_model") == g.get("gpu_model") for x in gpu_list):
            continue
        gpu_list.append(g)

    primary = None
    for g in gpu_list:
        if g.get("gpu_type") == "Discrete":
            primary = g
            break
    if not primary and gpu_list:
        primary = gpu_list[0]
    if primary:
        return {**primary, "all_gpus": gpu_list}
    return {"all_gpus": gpu_list}


def _get_gpu_usage() -> Dict[str, Any]:
    rows = _nvidia_usage_from_smi()
    if rows:
        return {"gpus": rows}

    # Catalog-only AMD: return 0% util with known names
    info = _get_gpu_info()
    cleaned = []
    for g in info.get("all_gpus") or []:
        name = g.get("gpu_model", "Unknown GPU")
        if _is_phantom_gpu_name(name):
            continue
        vram_gb = float(g.get("gpu_dedicated_vram") or 0)
        cleaned.append(
            {
                "name": name,
                "utilization": 0.0,
                "memory_used_mb": 0.0,
                "memory_total_mb": round(vram_gb * 1024, 2),
                "vendor": _infer_vendor(name),
            }
        )
    return {"gpus": cleaned}


def _get_npu_info() -> Dict[str, Any]:
    """Best-effort Linux NPU detection (often limited vs Windows)."""
    brand = _cpu_brand().lower()
    npu_tops = 0
    if "ryzen ai max" in brand or "ai max" in brand:
        npu_tops = 50
    elif "ultra" in brand:
        npu_tops = 47 if re.search(r"ultra\s*2", brand) else 11

    name = None
    if shutil.which("lspci"):
        code, out, _ = _run(["lspci", "-nn"], timeout=5)
        if code == 0:
            for line in out.splitlines():
                low = line.lower()
                if any(x in low for x in ("npu", "xdna", "neural", "ai boost", "compute accelerator")):
                    if "microsoft" in low:
                        continue
                    name = line.split(":", 2)[-1].strip() if line.count(":") >= 2 else line.strip()
                    break

    # sysfs DRM / accel nodes sometimes expose NPUs
    if not name:
        for base in (Path("/sys/class/accel"), Path("/dev")):
            if not base.exists():
                continue
            for p in base.iterdir():
                n = p.name.lower()
                if "accel" in n or "npu" in n or "xdna" in n:
                    name = "NPU Compute Accelerator Device"
                    break
            if name:
                break

    if not name:
        return {}

    driver = "Unknown"
    if "amd" in brand or "ryzen" in brand or (name and "amd" in name.lower()):
        driver = "AMD XDNA Driver"
    elif "intel" in brand or (name and "intel" in name.lower()):
        driver = "Intel(R) AI Boost Driver"

    return {
        "npu_model": name,
        "npu_driver": driver,
        "npu_driver_version": "Unknown",
        "npu_max_tops": npu_tops or None,
    }


def _get_npu_usage() -> Dict[str, Any]:
    info = _get_npu_info()
    model = info.get("npu_model") if info else None
    # Linux rarely exposes a stable public counter; return 0 when present.
    return {"npu_utilization": 0.0, "npu_model": model}


def _get_os_info() -> Dict[str, Any]:
    rel = _read_os_release()
    return {
        "os_name": rel.get("NAME") or platform.system(),
        "os_version": rel.get("VERSION") or platform.version(),
        "os_pretty_name": rel.get("PRETTY_NAME") or platform.platform(),
        "os_id": rel.get("ID") or "",
        "kernel": platform.release(),
        "architecture": platform.machine(),
    }


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "anro-llm-system-info", "platform": "linux"}


@app.get("/api/version")
async def get_version():
    return {"version": __version__, "service": "linux-host-service", "platform": "linux"}


@app.get("/api/cpu/usage")
async def get_cpu_usage():
    return _get_cpu_usage()


@app.get("/api/gpu")
async def get_gpu():
    return _get_cached("gpu_info", _get_gpu_info)


@app.get("/api/gpu/usage")
async def get_gpu_usage():
    return _get_cached("gpu_usage", _get_gpu_usage)


@app.get("/api/npu")
async def get_npu():
    return _get_cached("npu_info", _get_npu_info)


@app.get("/api/npu/usage")
async def get_npu_usage():
    return _get_cached("npu_usage", _get_npu_usage)


@app.get("/api/memory")
async def get_memory():
    return _get_cached("memory_info", _get_memory_info)


@app.get("/api/chassis")
async def get_chassis():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _get_cached("chassis_info", _get_chassis_info)
    )


@app.get("/api/power")
async def get_power():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _get_cached("power_info", _get_power_info)
    )


@app.get("/api/memory/usage")
async def get_memory_usage():
    return _get_memory_usage()


@app.get("/api/storage")
async def get_storage():
    return _get_cached("storage_info", _get_storage_info)


@app.get("/api/system-specs")
async def get_system_specs():
    loop = asyncio.get_running_loop()
    (
        gpu_info,
        gpu_usage,
        npu_info,
        npu_usage,
        memory_info,
        storage_info,
        os_info,
        chassis_info,
        power_info,
    ) = await asyncio.gather(
        loop.run_in_executor(None, lambda: _get_cached("gpu_info", _get_gpu_info)),
        loop.run_in_executor(None, lambda: _get_cached("gpu_usage", _get_gpu_usage)),
        loop.run_in_executor(None, lambda: _get_cached("npu_info", _get_npu_info)),
        loop.run_in_executor(None, lambda: _get_cached("npu_usage", _get_npu_usage)),
        loop.run_in_executor(None, lambda: _get_cached("memory_info", _get_memory_info)),
        loop.run_in_executor(None, lambda: _get_cached("storage_info", _get_storage_info)),
        loop.run_in_executor(None, _get_os_info),
        loop.run_in_executor(None, lambda: _get_cached("chassis_info", _get_chassis_info)),
        loop.run_in_executor(None, lambda: _get_cached("power_info", _get_power_info)),
    )
    return {
        "gpu_info": gpu_info,
        "gpu_usage": gpu_usage,
        "npu_info": npu_info,
        "npu_usage": npu_usage,
        "memory_info": memory_info,
        "storage_info": storage_info,
        "os_info": os_info,
        "cpu_info": {"brand": _cpu_brand()},
        "chassis_info": chassis_info,
        "power_info": power_info,
    }


@app.get("/api/metrics/bundle")
async def get_metrics_bundle():
    loop = asyncio.get_running_loop()
    (
        cpu_usage,
        memory_usage,
        gpu_usage,
        gpu_info,
        npu_info,
        npu_usage,
        memory_info,
        storage_info,
        os_info,
        chassis_info,
        power_info,
    ) = await asyncio.gather(
        loop.run_in_executor(None, _get_cpu_usage),
        loop.run_in_executor(None, _get_memory_usage),
        loop.run_in_executor(None, lambda: _get_cached("gpu_usage", _get_gpu_usage)),
        loop.run_in_executor(None, lambda: _get_cached("gpu_info", _get_gpu_info)),
        loop.run_in_executor(None, lambda: _get_cached("npu_info", _get_npu_info)),
        loop.run_in_executor(None, lambda: _get_cached("npu_usage", _get_npu_usage)),
        loop.run_in_executor(None, lambda: _get_cached("memory_info", _get_memory_info)),
        loop.run_in_executor(None, lambda: _get_cached("storage_info", _get_storage_info)),
        loop.run_in_executor(None, _get_os_info),
        loop.run_in_executor(None, lambda: _get_cached("chassis_info", _get_chassis_info)),
        loop.run_in_executor(None, lambda: _get_cached("power_info", _get_power_info)),
    )
    system_specs = {
        "gpu_info": gpu_info,
        "gpu_usage": gpu_usage,
        "npu_info": npu_info,
        "npu_usage": npu_usage,
        "memory_info": memory_info,
        "storage_info": storage_info,
        "os_info": os_info,
        "cpu_info": {"brand": cpu_usage.get("brand") or _cpu_brand()},
        "chassis_info": chassis_info,
        "power_info": power_info,
    }
    return {
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "system_specs": system_specs,
        "gpu_usage": gpu_usage,
    }


def main():
    parser = argparse.ArgumentParser(description="Linux Host System Info Service")
    parser.add_argument("--port", type=int, default=8088, help="Port (default: 8088)")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0 for Docker host.docker.internal access)",
    )
    args = parser.parse_args()

    if platform.system().lower() == "windows":
        print(
            "WARNING: linux_host_service.py is intended for Linux hosts. "
            "On Windows use scripts/windows_host_service.py.",
            file=sys.stderr,
        )

    print("=" * 60)
    print(f"Anro LLM Linux System Information Service v{__version__}")
    print("=" * 60)
    print(f"Starting on {args.host}:{args.port}")
    print(
        "Endpoints: /health /api/version /api/chassis /api/power "
        "/api/metrics/bundle /api/system-specs ..."
    )
    print("=" * 60)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
