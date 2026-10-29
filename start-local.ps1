[CmdletBinding()]
param([switch]$StatusOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create .venv and install backend/requirements.txt before starting the local services.'
}

$services = @(
    @{ Name = 'web'; Module = 'uvicorn backend.main:app'; Args = @('-X','utf8','-u','-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000') },
    @{ Name = 'collector'; Module = 'backend.collector.collection_worker'; Args = @('-X','utf8','-u','-m','backend.collector.collection_worker') },
    @{ Name = 'models'; Module = 'backend.collector.model_worker'; Args = @('-X','utf8','-u','-m','backend.collector.model_worker') }
)

# Match this checkout's venv launchers only, not unrelated Python applications.
function Find-LocalProcess($service) {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match [regex]::Escape($pythonPath) -and
        $_.CommandLine -match ([regex]::Escape($service.Module) + '(\s|$)')
    }
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not $StatusOnly) {
        $logDir = Join-Path $projectRoot 'data\logs'
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        foreach ($service in $services) {
            $existing = @(Find-LocalProcess $service)
            if ($existing.Count) {
                Write-Host "$($service.Name): already running (launcher PID $($existing.ProcessId -join ', '))"
                continue
            }
            $prefix = Join-Path $logDir "$($service.Name)-$stamp"
            $started = Start-Process -FilePath $pythonPath -ArgumentList $service.Args -WorkingDirectory $projectRoot `
                -WindowStyle Hidden -RedirectStandardOutput "$prefix.stdout.log" -RedirectStandardError "$prefix.stderr.log" -PassThru
            Write-Host "$($service.Name): starting (launcher PID $($started.Id)); logs: $prefix.*.log"
        }
    }
    $probe = @'
import json
from dotenv import load_dotenv
load_dotenv('.env')
from backend.database import SessionLocal
from backend.runtime import worker_status
with SessionLocal() as db:
    print(json.dumps(worker_status(db)))
'@
    $deadline = (Get-Date).AddSeconds(40)
    do {
        $workers = @(& $pythonPath -X utf8 -c $probe | ConvertFrom-Json)
        if ($LASTEXITCODE -ne 0) { throw 'Could not read worker health from the project database.' }
        $webReady = $false
        try { $webReady = (Invoke-RestMethod 'http://127.0.0.1:8000/health/ready' -TimeoutSec 2).status -eq 'ok' } catch { }
        $missingProcesses = @($services | Where-Object { -not @(Find-LocalProcess $_).Count })
        $healthy = $webReady -and -not $missingProcesses.Count -and @($workers | Where-Object { -not $_.online }).Count -eq 0
        if ($healthy -or $StatusOnly -or (Get-Date) -ge $deadline) { break }
        Start-Sleep -Seconds 2
    } while ($true)
    Write-Host "web: ready=$webReady (http://127.0.0.1:8000/)"
    $workers | Select-Object role, online, state, @{Name='heartbeat_Beijing'; Expression={
        if ($_.last_seen) { ([DateTimeOffset]$_.last_seen).ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd HH:mm:ss') }
    }} | Format-Table -AutoSize
    if (-not $healthy) {
        throw 'Not all three local services are healthy. Check their logs and the administrator runtime page.'
    }
    Write-Host 'All three services are online. Daily collection starts at 10:00 Asia/Shanghai while the computer is awake.'
} finally {
    Pop-Location
}
