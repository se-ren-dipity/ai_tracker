[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$modulePrefix = '^(?:"[^"]+"|\S+)\s+(?:-X\s+utf8\s+)?(?:-u\s+)?-m\s+'
$services = @(
    @{ Name = 'web'; Module = 'uvicorn backend.main:app' },
    @{ Name = 'collector'; Module = 'backend.collector.collection_worker' },
    @{ Name = 'models'; Module = 'backend.collector.model_worker' }
)

foreach ($service in $services) {
    $processes = @(Get-CimInstance Win32_Process)
    # Match the actual executable, not a path mentioned in another program's arguments.
    $launchers = @($processes | Where-Object {
        $_.ExecutablePath -eq $pythonPath -and
        $_.CommandLine -match ($modulePrefix + [regex]::Escape($service.Module) + '(\s|$)')
    })
    if (-not $launchers.Count) {
        Write-Host "$($service.Name): not running"
        continue
    }

    foreach ($launcher in $launchers) {
        if (-not $PSCmdlet.ShouldProcess("$($service.Name) (launcher PID $($launcher.ProcessId))", 'Stop process tree')) {
            continue
        }

        # Windows venv launchers have a real Python child; verify both exit.
        $treeIds = @($launcher.ProcessId)
        $parentIds = $treeIds
        do {
            $children = @($processes | Where-Object {
                $_.ParentProcessId -in $parentIds -and $_.ProcessId -notin $treeIds
            })
            $parentIds = @($children | ForEach-Object { $_.ProcessId })
            $treeIds += $parentIds
        } while ($parentIds.Count)

        & "$env:SystemRoot\System32\taskkill.exe" /PID $launcher.ProcessId /T /F
        if ($LASTEXITCODE -ne 0) {
            throw "Could not stop $($service.Name) (launcher PID $($launcher.ProcessId))."
        }
        $remaining = @(Get-Process -Id $treeIds -ErrorAction SilentlyContinue)
        if ($remaining.Count) {
            $remaining | Wait-Process -Timeout 10 -ErrorAction Stop
        }
        Write-Host "$($service.Name): stopped (process tree PIDs $($treeIds -join ', '))"
    }
}

Write-Host 'Stop request complete.'
