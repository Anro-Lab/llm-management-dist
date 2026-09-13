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
from pathlib import Path
from typing import Dict, Any, Optional, List
import argparse
from functools import lru_cache
import asyncio
from threading import Lock

try:
    from fastapi import FastAPI, HTTPException, BackgroundTasks
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError:
    print("ERROR: Required packages not installed. Please install:")
    print("  pip install fastapi uvicorn")
    sys.exit(1)

# Keep in sync with src/__init__.py
__version__ = "0.12.12"

app = FastAPI(
    title="Windows Host System Info Service",
    description="Provides Windows system information (GPU, NPU, CPU, Memory) via HTTP API",
    version=__version__,
)

# Simple in-memory cache for expensive operations with background refresh
_cache = {}
_cache_lock = Lock()
_cache_ttl = {
    'gpu_usage': 2.0,  # Cache GPU usage for 2 seconds
    'npu_usage': 2.0,  # Cache NPU usage for 2 seconds
    'gpu_info': 30.0,  # Cache GPU info for 30 seconds (rarely changes)
    'npu_info': 30.0,  # Cache NPU info for 30 seconds (rarely changes)
    'memory_info': 2.0,  # Cache memory info for 2 seconds
    'storage_info': 5.0,  # Cache storage info for 5 seconds
}
_refresh_intervals = {
    'gpu_usage': 1.5,  # Refresh every 1.5 seconds in background
    'npu_usage': 1.5,  # Refresh every 1.5 seconds in background
    'memory_info': 1.5,  # Refresh every 1.5 seconds in background
}
_refresh_tasks = {}  # Track background refresh tasks
_refresh_last_time = {}  # Track last refresh time


def _get_cached(key: str, func, *args, **kwargs):
    """Get value from cache or compute and cache it."""
    now = time.time()
    
    with _cache_lock:
        if key in _cache:
            value, timestamp = _cache[key]
            ttl = _cache_ttl.get(key, 1.0)
            if now - timestamp < ttl:
                # Check if background refresh is needed
                refresh_interval = _refresh_intervals.get(key)
                if refresh_interval and (now - _refresh_last_time.get(key, 0)) >= refresh_interval:
                    # Trigger background refresh (non-blocking)
                    _trigger_background_refresh(key, func, *args, **kwargs)
                return value
    
    # Compute new value (cache miss or expired)
    value = func(*args, **kwargs)
    with _cache_lock:
        _cache[key] = (value, now)
        _refresh_last_time[key] = now
    return value


def _trigger_background_refresh(key: str, func, *args, **kwargs):
    """Trigger background refresh of cache entry."""
    now = time.time()
    last_refresh = _refresh_last_time.get(key, 0)
    refresh_interval = _refresh_intervals.get(key)
    
    if not refresh_interval or (now - last_refresh) < refresh_interval:
        return  # Too soon to refresh
    
    # Check if refresh task is already running
    if key in _refresh_tasks and not _refresh_tasks[key].done():
        return  # Refresh already in progress
    
    # Start background refresh task
    async def refresh_task():
        try:
            # Run in executor to avoid blocking
            loop = asyncio.get_event_loop()
            value = await loop.run_in_executor(None, func, *args, **kwargs)
            with _cache_lock:
                _cache[key] = (value, time.time())
                _refresh_last_time[key] = time.time()
        except Exception as e:
            # Log error but don't fail - use stale cache
            print(f"WARNING: Background refresh failed for {key}: {e}", file=sys.stderr)
    
    loop = asyncio.get_event_loop()
    if loop.is_running():
        task = asyncio.create_task(refresh_task())
        _refresh_tasks[key] = task
    else:
        # If event loop not running, run synchronously (shouldn't happen in FastAPI)
        value = func(*args, **kwargs)
        with _cache_lock:
            _cache[key] = (value, time.time())
            _refresh_last_time[key] = time.time()


async def _background_refresh_loop():
    """Background task to continuously refresh cache entries."""
    while True:
        try:
            await asyncio.sleep(1.0)  # Check every second
            
            now = time.time()
            with _cache_lock:
                cache_keys = list(_cache.keys())
            
            for key in cache_keys:
                refresh_interval = _refresh_intervals.get(key)
                if not refresh_interval:
                    continue
                
                last_refresh = _refresh_last_time.get(key, 0)
                if (now - last_refresh) >= refresh_interval:
                    # Determine which function to call
                    func_map = {
                        'gpu_usage': _get_gpu_usage,
                        'npu_usage': _get_npu_usage,
                        'memory_info': _get_memory_info,
                    }
                    
                    func = func_map.get(key)
                    if func:
                        _trigger_background_refresh(key, func)
        except Exception as e:
            print(f"WARNING: Background refresh loop error: {e}", file=sys.stderr)
            await asyncio.sleep(5.0)  # Wait longer on error


@app.on_event("startup")
async def startup_event():
    """Start background refresh task on startup."""
    # Warm up cache with initial values
    try:
        _get_cached('gpu_info', _get_gpu_info)
        _get_cached('npu_info', _get_npu_info)
    except Exception as e:
        print(f"WARNING: Cache warm-up failed: {e}", file=sys.stderr)
    
    # Start background refresh loop
    asyncio.create_task(_background_refresh_loop())


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
    """Get memory usage information from Windows via PowerShell."""
    usage_script = '''
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $totalBytes = $os.TotalVisibleMemorySize * 1024
        $freeBytes = $os.FreePhysicalMemory * 1024
        $usedBytes = $totalBytes - $freeBytes
        
        [PSCustomObject]@{
            Total = $totalBytes
            Used = $usedBytes
            Available = $freeBytes
        } | ConvertTo-Json -Depth 3
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(usage_script, timeout=5)
    
    if result['success'] and result['output']:
        try:
            usage_data = json.loads(result['output'].strip())
            return {
                "total": usage_data.get('Total', 0),
                "used": usage_data.get('Used', 0),
                "available": usage_data.get('Available', 0)
            }
        except (json.JSONDecodeError, ValueError):
            pass
    
    return {
        "total": 0,
        "used": 0,
        "available": 0
    }


def _get_cpu_usage() -> Dict[str, Any]:
    """Get CPU usage — prefer % Processor Time (closer to Task Manager on modern AMD)."""
    usage_script = '''
    try {
        $loadPercentage = $null

        # Prefer Processor Time with a 1s two-sample window (first Get-Counter hit is often junk).
        # On Ryzen AI / frequency-scaled CPUs, % Processor Utility can sit near/above 100 while
        # Task Manager shows a lower overall %, so Time matches the UI users compare against.
        $timeCounter = Get-Counter -Counter "\\Processor(_Total)\\% Processor Time" -SampleInterval 1 -MaxSamples 2 -ErrorAction SilentlyContinue
        if ($timeCounter -and $timeCounter.CounterSamples -and $timeCounter.CounterSamples.Count -gt 0) {
            $loadPercentage = [math]::Round([double]$timeCounter.CounterSamples[-1].CookedValue, 1)
        }

        if ($null -eq $loadPercentage) {
            $cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($cpu -and $null -ne $cpu.LoadPercentage) {
                $loadPercentage = [double]$cpu.LoadPercentage
            }
        }

        if ($null -eq $loadPercentage) {
            $utility = Get-Counter -Counter "\\Processor Information(_Total)\\% Processor Utility" -SampleInterval 1 -MaxSamples 2 -ErrorAction SilentlyContinue
            if ($utility -and $utility.CounterSamples -and $utility.CounterSamples.Count -gt 0) {
                $loadPercentage = [math]::Round([double]$utility.CounterSamples[-1].CookedValue, 1)
            }
        }

        if ($null -eq $loadPercentage) { $loadPercentage = 0.0 }
        if ($loadPercentage -lt 0) { $loadPercentage = 0.0 }
        if ($loadPercentage -gt 100) { $loadPercentage = 100.0 }

        $cores = @(Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue)
        $totalCores = ($cores | Measure-Object -Property NumberOfCores -Sum).Sum
        $logicalCores = ($cores | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
        $cpu0 = $cores | Select-Object -First 1
        $frequency = 0
        if ($cpu0) {
            $frequency = $cpu0.CurrentClockSpeed
            if ($null -eq $frequency -or $frequency -eq 0) { $frequency = $cpu0.MaxClockSpeed }
        }

        [PSCustomObject]@{
            Utilization = $loadPercentage
            Frequency = $frequency
            PhysicalCores = $totalCores
            LogicalCores = $logicalCores
        } | ConvertTo-Json -Depth 3
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(usage_script, timeout=12)
    
    if result['success'] and result['output']:
        try:
            usage_data = json.loads(result['output'].strip())
            return {
                "utilization": usage_data.get('Utilization', 0.0),
                "frequency": usage_data.get('Frequency', 0),
                "physical_cores": usage_data.get('PhysicalCores', 0),
                "logical_cores": usage_data.get('LogicalCores', 0)
            }
        except (json.JSONDecodeError, ValueError):
            pass
    
    return {
        "utilization": 0.0,
        "frequency": 0,
        "physical_cores": 0,
        "logical_cores": 0
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "anro-llm-system-info"}


@app.get("/api/version")
async def get_version():
    """Get service version (aligned with llm-management app release)."""
    return {"version": __version__, "service": "windows-host-service"}


@app.get("/api/cpu/usage")
async def get_cpu_usage():
    """Get CPU usage information."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_cpu_usage)


def _get_gpu_usage() -> Dict[str, Any]:
    """Get GPU usage information (utilization, memory usage) from Windows Performance Counters.
    
    Supports both NVIDIA and AMD GPUs with optimized batch counter queries.
    """
    usage_script = '''
    try {
        # Get all valid GPUs
        $gpus = Get-CimInstance Win32_VideoController -ErrorAction Stop | Where-Object { 
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
        }
        
        if (-not $gpus) {
            Write-Output "[]"
            exit 0
        }
        
        # Build GPU list with vendor detection and adapter info
        $gpuList = @()
        foreach ($gpu in $gpus) {
            $gpuName = $gpu.Name
            $adapterRAM = $gpu.AdapterRAM
            if ($adapterRAM -lt 0) {
                $adapterRAM = [uint64]($adapterRAM + [Math]::Pow(2, 64))
            }
            
            # Try to get accurate VRAM from registry (same method as _get_gpu_info)
            $vramBytes = $null
            try {
                $regPath = "HKLM:\\SYSTEM\\ControlSet001\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\*"
                $regGpus = Get-ItemProperty -Path $regPath -Name "HardwareInformation.QwMemorySize","DriverDesc" -ErrorAction SilentlyContinue
                foreach ($regGpu in $regGpus) {
                    if ($regGpu.DriverDesc -and $regGpu."HardwareInformation.QwMemorySize") {
                        $desc = $regGpu.DriverDesc.ToLower()
                        $name = $gpuName.ToLower()
                        if ($desc -like "*$name*" -or $name -like "*$desc*") {
                            $vramBytes = $regGpu."HardwareInformation.QwMemorySize"
                            break
                        }
                    }
                }
            } catch {}
            
            if ($vramBytes) {
                $adapterRAM = $vramBytes
            }
            
            # Detect vendor
            $vendor = "Unknown"
            $nameLower = $gpuName.ToLower()
            if ($nameLower -like "*nvidia*" -or $nameLower -like "*geforce*" -or $nameLower -like "*rtx*" -or $nameLower -like "*gtx*" -or $nameLower -like "*quadro*" -or $nameLower -like "*tesla*") {
                $vendor = "NVIDIA"
            } elseif ($nameLower -like "*amd*" -or $nameLower -like "*radeon*" -or $nameLower -like "*rx*" -or $nameLower -like "*rdna*" -or $nameLower -like "*vega*") {
                $vendor = "AMD"
            } elseif ($nameLower -like "*intel*" -or $nameLower -like "*arc*" -or $nameLower -like "*iris*" -or $nameLower -like "*uhd graphics*") {
                $vendor = "Intel"
            }
            
            $gpuList += [PSCustomObject]@{
                Name = $gpuName
                AdapterRAM = $adapterRAM
                Vendor = $vendor
                Index = $gpu.Index
                PNPDeviceID = $gpu.PNPDeviceID
            }
        }
        
        # Build combined counter paths for batch query (optimized - single call)
        $counterPaths = @()
        
        # Common counters for both vendors
        $counterPaths += "\\GPU Engine(*)\\Utilization Percentage"
        
        # Use GPU Adapter Memory counters (most accurate - gives total adapter memory usage)
        $counterPaths += "\\GPU Adapter Memory(*)\\Dedicated Usage"
        $counterPaths += "\\GPU Adapter Memory(*)\\Shared Usage"
        
        # Fallback: try other adapter counter formats
        $counterPaths += "\\GPU Adapter(*)\\Dedicated Usage"
        $counterPaths += "\\GPU Adapter(*)\\Shared Usage"
        
        # Last resort: process-level counters (need to sum per adapter, less accurate)
        $counterPaths += "\\GPU Process Memory(*)\\Dedicated Usage"
        $counterPaths += "\\GPU Process Memory(*)\\Shared Usage"
        
        # Batch query all counters at once.
        # Keep MaxSamples 1 for latency: dual-GPU laptops can have thousands of
        # GPU Engine instances; MaxSamples 2 made /api/gpu/usage multi-second/hang.
        # LUID assignment (below) is what fixes stuck-at-0%, not the second sample.
        $allCounters = $null
                try {
            $allCounters = Get-Counter -Counter $counterPaths -SampleInterval 1 -MaxSamples 1 -ErrorAction SilentlyContinue
        } catch {
            # Performance counters may not be available
        }
        
        function Get-LuidKeyFromInstance([string]$instanceName) {
            if ($instanceName -match 'luid_0x([0-9a-f]+)_0x([0-9a-f]+)') {
                return ($matches[1] + '_' + $matches[2]).ToLower()
            }
            return $null
        }
        function Set-MapMax([hashtable]$map, [string]$key, [double]$val) {
            if (-not $map.ContainsKey($key) -or $val -gt $map[$key]) {
                $map[$key] = $val
            }
        }
        function Set-MapMaxLong([hashtable]$map, [string]$key, [long]$val) {
            if (-not $map.ContainsKey($key) -or $val -gt $map[$key]) {
                $map[$key] = $val
            }
        }

        # Process counter results (prefer LUID keys — each GPU may use phys_0)
        $utilizationMap = @{}
        $memoryDedicatedMap = @{}
        $memorySharedMap = @{}
        $allMemoryValues = @()
        
        if ($allCounters -and $allCounters.CounterSamples) {
            foreach ($sample in $allCounters.CounterSamples) {
                if ($sample.CookedValue -eq $null) { continue }
                
                $counterValue = [double]$sample.CookedValue
                $instanceName = $sample.InstanceName
                $counterPath = $sample.Path
                $luidKey = Get-LuidKeyFromInstance $instanceName
                                
                # Extract adapter index from instance name (single-GPU fallback only)
                $adapterIdx = $null
                if ($instanceName -match "phys_(\\d+)") {
                    $adapterIdx = [int]$matches[1]
                } elseif ($instanceName -match "adapter_(\\d+)") {
                    $adapterIdx = [int]$matches[1]
                } elseif ($instanceName -match "^(\\d+)") {
                    $adapterIdx = [int]$matches[1]
                }
                $mapKey = if ($luidKey) { $luidKey } elseif ($adapterIdx -ne $null) { "idx_$adapterIdx" } else { $null }
                
                # Process utilization counters
                if ($counterPath -like "*Utilization Percentage*") {
                    if ($mapKey) {
                        Set-MapMax $utilizationMap $mapKey $counterValue
                        # Also index by phys_N so single-GPU / Index fallback can match
                        # when Win32_VideoController.Index is empty (common on AMD CIM).
                        if ($luidKey -and $adapterIdx -ne $null) {
                            Set-MapMax $utilizationMap "idx_$adapterIdx" $counterValue
                        }
                    } else {
                        if (-not $utilizationMap.ContainsKey("global")) {
                            $utilizationMap["global"] = 0.0
                        }
                        if ($counterValue -gt $utilizationMap["global"]) {
                            $utilizationMap["global"] = $counterValue
                        }
                    }
                    # Always track a global max for single-GPU fallback
                    if (-not $utilizationMap.ContainsKey("global") -or $counterValue -gt $utilizationMap["global"]) {
                        $utilizationMap["global"] = $counterValue
                    }
                }
                
                # Process memory counters (in bytes, convert to MB)
                # Prefer GPU Adapter Memory counters (most accurate - total usage directly)
                if ($counterPath -like "*Adapter Memory*Dedicated Usage*") {
                    $memAdapterIdx = $null
                    if ($adapterIdx -ne $null) {
                        $memAdapterIdx = $adapterIdx
                    } elseif ($instanceName -match "adapter_(\\d+)") {
                        $memAdapterIdx = [int]$matches[1]
                    } elseif ($instanceName -match "phys_(\\d+)") {
                        $memAdapterIdx = [int]$matches[1]
                    } elseif ($instanceName -match "^(\\d+)") {
                        $memAdapterIdx = [int]$matches[1]
                    }
                    
                    if ($luidKey) {
                        Set-MapMaxLong $memoryDedicatedMap $luidKey ([long]$counterValue)
                    } elseif ($memAdapterIdx -ne $null) {
                        $idxKey = "idx_$memAdapterIdx"
                        Set-MapMaxLong $memoryDedicatedMap $idxKey ([long]$counterValue)
                    }
                    $allMemoryValues += [PSCustomObject]@{
                        Value = [long]$counterValue
                        InstanceName = $instanceName
                        AdapterIdx = $memAdapterIdx
                        LuidKey = $luidKey
                    }
                } elseif ($counterPath -like "*Adapter*Dedicated Usage*" -and $counterPath -notlike "*Process*") {
                    if ($mapKey) {
                        Set-MapMaxLong $memoryDedicatedMap $mapKey ([long]$counterValue)
                    }
                } elseif ($counterPath -like "*Process Memory*Dedicated Usage*") {
                    # Fallback: process-level counter - need to sum per adapter carefully
                    # Process memory instance format can be: "pid_adapter_X" or "processname_adapter_X" or just "adapter_X"
                    $procAdapterIdx = $null
                    
                    # Try to extract adapter index from instance name
                    if ($instanceName -match "adapter_(\d+)") {
                        $procAdapterIdx = [int]$matches[1]
                    } elseif ($instanceName -match "phys_(\d+)") {
                        $procAdapterIdx = [int]$matches[1]
                    } elseif ($instanceName -match "^(\d+)") {
                        # Might be just adapter index
                        $procAdapterIdx = [int]$matches[1]
                    }
                    
                    # If we found an adapter index, sum the memory
                    if ($luidKey) {
                        if (-not $memoryDedicatedMap.ContainsKey($luidKey)) {
                            $memoryDedicatedMap[$luidKey] = 0
                        }
                        $memoryDedicatedMap[$luidKey] += [long]$counterValue
                    } elseif ($procAdapterIdx -ne $null) {
                        $idxKey = "idx_$procAdapterIdx"
                        if (-not $memoryDedicatedMap.ContainsKey($idxKey)) {
                            $memoryDedicatedMap[$idxKey] = 0
                        }
                        $memoryDedicatedMap[$idxKey] += [long]$counterValue
                    } elseif ($mapKey) {
                        if (-not $memoryDedicatedMap.ContainsKey($mapKey)) {
                            $memoryDedicatedMap[$mapKey] = 0
                        }
                        $memoryDedicatedMap[$mapKey] += [long]$counterValue
                    } elseif (-not $memoryDedicatedMap.ContainsKey("global")) {
                        $memoryDedicatedMap["global"] = 0
                    } else {
                        $memoryDedicatedMap["global"] += [long]$counterValue
                    }
                }
                
                if ($counterPath -like "*Adapter Memory*Shared Usage*") {
                    if ($mapKey) {
                        Set-MapMaxLong $memorySharedMap $mapKey ([long]$counterValue)
                    } elseif ($adapterIdx -ne $null) {
                        Set-MapMaxLong $memorySharedMap "idx_$adapterIdx" ([long]$counterValue)
                    }
                } elseif ($counterPath -like "*Adapter*Shared Usage*" -and $counterPath -notlike "*Process*") {
                    if ($mapKey) {
                        Set-MapMaxLong $memorySharedMap $mapKey ([long]$counterValue)
                    }
                } elseif ($counterPath -like "*Process Memory*Shared Usage*") {
                    if ($luidKey) {
                        if (-not $memorySharedMap.ContainsKey($luidKey)) {
                            $memorySharedMap[$luidKey] = 0
                        }
                        $memorySharedMap[$luidKey] += [long]$counterValue
                    } elseif ($instanceName -match "adapter_(\\d+)") {
                        $procAdapterIdx = [int]$matches[1]
                        $idxKey = "idx_$procAdapterIdx"
                        if (-not $memorySharedMap.ContainsKey($idxKey)) {
                            $memorySharedMap[$idxKey] = 0
                        }
                        $memorySharedMap[$idxKey] += [long]$counterValue
                    } elseif ($mapKey) {
                        if (-not $memorySharedMap.ContainsKey($mapKey)) {
                            $memorySharedMap[$mapKey] = 0
                        }
                        $memorySharedMap[$mapKey] += [long]$counterValue
                    }
                }
            }
        }
        
        function Get-UtilForKey([string]$key) {
            if ($utilizationMap.ContainsKey($key)) {
                return [math]::Round($utilizationMap[$key], 2)
            }
            return $null
        }
        function Get-MemMbForKey([string]$key) {
            $ded = [long]0
            $shr = [long]0
            $found = $false
            if ($memoryDedicatedMap.ContainsKey($key)) {
                $ded = [long]$memoryDedicatedMap[$key]
                $found = $true
            }
            if ($memorySharedMap.ContainsKey($key)) {
                $shr = [long]$memorySharedMap[$key]
                $found = $true
            }
            if ($found) {
                return [math]::Round(($ded + $shr) / 1MB, 2)
            }
            return $null
        }
        function Get-LuidKeysFromMaps() {
            @(
                $memoryDedicatedMap.Keys + $memorySharedMap.Keys + $utilizationMap.Keys |
                Where-Object { $_ -is [string] -and $_ -notlike "idx_*" -and $_ -ne "global" }
            ) | Select-Object -Unique
        }

        $singleGpu = ($gpuList.Count -eq 1)
        $luidKeys = @(Get-LuidKeysFromMaps)
        # Use LUID pairing for single-GPU too: AMD iGPU counters are LUID-keyed and
        # Win32_VideoController.Index is often empty, so idx_* lookup alone yields 0%.
        $useLuidAssignment = ($luidKeys.Count -gt 0)

        $gpuEntries = @()
        if ($useLuidAssignment) {
            $usedLuids = @{}
            $gpusSorted = @($gpuList | Sort-Object { [uint64]$_.AdapterRAM } -Descending)
            $luidsByMem = @($luidKeys | Sort-Object { Get-MemMbForKey $_ } -Descending)
            $assignments = @{}

            foreach ($gpuInfo in $gpusSorted) {
                $capMb = ([double]$gpuInfo.AdapterRAM / 1MB) * 1.1
                $picked = $null
                foreach ($lk in $luidsByMem) {
                    if ($usedLuids[$lk]) { continue }
                    $memMb = Get-MemMbForKey $lk
                    if ($memMb -le $capMb) {
                        $picked = $lk
                        break
                    }
                }
                if (-not $picked) {
                    foreach ($lk in ($luidKeys | Sort-Object { Get-MemMbForKey $_ })) {
                        if (-not $usedLuids[$lk]) { $picked = $lk; break }
                    }
                }
                if ($picked) {
                    $usedLuids[$picked] = $true
                    $u = Get-UtilForKey $picked
                    $m = Get-MemMbForKey $picked
                    $assignments[$gpuInfo.Name] = [PSCustomObject]@{
                        Utilization = if ($u -ne $null) { $u } else { 0.0 }
                        MemoryUsedMb = if ($m -ne $null) { $m } else { 0.0 }
                        LuidKey = $picked
                    }
                }
            }

            foreach ($gpuInfo in $gpuList) {
                $a = $assignments[$gpuInfo.Name]
                $gpuEntries += [PSCustomObject]@{
                    GpuInfo = $gpuInfo
                    Utilization = if ($a) { [double]$a.Utilization } else { 0.0 }
                    MemoryUsedMb = if ($a) { [double]$a.MemoryUsedMb } else { 0.0 }
                    AdapterIndex = if ($a) { $a.LuidKey } else { $null }
                }
            }
        } else {
            function Get-UtilForIdx([int[]]$idxList) {
                foreach ($idx in $idxList) {
                    $k = "idx_$idx"
                    $u = Get-UtilForKey $k
                    if ($u -ne $null) { return $u }
                    if ($utilizationMap.ContainsKey($idx)) {
                        return [math]::Round($utilizationMap[$idx], 2)
                    }
                }
                return $null
            }
            function Get-MemMbForIdx([int[]]$idxList) {
                foreach ($idx in $idxList) {
                    $m = Get-MemMbForKey "idx_$idx"
                    if ($m -ne $null) { return $m }
                }
                return $null
            }
            function Test-IdxHasData([int]$idx) {
                $k = "idx_$idx"
                return ($utilizationMap.ContainsKey($k) -or $memoryDedicatedMap.ContainsKey($k) -or $memorySharedMap.ContainsKey($k) -or
                    $utilizationMap.ContainsKey($idx) -or $memoryDedicatedMap.ContainsKey($idx))
            }

            $usedCounterKeys = @{}
            $enumIndex = 0
            foreach ($gpuInfo in $gpuList) {
                $idxCandidates = @()
                if ($gpuInfo.Index -ne $null) { $idxCandidates += [int]$gpuInfo.Index }
                if ($idxCandidates -notcontains $enumIndex) { $idxCandidates += $enumIndex }

                $matchedIdx = $null
                foreach ($c in $idxCandidates) {
                    if ($usedCounterKeys.ContainsKey($c)) { continue }
                    if (Test-IdxHasData $c) {
                        $matchedIdx = $c
                        break
                    }
                }

                $utilization = 0.0
                $memoryUsed = 0.0
                if ($matchedIdx -ne $null) {
                    $u = Get-UtilForIdx @($matchedIdx)
                    if ($u -ne $null) { $utilization = $u }
                    $m = Get-MemMbForIdx @($matchedIdx)
                    if ($m -ne $null) { $memoryUsed = $m }
                    $usedCounterKeys[$matchedIdx] = $true
                } elseif ($singleGpu -and $utilizationMap.ContainsKey("global")) {
                    $utilization = [math]::Round($utilizationMap["global"], 2)
                } elseif ($singleGpu -and $utilizationMap.Count -gt 0) {
                    $maxU = 0.0
                    foreach ($uk in $utilizationMap.Keys) {
                        if ([double]$utilizationMap[$uk] -gt $maxU) { $maxU = [double]$utilizationMap[$uk] }
                    }
                    $utilization = [math]::Round($maxU, 2)
                }

                $gpuEntries += [PSCustomObject]@{
                    GpuInfo = $gpuInfo
                    Utilization = $utilization
                    MemoryUsedMb = $memoryUsed
                    AdapterIndex = $matchedIdx
                    PhaseAMatched = ($matchedIdx -ne $null)
                }
                $enumIndex++
            }

            if (-not $singleGpu) {
                $unmatched = @($gpuEntries | Where-Object { -not $_.PhaseAMatched })
                $unusedKeys = @(
                    $memoryDedicatedMap.Keys + $memorySharedMap.Keys + $utilizationMap.Keys |
                    Where-Object { $_ -like "idx_*" -and -not $usedCounterKeys.ContainsKey([int]($_ -replace '^idx_','')) } |
                    Select-Object -Unique |
                    Sort-Object { [int]($_ -replace '^idx_','') }
                )
                if ($unmatched.Count -gt 0 -and $unusedKeys.Count -gt 0) {
                    $unmatchedSorted = $unmatched | Sort-Object { $_.GpuInfo.AdapterRAM } -Descending
                    $pairCount = [Math]::Min($unmatchedSorted.Count, $unusedKeys.Count)
                    for ($i = 0; $i -lt $pairCount; $i++) {
                        $entry = $unmatchedSorted[$i]
                        $key = $unusedKeys[$i]
                        $u = Get-UtilForKey $key
                        if ($u -ne $null) { $entry.Utilization = $u }
                        $m = Get-MemMbForKey $key
                        if ($m -ne $null) { $entry.MemoryUsedMb = $m }
                        $entry.AdapterIndex = $key
                    }
                }
            }
        }

        $result = @()
        foreach ($entry in $gpuEntries) {
            $gi = $entry.GpuInfo
            $memoryTotal = [math]::Round($gi.AdapterRAM / 1MB, 2)
            $memUsed = [double]$entry.MemoryUsedMb
            if ($memoryTotal -gt 0 -and $memUsed -gt ($memoryTotal * 1.05)) {
                $memUsed = 0.0
            }
            $result += [PSCustomObject]@{
                name = $gi.Name
                utilization = [double]$entry.Utilization
                memory_used_mb = $memUsed
                memory_total_mb = [double]$memoryTotal
                vendor = $gi.Vendor
                adapter_index = $entry.AdapterIndex
            }
        }
        
        $result | ConvertTo-Json -Depth 3
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(usage_script, timeout=15)
    
    if not result['success']:
        # Return empty list on error, don't fail completely
        return {'gpus': []}
    
    try:
        output = result['output'].strip()
        if not output or output == '[]' or output == 'null':
            return {'gpus': []}
        
        usage_data = json.loads(output)
        if not isinstance(usage_data, list):
            usage_data = [usage_data] if usage_data else []
        
        # Clean up and normalize the data
        cleaned_gpus = []
        for gpu in usage_data:
            utilization = gpu.get('utilization', gpu.get('Utilization', 0.0))
            if isinstance(utilization, dict):
                utilization = utilization.get('value', 0.0)
            elif not isinstance(utilization, (int, float)):
                utilization = 0.0
            
            cleaned_gpu = {
                'name': gpu.get('name', gpu.get('Name', 'Unknown GPU')),
                'utilization': float(utilization),
                'memory_used_mb': float(gpu.get('memory_used_mb', gpu.get('MemoryUsed', 0))),
                'memory_total_mb': float(gpu.get('memory_total_mb', gpu.get('MemoryTotal', 0))),
                'vendor': gpu.get('vendor', gpu.get('Vendor', 'Unknown')),
            }
            if gpu.get('adapter_index') is not None:
                cleaned_gpu['adapter_index'] = gpu.get('adapter_index')
            cleaned_gpus.append(cleaned_gpu)
        
        return {'gpus': cleaned_gpus}
    except json.JSONDecodeError as e:
        # Return empty list on parse error
        return {'gpus': []}


@app.get("/api/gpu")
async def get_gpu():
    """Get GPU information."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached('gpu_info', _get_gpu_info))


@app.get("/api/gpu/usage")
async def get_gpu_usage():
    """Get GPU usage information (utilization, memory usage)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached('gpu_usage', _get_gpu_usage))


def _get_npu_usage() -> Dict[str, Any]:
    """Get NPU utilization information from Windows Performance Counters.
    
    Supports AMD XDNA NPU via performance counters.
    """
    # First get NPU info to check if NPU exists
    npu_info = _get_npu_info()
    if not npu_info or not npu_info.get('npu_model'):
        return {
            'npu_utilization': 0.0,
            'npu_model': None
        }
    
    npu_model = npu_info.get('npu_model', '')
    
    usage_script = '''
    try {
        # Try multiple NPU counter paths (batch query for efficiency)
        $counterPaths = @(
            "\\NPU Engine(*)\\Utilization Percentage",
            "\\AMD XDNA Engine(*)\\Utilization Percentage",
            "\\Compute Accelerator(*)\\Utilization Percentage",
            "\\NPU(*)\\Utilization Percentage"
        )
        
        $utilization = 0.0
        $foundCounter = $false
        
        # Try to get NPU utilization from performance counters
        try {
            $counters = Get-Counter -Counter $counterPaths -SampleInterval 1 -MaxSamples 1 -ErrorAction SilentlyContinue
            if ($counters -and $counters.CounterSamples) {
                foreach ($sample in $counters.CounterSamples) {
                    if ($sample.CookedValue -ne $null) {
                        $counterValue = [double]$sample.CookedValue
                        # Take the maximum utilization across all NPU engines
                        if ($counterValue -gt $utilization) {
                            $utilization = $counterValue
                            $foundCounter = $true
                        }
                    }
                }
            }
        } catch {
            # Performance counters may not be available for NPU
        }
        
        # If no counter found, try alternative method
        if (-not $foundCounter) {
            # Try to get NPU device status (fallback)
            try {
                $npuDevice = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | Where-Object {
                    (($_.Name -like "*NPU*" -and $_.Name -like "*Compute Accelerator*") -or 
                     $_.Name -like "*XDNA*" -or 
                     ($_.Name -like "*AMD*" -and $_.Name -like "*Neural Processing*") -or
                     ($_.Name -like "*AMD*" -and $_.Name -like "*AI Engine*") -or
                     $_.Name -like "*Intel*AI Boost*") -and
                    $_.Status -eq "OK"
                } | Select-Object -First 1
                
                if ($npuDevice) {
                    # NPU is present but utilization not available via counters
                    # Return 0.0 to indicate NPU exists but utilization unavailable
                    $utilization = 0.0
                }
            } catch {
                # NPU device query failed
            }
        }
        
        [PSCustomObject]@{
            Utilization = [math]::Round($utilization, 2)
        } | ConvertTo-Json -Depth 3
    } catch {
        Write-Error $_.Exception.Message
        exit 1
    }
    '''
    
    result = _execute_powershell(usage_script, timeout=10)
    
    if not result['success']:
        return {
            'npu_utilization': 0.0,
            'npu_model': npu_model
        }
    
    try:
        output = result['output'].strip()
        if output:
            usage_data = json.loads(output)
            utilization = usage_data.get('Utilization', 0.0)
            if not isinstance(utilization, (int, float)):
                utilization = 0.0
            
            return {
                'npu_utilization': float(utilization),
                'npu_model': npu_model
            }
    except (json.JSONDecodeError, ValueError):
        pass
    
    return {
        'npu_utilization': 0.0,
        'npu_model': npu_model
    }


@app.get("/api/npu")
async def get_npu():
    """Get NPU information."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached('npu_info', _get_npu_info))


@app.get("/api/npu/usage")
async def get_npu_usage():
    """Get NPU utilization information."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _get_cached('npu_usage', _get_npu_usage))


@app.get("/api/memory")
async def get_memory():
    """Get memory information."""
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
    """Get memory usage information."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_memory_usage)


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


@app.get("/api/storage")
async def get_storage():
    """Get storage information."""
    return _get_storage_info()


@app.get("/api/system-specs")
async def get_system_specs():
    """Get full system specifications."""
    # Execute expensive operations in parallel using asyncio
    loop = asyncio.get_event_loop()
    
    # Run CPU-bound operations in thread pool to avoid blocking
    gpu_info_task = loop.run_in_executor(None, lambda: _get_cached('gpu_info', _get_gpu_info))
    gpu_usage_task = loop.run_in_executor(None, lambda: _get_cached('gpu_usage', _get_gpu_usage))
    npu_info_task = loop.run_in_executor(None, lambda: _get_cached('npu_info', _get_npu_info))
    npu_usage_task = loop.run_in_executor(None, lambda: _get_cached('npu_usage', _get_npu_usage))
    memory_info_task = loop.run_in_executor(None, lambda: _get_cached('memory_info', _get_memory_info))
    storage_info_task = loop.run_in_executor(None, lambda: _get_cached('storage_info', _get_storage_info))
    chassis_task = loop.run_in_executor(None, lambda: _get_cached("chassis_info", _get_chassis_info))
    power_task = loop.run_in_executor(None, lambda: _get_cached("power_info", _get_power_info))
    
    # Wait for all tasks to complete in parallel
    gpu_info, gpu_usage, npu_info, npu_usage, memory_info, storage_info, chassis_info, power_info = await asyncio.gather(
        gpu_info_task,
        gpu_usage_task,
        npu_info_task,
        npu_usage_task,
        memory_info_task,
        storage_info_task,
        chassis_task,
        power_task,
    )
    
    return {
        "gpu_info": gpu_info,
        "gpu_usage": gpu_usage,
        "npu_info": npu_info,
        "npu_usage": npu_usage,
        "memory_info": memory_info,
        "storage_info": storage_info,
        "chassis_info": chassis_info,
        "power_info": power_info,
    }


@app.get("/api/metrics/bundle")
async def get_metrics_bundle():
    """Single round-trip for llm-management /api/resources (CPU, RAM, GPU usage, system specs).

    Includes chassis_info / power_info for parity with linux_host_service.py.
    """
    loop = asyncio.get_event_loop()
    cpu_task = loop.run_in_executor(None, _get_cpu_usage)
    mem_task = loop.run_in_executor(None, _get_memory_usage)
    gpu_usage_task = loop.run_in_executor(None, lambda: _get_cached("gpu_usage", _get_gpu_usage))
    gpu_info_task = loop.run_in_executor(None, lambda: _get_cached("gpu_info", _get_gpu_info))
    npu_info_task = loop.run_in_executor(None, lambda: _get_cached("npu_info", _get_npu_info))
    npu_usage_task = loop.run_in_executor(None, lambda: _get_cached("npu_usage", _get_npu_usage))
    memory_info_task = loop.run_in_executor(None, lambda: _get_cached("memory_info", _get_memory_info))
    storage_info_task = loop.run_in_executor(None, lambda: _get_cached("storage_info", _get_storage_info))
    chassis_task = loop.run_in_executor(None, lambda: _get_cached("chassis_info", _get_chassis_info))
    power_task = loop.run_in_executor(None, lambda: _get_cached("power_info", _get_power_info))

    (
        cpu_usage,
        memory_usage,
        gpu_usage,
        gpu_info,
        npu_info,
        npu_usage,
        memory_info,
        storage_info,
        chassis_info,
        power_info,
    ) = await asyncio.gather(
        cpu_task,
        mem_task,
        gpu_usage_task,
        gpu_info_task,
        npu_info_task,
        npu_usage_task,
        memory_info_task,
        storage_info_task,
        chassis_task,
        power_task,
    )

    system_specs = {
        "gpu_info": gpu_info,
        "gpu_usage": gpu_usage,
        "npu_info": npu_info,
        "npu_usage": npu_usage,
        "memory_info": memory_info,
        "storage_info": storage_info,
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
