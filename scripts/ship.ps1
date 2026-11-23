# Ship the changes Claude left in this folder: install, test, and only if every test passes,
# commit as Arush70 <ytfinex70@gmail.com> and push. Run from anywhere:
#
#   powershell -ExecutionPolicy Bypass -File "C:\Machine Learning projects\guild\scripts\ship.ps1"
#
# Files Claude cannot write directly arrive in _incoming\ and are moved into place here
# (_incoming\github\... goes to .github\...). The commit message comes from _incoming\COMMIT_MSG.txt when present.

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
$incoming = Join-Path $repo "_incoming"

function Say($text, $color = "Cyan") { Write-Host "`n== $text" -ForegroundColor $color }

# 1. files waiting in _incoming
$message = "update from Claude"
if (Test-Path $incoming) {
    $msgFile = Join-Path $incoming "COMMIT_MSG.txt"
    if (Test-Path $msgFile) { $message = (Get-Content $msgFile -Raw).Trim(); Remove-Item $msgFile }
    Get-ChildItem $incoming -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($incoming.Length + 1)
        # remote tools cannot write any path containing ".github", so those files arrive under "github\"
        if ($rel -like "github\*") { $rel = "." + $rel }
        $target = Join-Path $repo $rel
        New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
        Move-Item -Force $_.FullName $target
        Write-Host "moved into place: $($target.Substring($repo.Length + 1))"
    }
    Remove-Item -Recurse -Force $incoming
}

if (-not (git status --porcelain)) { Say "Nothing to ship: no changes." "Yellow"; exit 0 }
git status --short

# 2. install and test
Say "Installing"
python -m pip install -e ".[dev]" -q --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Say "pip install failed. Nothing was committed." "Red"; exit 1 }
Say "Running the tests (a few minutes)"
python -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) {
    Say "Some tests failed. Nothing was committed. Send Claude everything from FAILURES down." "Red"
    exit 1
}

# 3. commit as the owner, push
Say "Committing as Arush70 <ytfinex70@gmail.com>"
git add -A
git -c user.name="Arush70" -c user.email="ytfinex70@gmail.com" commit -q -m $message
if ($LASTEXITCODE -ne 0) { Say "git commit failed (see above)." "Red"; exit 1 }
git log -1 --format="%h %an <%ae> %s"
Say "Pushing"
git push
if ($LASTEXITCODE -ne 0) { Say "git push failed (see above). The commit is safe locally." "Red"; exit 1 }
Say "Done. CI: https://github.com/Arush70/guild/actions" "Green"
