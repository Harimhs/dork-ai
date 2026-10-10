# Dork — install / update / start on Windows.
#
#   .\run.ps1                 set everything up and start Dork
#   .\run.ps1 --autostart     ...and start Dork at every login
#   .\run.ps1 --scan          just read this machine and report
#   (all options: .\run.ps1 --help)      (or double-click run.bat)
#
# Makes sure Python 3.10+ is installed (via winget if needed), then hands over to scripts\setup.py, which scans
# the drives / RAM / GPU, downloads the models and the C++ engine, and starts Dork.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Find-Python {
    foreach ($c in @("py -3.12", "py -3.11", "py -3.10", "python", "python3")) {
        $parts = $c.Split(" ")
        try {
            $ok = & $parts[0] @($parts[1..9] | Where-Object { $_ }) -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
            if ($ok -eq "True") { return $parts }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "`n== Installing Python 3.11 (winget)"
    winget install --id Python.Python.3.11 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
    if (-not $py) { Write-Host "Python 3.10+ still not found. Install it from https://python.org and run again."; exit 1 }
}

$exe = $py[0]
$pyArgs = @($py[1..9] | Where-Object { $_ }) + @("scripts\setup.py") + $args
& $exe @pyArgs
exit $LASTEXITCODE
