$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot


# ============================================================
# Load .env if present
# ============================================================

$EnvFile = Join-Path $PSScriptRoot ".env"

if (Test-Path $EnvFile) {
    Write-Host "Loading configuration from .env"

    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()

        # Ignore blank lines and comments
        if (-not $line -or $line.StartsWith("#")) {
            return
        }

        # Split only on the first =
        $parts = $line -split "=", 2

        if ($parts.Count -ne 2) {
            return
        }

        $name  = $parts[0].Trim()
        $value = $parts[1].Trim()

        # Strip matching surrounding quotes
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable(
            $name,
            $value,
            [EnvironmentVariableTarget]::Process
        )
    }
}
else {
    Write-Host "No .env found; using defaults/environment variables."
}


# ============================================================
# Defaults
# Existing environment/.env values win
# ============================================================

if (-not $env:FFMPEG_PRESET) {
    $env:FFMPEG_PRESET = "veryfast"
}

if (-not $env:TZ) {
    $env:TZ = "America/Chicago"
}

$env:PYTHONUNBUFFERED = "1"


# ============================================================
# Required configuration
# ============================================================

if (-not $env:PLEX_URL) {
    throw "PLEX_URL is not configured. Set it in .env or the environment."
}

if (-not $env:PLEX_TOKEN) {
    throw "PLEX_TOKEN is not configured. Set it in .env or the environment."
}


# ============================================================
# Prerequisite checks
# ============================================================

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.11 first."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg was not found in PATH."
}

if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw "ffprobe was not found in PATH."
}


# ============================================================
# Python environment
# ============================================================

$VenvDir = Join-Path $PSScriptRoot ".venv"
$Python  = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Host "Creating Python 3.11 virtual environment..."

    py -3.11 -m venv $VenvDir

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create Python virtual environment."
    }

    & $Python -m pip install --upgrade pip

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip."
    }

}

Write-Host "Installing Clipplex dependencies..."
& $Python -m pip install -r requirements.txt

if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Clipplex dependencies."
}

& $Python -c "import waitress" 2>$null

if ($LASTEXITCODE -ne 0) {
    throw "Waitress is not available after dependency installation."
}

# ============================================================
# Output directories
# ============================================================

$MediaRoot = Join-Path $PSScriptRoot "app\static\media"

New-Item -ItemType Directory -Force `
    (Join-Path $MediaRoot "videos") | Out-Null

New-Item -ItemType Directory -Force `
    (Join-Path $MediaRoot "images") | Out-Null

New-Item -ItemType Directory -Force `
    (Join-Path $MediaRoot "gifs") | Out-Null


# ============================================================
# Start Clipplex
# ============================================================

Write-Host ""
Write-Host "============================================================"
Write-Host " Clipplex"
Write-Host "============================================================"
Write-Host " URL:    http://localhost:9945"
Write-Host " Plex:   $($env:PLEX_URL)"
Write-Host " Preset: $($env:FFMPEG_PRESET)"
Write-Host "============================================================"
Write-Host ""

& $Python -m waitress `
    --host=0.0.0.0 `
    --port=9945 `
    --threads=4 `
    main:app

if ($LASTEXITCODE -ne 0) {
    throw "Clipplex exited with status $LASTEXITCODE."
}
