param(
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $RootDir ".runtime"
$PythonEnv = Join-Path $RuntimeDir "python"
$HermesEnv = Join-Path $RuntimeDir "hermes"
$UvEnv = Join-Path $RuntimeDir "uv"
$BinDir = Join-Path $RuntimeDir "bin"
$Checksums = Join-Path $RootDir "installers/tectonic-0.16.9.sha256"
$TectonicVersion = "0.16.9"

$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command python -ErrorAction SilentlyContinue) {
    $Candidate = (Get-Command python).Source
    & $Candidate -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -eq 0) {
        $PythonCommand = Get-Command python
    }
}
if (-not $PythonCommand -and (Get-Command py -ErrorAction SilentlyContinue)) {
    & (Get-Command py).Source -3.12 -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
    if ($LASTEXITCODE -eq 0) {
        $PythonCommand = Get-Command py
        $PythonPrefix = @("-3.12")
    }
}
if (-not $PythonCommand) {
    throw "Python 3.12 or newer is required."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js 20.9 or newer is required."
}
node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 20 || (major === 20 && minor >= 9) ? 0 : 1)'
if ($LASTEXITCODE -ne 0) {
    throw "Node.js 20.9 or newer is required."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm is required."
}

New-Item -ItemType Directory -Force -Path $RuntimeDir, $BinDir | Out-Null
& $PythonCommand.Source @PythonPrefix -m venv $PythonEnv
if ($LASTEXITCODE -ne 0) { throw "Could not create the Career Companion Python environment." }
$RuntimePython = Join-Path $PythonEnv "Scripts/python.exe"
$RuntimeCli = Join-Path $PythonEnv "Scripts/career-companion.exe"
$HermesPython = Join-Path $HermesEnv "Scripts/python.exe"
$HermesExecutable = Join-Path $HermesEnv "Scripts/hermes.exe"
& $PythonCommand.Source @PythonPrefix -m venv $UvEnv
if ($LASTEXITCODE -ne 0) { throw "Could not create the locked environment manager." }
$UvPython = Join-Path $UvEnv "Scripts/python.exe"
& $UvPython -m pip install --disable-pip-version-check "uv==0.11.6"
if ($LASTEXITCODE -ne 0) { throw "Could not install the locked environment manager." }
$env:UV_PROJECT_ENVIRONMENT = $PythonEnv
& (Join-Path $UvEnv "Scripts/uv.exe") sync --project $RootDir --frozen --no-dev --extra companion
if ($LASTEXITCODE -ne 0) { throw "Could not install locked Career Companion dependencies." }
& $PythonCommand.Source @PythonPrefix -m venv $HermesEnv
if ($LASTEXITCODE -ne 0) { throw "Could not create the Hermes environment." }
& $HermesPython -m pip install --disable-pip-version-check -r (Join-Path $RootDir "agent-profile/requirements-hermes.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install pinned Hermes." }

Push-Location (Join-Path $RootDir "frontend")
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "Could not install frontend dependencies." }
    $env:CAREERPILOT_STATIC_EXPORT = "true"
    $env:NEXT_PUBLIC_API_BASE_URL = ""
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Could not build the browser application." }
} finally {
    Pop-Location
}

& $RuntimePython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Could not install Playwright Chromium." }

$Architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
if ($Architecture -eq "Arm64") {
    Write-Warning "Windows ARM64 will use the signed x64 Tectonic build through Windows emulation."
} elseif ($Architecture -ne "X64") {
    throw "No supported native Tectonic package is available for Windows $Architecture. Use Docker."
}
$TectonicAsset = "tectonic-$TectonicVersion-x86_64-pc-windows-msvc.zip"
$ChecksumLine = Get-Content $Checksums | Where-Object { $_ -match [regex]::Escape($TectonicAsset) }
if (-not $ChecksumLine) { throw "Tectonic checksum manifest is incomplete." }
$ExpectedChecksum = ($ChecksumLine -split "\s+")[0].ToLowerInvariant()
$ArchivePath = Join-Path ([System.IO.Path]::GetTempPath()) "career-companion-tectonic-$([guid]::NewGuid()).zip"
try {
    $DownloadUrl = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.16.9/$TectonicAsset"
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ArchivePath -UseBasicParsing
    $ActualChecksum = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash.ToLowerInvariant()
    if ($ActualChecksum -ne $ExpectedChecksum) {
        throw "Tectonic checksum verification failed."
    }
    Expand-Archive -Path $ArchivePath -DestinationPath $BinDir -Force
} finally {
    Remove-Item $ArchivePath -Force -ErrorAction SilentlyContinue
}
$TectonicExecutable = Join-Path $BinDir "tectonic.exe"
if (-not (Test-Path $TectonicExecutable)) {
    $Extracted = Get-ChildItem $BinDir -Filter "tectonic.exe" -Recurse | Select-Object -First 1
    if (-not $Extracted) { throw "The verified Tectonic archive did not contain tectonic.exe." }
    Copy-Item $Extracted.FullName $TectonicExecutable -Force
}

$PlaywrightChecksum = (& $RuntimePython -c 'from career_companion.playwright_integrity import chromium_sha256; print(chromium_sha256()[1])').Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not record the Chromium integrity checksum." }
& $RuntimeCli setup `
    --hermes-executable $HermesExecutable `
    --tectonic-executable $TectonicExecutable `
    --playwright-checksum $PlaywrightChecksum
if ($LASTEXITCODE -ne 0) { throw "Career Companion setup failed." }
& $RuntimeCli doctor
if ($LASTEXITCODE -ne 0) { throw "Career Companion doctor found a required problem." }

Write-Host "Career Companion is installed."
Write-Host "Start it later with: $RuntimeCli start"
if (-not $NoStart) {
    & $RuntimeCli start
    exit $LASTEXITCODE
}
