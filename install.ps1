# EVA one-line bootstrap installer (Windows).
#
#   iex (irm https://raw.githubusercontent.com/arturkorb3/eva-evolutional-agent/main/install.ps1)
#
# What it does (and nothing more):
#   1. checks for git + docker
#   2. clones the repo to $env:EVA_HOME (default: $HOME\eva) - or pulls if already there
#   3. hands over to the repo's own `run.ps1 install` (PATH shim, tab-completion,
#      optional image build - all reversible via `eva uninstall`)
$ErrorActionPreference = "Stop"

$repoUrl = "https://github.com/arturkorb3/eva-evolutional-agent.git"
$dest = if ($env:EVA_HOME) { $env:EVA_HOME } else { Join-Path $HOME "eva" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required. Install it first (winget install Git.Git)."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Warning "docker not found - EVA runs sandboxed in Docker."
    Write-Warning "Install Docker Desktop (https://docs.docker.com/get-docker/) before first use."
}

if (Test-Path -LiteralPath (Join-Path $dest ".git")) {
    Write-Host "Existing checkout at $dest - updating (git pull)..."
    git -C $dest pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull failed - resolve manually in $dest." }
}
elseif ((Test-Path -LiteralPath $dest) -and @(Get-ChildItem -LiteralPath $dest -Force).Count -gt 0) {
    throw "$dest exists and is not an EVA checkout. Set `$env:EVA_HOME to another directory."
}
else {
    Write-Host "Cloning EVA into $dest ..."
    git clone --depth 1 $repoUrl $dest
    if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
}

& (Join-Path $dest "run.ps1") install

Write-Host ""
Write-Host "EVA is installed at: $dest" -ForegroundColor Green
Write-Host "Next: open a new terminal and run   eva"
