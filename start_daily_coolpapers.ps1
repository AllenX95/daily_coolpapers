$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONDONTWRITEBYTECODE = "1"

$Url = "http://127.0.0.1:8765/"
$HealthUrl = "http://127.0.0.1:8765/api/health"
$LogDir = Join-Path $Root "logs"
$StartupLog = Join-Path $LogDir "startup.log"
$StdoutLog = Join-Path $LogDir "server.stdout.log"
$StderrLog = Join-Path $LogDir "server.stderr.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Content -Encoding UTF8 -Path $StartupLog -Value ("{0} startup begin" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))

function Write-StartupLog {
    param([string]$Message)
    $Line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Encoding UTF8 -Path $StartupLog -Value $Line
    Write-Host $Line
}

function Invoke-LocalGet {
    param(
        [string]$Target,
        [int]$TimeoutSec = 2
    )
    try {
        return Invoke-WebRequest -UseBasicParsing -Uri $Target -TimeoutSec $TimeoutSec
    }
    catch {
        return $null
    }
}

function Test-DailyCoolPapersHealth {
    $Response = Invoke-LocalGet -Target $HealthUrl -TimeoutSec 2
    if (-not $Response -or $Response.StatusCode -ne 200) {
        return $false
    }
    try {
        $Payload = $Response.Content | ConvertFrom-Json
        return ($Payload.ok -eq $true -and $Payload.service -eq "daily-coolpapers")
    }
    catch {
        return $false
    }
}

function Test-DailyCoolPapersHome {
    $Response = Invoke-LocalGet -Target $Url -TimeoutSec 4
    return ($Response -and $Response.StatusCode -eq 200 -and $Response.Content -like "*Daily Cool Papers*")
}

function Open-DailyCoolPapers {
    try {
        Start-Process $Url
    }
    catch {
        Write-StartupLog ("browser open failed: {0}" -f $_.Exception.Message)
    }
}

function Stop-OldDailyCoolPapers {
    if (-not (Test-DailyCoolPapersHome)) {
        return $true
    }

    Write-StartupLog "old service detected without health endpoint; requesting shutdown"
    try {
        Invoke-WebRequest -UseBasicParsing -Method Post -Uri ($Url + "api/shutdown") -TimeoutSec 3 | Out-Null
    }
    catch {
        Write-StartupLog ("shutdown request failed: {0}" -f $_.Exception.Message)
    }

    $Deadline = (Get-Date).AddSeconds(12)
    while ((Get-Date) -lt $Deadline) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-DailyCoolPapersHome)) {
            Write-StartupLog "old service stopped"
            return $true
        }
    }

    Write-StartupLog "old service is still responding; leaving it untouched"
    return $false
}

if (Test-DailyCoolPapersHealth) {
    Write-StartupLog "service already running"
    Open-DailyCoolPapers
    exit 0
}

if (-not (Stop-OldDailyCoolPapers)) {
    Write-Host "Daily Cool Papers is already running, but it did not pass the new health check."
    Write-Host "Open logs\startup.log for details."
    Read-Host "Press Enter to close"
    exit 1
}

function Get-PythonCandidates {
    $Candidates = New-Object System.Collections.Generic.List[string]

    if ($env:DAILY_COOLPAPERS_PYTHON) {
        $Candidates.Add($env:DAILY_COOLPAPERS_PYTHON)
    }

    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $Candidates.Add($VenvPython)
    }

    $CondaPython = Join-Path $env:USERPROFILE "anaconda3\python.exe"
    if (Test-Path $CondaPython) {
        $Candidates.Add($CondaPython)
    }

    try {
        foreach ($Item in (& where.exe python 2>$null)) {
            if ($Item) {
                $Candidates.Add($Item)
            }
        }
    }
    catch {
    }

    $Seen = @{}
    foreach ($Candidate in $Candidates) {
        if (-not $Candidate) {
            continue
        }
        $Full = $Candidate.Trim('"')
        if (-not (Test-Path $Full)) {
            continue
        }
        $Key = $Full.ToLowerInvariant()
        if ($Seen.ContainsKey($Key)) {
            continue
        }
        $Seen[$Key] = $true
        $Full
    }
}

function Test-PythonCandidate {
    param([string]$Candidate)
    $CheckCode = "import flask, httpx, bs4; from daily_coolpapers.app import create_app; print('ok')"
    $Output = & $Candidate -B -c $CheckCode 2>&1
    if ($LASTEXITCODE -eq 0) {
        return $true
    }
    $Reason = ($Output | Select-Object -Last 1)
    Write-StartupLog ("python rejected: {0} :: {1}" -f $Candidate, $Reason)
    return $false
}

$Python = $null
foreach ($Candidate in Get-PythonCandidates) {
    if (Test-PythonCandidate $Candidate) {
        $Python = $Candidate
        break
    }
}

if (-not $Python) {
    Write-StartupLog "no usable Python found"
    Write-Host "No usable Python runtime was found. The runtime must import flask, httpx, bs4, and this project."
    Write-Host "Set DAILY_COOLPAPERS_PYTHON to the correct python.exe path, or install requirements into one Python."
    Read-Host "Press Enter to close"
    exit 1
}

Write-StartupLog ("python: {0}" -f $Python)
Set-Content -Encoding UTF8 -Path $StdoutLog -Value ""
Set-Content -Encoding UTF8 -Path $StderrLog -Value ""

$RunPy = Join-Path $Root "run.py"
$CmdLine = '""{0}" -B "{1}" 1>>"{2}" 2>>"{3}""' -f $Python, $RunPy, $StdoutLog, $StderrLog
$Process = Start-Process `
    -FilePath $env:ComSpec `
    -ArgumentList @("/d", "/c", $CmdLine) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru

Write-StartupLog ("started process id: {0}" -f $Process.Id)

$Deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $Deadline) {
    Start-Sleep -Milliseconds 500

    if ($Process.HasExited) {
        Write-StartupLog ("service exited early with code {0}" -f $Process.ExitCode)
        Write-Host "Daily Cool Papers service exited before it became ready."
        Write-Host "Startup log: $StartupLog"
        Write-Host "Stdout log: $StdoutLog"
        Write-Host "Stderr log: $StderrLog"
        Read-Host "Press Enter to close"
        exit 1
    }

    if (Test-DailyCoolPapersHealth) {
        Write-StartupLog "service ready"
        Open-DailyCoolPapers
        exit 0
    }
}

Write-StartupLog "service did not become ready within 60 seconds"
Write-Host "Daily Cool Papers service did not start within 60 seconds."
Write-Host "Startup log: $StartupLog"
Write-Host "Stdout log: $StdoutLog"
Write-Host "Stderr log: $StderrLog"
Read-Host "Press Enter to close"
exit 1
