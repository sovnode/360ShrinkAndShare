# 360 Video Shrinker - builds the shareable install package (the GitHub release zip).
#   powershell -ExecutionPolicy Bypass -File package.ps1
# Output: dist\360 Video Shrinker <version>.zip (and the unzipped dist\build\ folder it was made from).
# Bundles a copy of this PC's Python 3.14 (python.org, signed python.exe) with tkinter, numpy and Pillow
# from the user's site-packages and PyAV from the pylib folder. Nothing is downloaded. No personal data:
# no log, presets, settings, install.json, samples, work files or photos (built-in presets only).
$ErrorActionPreference = 'Stop'
$App     = $PSScriptRoot
$Version = (Select-String -Path "$App\paths.py" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
$PyHome  = 'C:\Program Files\Python314'
$UserSite = "$env:APPDATA\Python\Python314\site-packages"
# PyAV: the "pylib" folder named in this checkout's install.json (not committed), else the default data folder's
$PyLib   = "$env:LOCALAPPDATA\360 Video Shrinker\pylib"
if (Test-Path "$App\install.json") {
    $cfg = Get-Content "$App\install.json" -Raw | ConvertFrom-Json
    if ($cfg.pylib) { $PyLib = [Environment]::ExpandEnvironmentVariables($cfg.pylib) } }
$Name    = "360 Video Shrinker $Version"
$Dist    = "$App\dist"
$Stage   = "$Dist\build\$Name"
$Zip     = "$Dist\$Name.zip"

function Robo($from, $to, [string[]]$extra) {
    & robocopy $from $to /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 @extra | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $from -> $to failed ($LASTEXITCODE)" }
}

if (Test-Path "$Dist\build") { Remove-Item "$Dist\build" -Recurse -Force }
New-Item -ItemType Directory -Force "$Stage\payload\app", "$Stage\payload\python\Lib\site-packages" | Out-Null

# --- Python runtime (no docs, headers, pip, tests, IDLE, Tk demos, compiled caches) -------------------------
Robo $PyHome "$Stage\payload\python" @('/XD', "$PyHome\Doc", "$PyHome\include", "$PyHome\libs", "$PyHome\Scripts",
    "$PyHome\Lib\site-packages", "$PyHome\Lib\test", "$PyHome\Lib\idlelib", "$PyHome\Lib\ensurepip", "$PyHome\Lib\venv", "$PyHome\tcl\nmake",
    '__pycache__', 'demos', 'idle_test', '/XF', 'NEWS.txt', '*.pdb')
Copy-Item "$App\packaging\python314._pth" "$Stage\payload\python\"      # isolated: ignores any other Python
$site = "$Stage\payload\python\Lib\site-packages"
foreach ($d in 'numpy', 'numpy.libs', 'PIL') { Robo "$UserSite\$d" "$site\$d" @('/XD', '__pycache__', 'tests') }
foreach ($d in (Get-ChildItem $UserSite -Directory | Where-Object Name -match '^(numpy|pillow)-[\d.]+\.dist-info$')) {
    Robo $d.FullName "$site\$($d.Name)" @() }
foreach ($d in 'av', 'av.libs', 'av-19.0.2.dist-info') { Robo "$PyLib\$d" "$site\$d" @('/XD', '__pycache__') }
# (pylib\bin\pyav.exe is left out: the app does not use it)

# --- the program ------------------------------------------------------------------------------------------
foreach ($f in 'app.pyw', 'contextmenu.py', 'engine.py', 'gui.py', 'icon.ico', 'mp4meta.py', 'paths.py',
               'presets.py', 'selftest.py', 'shrink360.py') { Copy-Item "$App\$f" "$Stage\payload\app\" }
Copy-Item "$App\packaging\README-install.txt" "$Stage\payload\app\README.txt"
Copy-Item "$App\packaging\setup.py" "$Stage\payload\"
Copy-Item "$App\packaging\Install.cmd", "$App\packaging\READ ME FIRST.txt" "$Stage\"
Copy-Item "$App\LICENSE" "$Stage\LICENSE.txt"
Copy-Item "$App\LICENSE" "$Stage\payload\app\LICENSE.txt"

# --- leave-out check: nothing personal may be in the package ----------------------------------------------
# anywhere: data, settings, install records, private notes, Python start-up hooks, logs;
# outside the bundled Python (whose Tcl/Tk .msg files and licence .md files are upstream): photos, videos, notes, archives
$bad = Get-ChildItem $Stage -Recurse -File | Where-Object {
    $_.Name -match ('^(presets|settings|install|install-record|direct_url)\.json$|\.jsonl$|\.lnk$|\.private\.|' +
                    '^denylist\.txt$|^pyvenv\.cfg$|^(site|user)customize\.py$|\.(pth|pyc|log)$') -or
    ($_.FullName -notlike "$Stage\payload\python\*" -and
     $_.Name -match '\.(md|msg|zip|7z|jpe?g|heic|heif|png|webp|dng|tiff?|insv|insp|360|lrv|mp4|mov|m4v|mkv|webm|avi)$') -or
    ($_.Extension -eq '.exe' -and $_.Name -notin 'python.exe', 'pythonw.exe') }
if ($bad) { throw "Unexpected files in the package: $($bad.FullName -join ', ')" }
foreach ($exe in 'python.exe', 'pythonw.exe') {
    $sig = Get-AuthenticodeSignature "$Stage\payload\python\$exe"
    if ($sig.Status -ne 'Valid') { throw "$exe is not validly signed ($($sig.Status))" }
}

# --- zip ----------------------------------------------------------------------------------------------------
if (Test-Path $Zip) { Remove-Item $Zip -Force }
# fixed dates, '/' names, no pip install records (tools\make_zip.py)
& "$PyHome\python.exe" -I "$App\tools\make_zip.py" "$Dist\build" $Zip
if ($LASTEXITCODE) { throw "make_zip.py failed" }
$mb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
$files = (Get-ChildItem $Stage -Recurse -File).Count
Write-Output "Package: $Zip ($mb MB, $files files)"
