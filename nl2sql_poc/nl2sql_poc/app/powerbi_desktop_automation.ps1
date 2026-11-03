# ===========================================================================
# powerbi_desktop_automation.ps1 - remote control for the open Power BI Desktop window.
#
# WHO CALLS IT: app/powerbi_service.py (PowerBIService.run_desktop_automation) starts it as a
# child process: powershell.exe -File this.ps1 -Action <...> -ReportDir <...> [-TableName <...>]
#
# WHAT IT DOES (one -Action per run):
#   status          - find the Desktop window that has our project open and report its state
#   prepare         - if Desktop has unsaved edits, press its Save button (so the Python code can
#                     safely write new files into the project folder afterwards)
#   reload_refresh  - tell Desktop to reload the project from disk (so it sees the new page/table),
#                     then refresh (re-import) the new table's rows from MySQL
#
# HOW IT TALKS TO POWER BI DESKTOP (three different channels):
#   1. "Desktop Bridge" - a named pipe called pbi-desktop-bridge-<processId>. A named pipe is a
#      local, in-memory connection between two programs (like a private socket). Messages are
#      JSON-RPC (a JSON request with a "method" name, a JSON reply) with an HTTP-like
#      "Content-Length" header in front. Used to read state and to reload the project.
#      Only works when Desktop's "external tool access through secure local APIs" option is on.
#   2. Windows UI Automation - finds the real "Save" button in Desktop's window and clicks it.
#   3. Analysis Services - Desktop runs a hidden local Analysis Services engine (msmdsrv.exe) that
#      holds the data model. It listens on a random localhost port written to msmdsrv.port.txt.
#      The script connects there with Microsoft's Tabular Object Model DLL and asks for a refresh.
#
# OUTPUT CONTRACT: the LAST line written to stdout is one JSON object, {"success": true|false, ...}.
# The Python side parses only that line. Everything else goes to logs/powerbi_automation.log.
# Exit code 0 = success, 1 = failure.
# ===========================================================================
[CmdletBinding()]
param(
    # Which job to do (see list above). ValidateSet rejects anything else before the script runs.
    [Parameter(Mandatory = $true)]
    [ValidateSet('status', 'prepare', 'reload_refresh')]
    [string]$Action,

    # Path of the <Name>.Report folder; used to recognise which Desktop window has our project open.
    [Parameter(Mandatory = $true)]
    [string]$ReportDir,

    # Model table to refresh, e.g. "billing vw_monthly_revenue_ab12" (only for reload_refresh).
    [string]$TableName,

    # Upper limit for waiting on Desktop's save (Python kills the whole script 30 s after this).
    [int]$TimeoutSeconds = 180,

    # Optional diagnostic log file. Defaults to <backend>\logs\powerbi_automation.log.
    [string]$LogFile
)

# Make every error a terminating error, so it jumps to the nearest catch block instead of
# printing a warning and carrying on with bad data.
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# File logging. Never writes to stdout (the backend parses the last stdout line
# as JSON) and never throws.
# ---------------------------------------------------------------------------
# $script: variables live for the whole script run and are visible inside functions.
# RunStopwatch = total run time; RunId = "<process id>-<8 random hex>" so the lines of one run
# can be picked out of the shared log file when several runs are interleaved.
$script:RunStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$script:RunId = "$PID-" + [guid]::NewGuid().ToString('N').Substring(0, 8)
$script:LogPath = $null
# Work out the log file path (default: ..\logs\powerbi_automation.log next to the app folder)
# and create the logs folder. If that fails, LogPath stays $null and logging is silently off.
try {
    if ([string]::IsNullOrWhiteSpace($LogFile)) {
        $LogFile = Join-Path $PSScriptRoot '..\logs\powerbi_automation.log'
    }
    $script:LogPath = [System.IO.Path]::GetFullPath($LogFile)
    $logDir = [System.IO.Path]::GetDirectoryName($script:LogPath)
    if ($logDir -and -not [System.IO.Directory]::Exists($logDir)) {
        [System.IO.Directory]::CreateDirectory($logDir) | Out-Null
    }
}
catch {
    $script:LogPath = $null
}

# Write-Log: append one line "time [LEVEL] [action] [run id] message" to the log file.
# Uses .NET AppendAllText (UTF-8 without BOM) rather than Write-Output, because anything sent to
# stdout would confuse the Python side that reads stdout. Called everywhere in this script.
function Write-Log {
    param(
        [string]$Message,
        [ValidateSet('DEBUG', 'INFO', 'WARNING', 'ERROR')]
        [string]$Level = 'INFO'
    )
    try {
        if (-not $script:LogPath) { return }
        $line = '{0} [{1}] [{2}] [run {3}] {4}{5}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff'), $Level, $Action, $script:RunId, $Message, [Environment]::NewLine
        [System.IO.File]::AppendAllText($script:LogPath, $line, (New-Object System.Text.UTF8Encoding($false)))
    }
    catch {
        # Logging must never break the automation.
    }
}

# Format-Elapsed: turns a stopwatch into text like "1234 ms" for log messages.
function Format-Elapsed {
    param([System.Diagnostics.Stopwatch]$Watch)
    try { return ('{0} ms' -f [int64]$Watch.Elapsed.TotalMilliseconds) } catch { return '? ms' }
}

# Read-BridgeFrame: read ONE reply message from the Desktop Bridge pipe and return it as an object.
# A message ("frame") looks like:   Content-Length: 123<CR><LF><CR><LF>{ ...123 bytes of JSON... }
# Step 1 reads the header byte by byte until the blank line (\r\n\r\n) that ends it.
# Step 2 reads exactly Content-Length bytes of body and parses them as JSON.
# Called only by Invoke-Bridge. Throws if the pipe closes early or the header looks wrong.
function Read-BridgeFrame {
    param([System.IO.Stream]$Pipe)

    $header = New-Object System.Text.StringBuilder
    # $last4 keeps the last 4 characters read, to spot the "\r\n\r\n" end-of-header marker.
    $last4 = ''
    while ($true) {
        $b = $Pipe.ReadByte()
        if ($b -lt 0) { throw 'Power BI Desktop Bridge closed the pipe before returning headers.' }
        $ch = [char]$b
        [void]$header.Append($ch)
        $last4 = ($last4 + $ch)
        if ($last4.Length -gt 4) { $last4 = $last4.Substring($last4.Length - 4) }
        if ($last4 -eq "`r`n`r`n") { break }
        if ($header.Length -gt 16384) { throw 'Power BI Desktop Bridge response headers were too large.' }
    }

    # Pull the body length out of the header (case-insensitive, any line), with sanity limits.
    $match = [regex]::Match($header.ToString(), '(?im)^Content-Length:\s*(\d+)\s*$')
    if (-not $match.Success) { throw 'Power BI Desktop Bridge response omitted Content-Length.' }
    $length = [int]$match.Groups[1].Value
    if ($length -lt 1 -or $length -gt 10000000) { throw "Unexpected Power BI Desktop Bridge response length: $length" }

    # Read() may return fewer bytes than asked for, so keep reading until the body is complete.
    $buffer = New-Object byte[] $length
    $offset = 0
    while ($offset -lt $length) {
        $read = $Pipe.Read($buffer, $offset, $length - $offset)
        if ($read -le 0) { throw 'Power BI Desktop Bridge closed the pipe before returning the response body.' }
        $offset += $read
    }
    return ([System.Text.Encoding]::UTF8.GetString($buffer) | ConvertFrom-Json)
}

# Invoke-Bridge: send one JSON-RPC request to a Power BI Desktop process and return its "result".
# Inputs: DesktopProcessId = Windows process id of PBIDesktop.exe (each Desktop window has its own
#         pipe named after its process id); Method = e.g. 'application.state.get/v1' or
#         'file.reload/v1'; Params = the method's arguments.
# A new pipe connection is opened and closed for every call (simple and stateless).
# Throws if Desktop cannot be reached within 7 s or returns an "error" member.
function Invoke-Bridge {
    param(
        [int]$DesktopProcessId,
        [string]$Method,
        [hashtable]$Params = @{}
    )

    $pipeName = "pbi-desktop-bridge-$DesktopProcessId"
    $paramKeys = if ($Params -and $Params.Count) { ($Params.Keys | Sort-Object) -join ',' } else { '' }
    Write-Log "Invoke-Bridge: calling '$Method' on pipe '$pipeName' (param keys: [$paramKeys])"
    $callWatch = [System.Diagnostics.Stopwatch]::StartNew()
    # '.' = this computer; InOut = we both write the request and read the reply on the same pipe.
    $pipe = [System.IO.Pipes.NamedPipeClientStream]::new('.', $pipeName, [System.IO.Pipes.PipeDirection]::InOut)
    try {
        # Wait up to 7000 ms for Desktop to accept the connection (throws a timeout otherwise).
        $pipe.Connect(7000)
        # Build the JSON-RPC 2.0 request; the random id would let a reply be matched to its request.
        $requestId = [guid]::NewGuid().ToString('N')
        $requestObject = @{ jsonrpc = '2.0'; id = $requestId; method = $Method; params = $Params }
        $json = $requestObject | ConvertTo-Json -Compress -Depth 12
        # Frame it the same way replies are framed: ASCII header with the byte length, blank line, body.
        $body = [System.Text.Encoding]::UTF8.GetBytes($json)
        $head = [System.Text.Encoding]::ASCII.GetBytes("Content-Length: $($body.Length)`r`n`r`n")
        $pipe.Write($head, 0, $head.Length)
        $pipe.Write($body, 0, $body.Length)
        $pipe.Flush()

        $response = Read-BridgeFrame -Pipe $pipe
        if ($response.error) {
            throw "Desktop Bridge method '$Method' failed: $($response.error.message)"
        }
        Write-Log "Invoke-Bridge: '$Method' on PID $DesktopProcessId succeeded in $(Format-Elapsed $callWatch)"
        return $response.result
    }
    catch {
        # Callers decide whether this is fatal (some failures are expected while probing/polling).
        Write-Log "Invoke-Bridge: '$Method' on PID $DesktopProcessId failed after $(Format-Elapsed $callWatch): $($_.Exception.Message)" 'WARNING'
        throw
    }
    finally {
        # Always close the pipe, whether the call succeeded or failed.
        $pipe.Dispose()
    }
}

# Get-DesktopState: asks Desktop for its current state. Fields used in this script:
#   currentFilePath   - the .pbip file open in that window
#   hasUnsavedChanges - true when the user has edits that are not saved yet ("dirty")
function Get-DesktopState {
    param([int]$DesktopProcessId)
    return (Invoke-Bridge -DesktopProcessId $DesktopProcessId -Method 'application.state.get/v1' -Params @{})
}

# Find-TargetDesktop: several Power BI Desktop windows may be open. Find exactly the one that
# has OUR project open, so we never save/reload somebody else's report.
# How: for "...\Billing.Report" it expects the open file path to contain the parent folder and
# the name "Billing" (the .pbip file is <parent>\Billing.pbip). Every PBIDesktop process is asked
# for its state over the Bridge. Returns @{ ProcessId; State }. Throws if 0 or more than 1 match.
function Find-TargetDesktop {
    param([string]$TargetReportDir)

    $fullReportDir = [System.IO.Path]::GetFullPath($TargetReportDir).TrimEnd('\', '/')
    $expectedParent = [System.IO.Path]::GetDirectoryName($fullReportDir).TrimEnd('\', '/')
    $reportLeaf = [System.IO.Path]::GetFileName($fullReportDir)
    $expectedStem = $reportLeaf -replace '\.Report$', ''
    # Note: $matches is also PowerShell's automatic variable for -match results; here it is
    # simply reused as an ordinary array of matching windows.
    $matches = @()

    $findWatch = [System.Diagnostics.Stopwatch]::StartNew()
    # @( ) forces an array even when there are 0 or 1 processes, so .Count always works.
    $candidates = @(Get-Process -Name 'PBIDesktop' -ErrorAction SilentlyContinue)
    Write-Log "Find-TargetDesktop: found $($candidates.Count) PBIDesktop process(es); looking for stem '$expectedStem' under '$expectedParent'"

    foreach ($proc in $candidates) {
        try {
            $state = Get-DesktopState -DesktopProcessId $proc.Id
            $current = [string]$state.currentFilePath
            # An empty path means an unsaved/new report in that window: it cannot be ours.
            if ([string]::IsNullOrWhiteSpace($current)) {
                Write-Log "Find-TargetDesktop: PID $($proc.Id) has no currentFilePath; skipping" 'WARNING'
                continue
            }
            if ($current.IndexOf($expectedParent, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $current.IndexOf($expectedStem, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                Write-Log "Find-TargetDesktop: PID $($proc.Id) matches ('$current')"
                $matches += [pscustomobject]@{ ProcessId = [int]$proc.Id; State = $state }
            }
            else {
                Write-Log "Find-TargetDesktop: PID $($proc.Id) has a different file open ('$current'); skipping"
            }
        }
        catch {
            # Ignore Desktop processes where the local Bridge is unavailable; match only an exact project path.
            Write-Log "Find-TargetDesktop: Bridge unavailable for PID $($proc.Id); skipping: $($_.Exception.Message)" 'WARNING'
        }
    }

    Write-Log "Find-TargetDesktop: $($matches.Count) matching window(s) after $(Format-Elapsed $findWatch)"
    # Refuse to guess: zero matches (project not open / Bridge disabled) or several matches
    # (same project open twice) both stop the run with a clear message.
    if ($matches.Count -eq 0) {
        throw "Could not find an open Power BI Desktop Bridge instance for '$expectedStem'. Open the matching .pbip project and confirm 'Enable external tool access to Power BI Desktop through secure local APIs' is enabled."
    }
    if ($matches.Count -gt 1) {
        throw "More than one Power BI Desktop window matches '$expectedStem'. Close duplicate windows so the automation cannot update the wrong report."
    }
    return $matches[0]
}

# Get-DesktopWindows: Windows UI Automation (the accessibility API screen readers use) lists
# every top-level window on the desktop; keep only those that belong to this Desktop process.
function Get-DesktopWindows {
    param([int]$DesktopProcessId)
    $ae = [System.Windows.Automation.AutomationElement]
    $condition = New-Object System.Windows.Automation.PropertyCondition($ae::ProcessIdProperty, $DesktopProcessId)
    return @($ae::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $condition))
}

# Find-SaveButton: search inside Desktop's windows for a Button whose AutomationId is 'save'
# (the Save icon in Power BI Desktop's title bar). Only an enabled, visible button is returned,
# because clicking a disabled/hidden one would do nothing. Returns $null if none is found.
function Find-SaveButton {
    param([int]$DesktopProcessId)
    $ae = [System.Windows.Automation.AutomationElement]
    $idCondition = New-Object System.Windows.Automation.PropertyCondition($ae::AutomationIdProperty, 'save')
    $typeCondition = New-Object System.Windows.Automation.PropertyCondition($ae::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $condition = New-Object System.Windows.Automation.AndCondition($idCondition, $typeCondition)
    $windows = @(Get-DesktopWindows -DesktopProcessId $DesktopProcessId)
    Write-Log "Find-SaveButton: UI Automation found $($windows.Count) top-level window(s) for PID $DesktopProcessId"
    foreach ($window in $windows) {
        foreach ($button in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($button.Current.IsEnabled -and -not $button.Current.IsOffscreen) {
                Write-Log "Find-SaveButton: found enabled on-screen Save button in window '$($window.Current.Name)'"
                return $button
            }
        }
    }
    Write-Log 'Find-SaveButton: no enabled on-screen Save button found' 'WARNING'
    return $null
}

# Save-DesktopIfDirty: the "prepare" step. Why: the Python generator is about to write files into
# the project folder. If Desktop has unsaved edits, a later reload would throw them away, or a
# later manual Save would overwrite our new files. So we save first.
# This script does not save through the Bridge; instead it clicks the real Save button via
# UI Automation and then polls the Bridge until hasUnsavedChanges becomes false.
# Returns @{ attempted; saved; message }. Throws if saving cannot be done or confirmed.
function Save-DesktopIfDirty {
    param([int]$DesktopProcessId)

    $state = Get-DesktopState -DesktopProcessId $DesktopProcessId
    if (-not [bool]$state.hasUnsavedChanges) {
        Write-Log 'Save-DesktopIfDirty: no unsaved changes; save not needed'
        return [pscustomobject]@{ attempted = $false; saved = $true; message = 'No unsaved Desktop changes.' }
    }
    Write-Log "Save-DesktopIfDirty: Desktop has unsaved changes (file '$([string]$state.currentFilePath)')"
    if ([string]::IsNullOrWhiteSpace([string]$state.currentFilePath)) {
        throw 'Power BI Desktop has unsaved changes but no saved file path. Save the PBIP project once manually before using automatic mode.'
    }

    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $saveButton = Find-SaveButton -DesktopProcessId $DesktopProcessId
    if (-not $saveButton) {
        throw 'Power BI Desktop has unsaved changes, but the Save button could not be invoked through Windows UI Automation. Save the project manually once, then retry.'
    }
    # InvokePattern = UI Automation's way of "clicking" a button without moving the mouse.
    $savePattern = $saveButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    Write-Log 'Save-DesktopIfDirty: invoking Save button via UI Automation InvokePattern'
    $savePattern.Invoke()

    $saveWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $pollCount = 0
    Write-Log "Save-DesktopIfDirty: polling application state every 400 ms for up to $TimeoutSeconds s until hasUnsavedChanges is false"
    # Poll until Desktop reports "not dirty" or the -TimeoutSeconds deadline passes.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        $pollCount++
        try {
            $state = Get-DesktopState -DesktopProcessId $DesktopProcessId
            if (-not [bool]$state.hasUnsavedChanges) {
                Write-Log "Save-DesktopIfDirty: save confirmed after $pollCount poll(s), $(Format-Elapsed $saveWatch)"
                return [pscustomobject]@{ attempted = $true; saved = $true; message = 'Saved pending Desktop changes before modifying project files.' }
            }
        }
        catch {
            # Desktop Bridge can be temporarily busy while the save completes.
            Write-Log "Save-DesktopIfDirty: poll $pollCount failed (Bridge busy?); retrying: $($_.Exception.Message)" 'WARNING'
        }
    }
    Write-Log "Save-DesktopIfDirty: save NOT confirmed after $pollCount poll(s), $(Format-Elapsed $saveWatch)" 'ERROR'
    throw "Power BI Desktop did not confirm the save within $TimeoutSeconds seconds; refusing to edit the project files."
}

# Refresh-LocalModelTable: re-import one table's rows (from the MySQL view) into Desktop's model.
# Why this is needed: reloading the project only loads the table DEFINITION (columns, M query);
# an import-mode table stays empty until it is refreshed.
# Steps:
#   1. find PBIDesktop.exe on disk and load Microsoft.AnalysisServices.Tabular.dll that ships
#      with it (the Tabular Object Model, Microsoft's .NET API for tabular models)
#   2. find msmdsrv.port.txt files - each holds the localhost port of a running Desktop model
#   3. connect to each port (newest file first) until a database containing the table is found,
#      then request a Full refresh of that table and SaveChanges() (which actually runs it)
# Returns @{ completed; database; table; port; refreshType; message }. Throws if nothing worked.
function Refresh-LocalModelTable {
    param(
        [int]$DesktopProcessId,
        [string]$TargetTableName
    )

    if ([string]::IsNullOrWhiteSpace($TargetTableName)) { throw 'TableName is required for reload_refresh.' }

    # Step 1: locate the Desktop executable. Process.Path can be empty (e.g. permissions), so
    # the WMI/CIM Win32_Process query is used as a fallback.
    $proc = Get-Process -Id $DesktopProcessId -ErrorAction Stop
    $exePath = $null
    try { $exePath = $proc.Path } catch { }
    if ([string]::IsNullOrWhiteSpace($exePath)) {
        Write-Log 'Refresh-LocalModelTable: Process.Path unavailable; falling back to Win32_Process.ExecutablePath' 'WARNING'
        try { $exePath = (Get-CimInstance Win32_Process -Filter "ProcessId=$DesktopProcessId").ExecutablePath } catch { }
    }
    if ([string]::IsNullOrWhiteSpace($exePath)) {
        throw 'Could not locate the running Power BI Desktop executable to load its Analysis Services libraries.'
    }
    Write-Log "Refresh-LocalModelTable: Desktop executable '$exePath'"

    # Look for the Tabular DLL: (a) next to the exe, (b) standard Program Files install folders,
    # (c) a recursive search of the package folder (Microsoft Store installs).
    # Using Desktop's own copy guarantees a version that matches the running engine.
    $binDir = Split-Path $exePath -Parent
    $tabularDll = Join-Path $binDir 'Microsoft.AnalysisServices.Tabular.dll'
    if (-not (Test-Path $tabularDll)) {
        Write-Log "Refresh-LocalModelTable: Tabular DLL not next to executable; trying Program Files fallbacks" 'WARNING'
        $fallbacks = @(
            (Join-Path $env:ProgramFiles 'Microsoft Power BI Desktop\bin\Microsoft.AnalysisServices.Tabular.dll'),
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Power BI Desktop\bin\Microsoft.AnalysisServices.Tabular.dll')
        )
        $tabularDll = $fallbacks | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $tabularDll -or -not (Test-Path $tabularDll)) {
        # Microsoft Store installs keep the libraries elsewhere inside the package folder.
        Write-Log "Refresh-LocalModelTable: Tabular DLL not in Program Files; searching package folder recursively" 'WARNING'
        $searchRoots = @((Split-Path $binDir -Parent), $binDir) |
            Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
        foreach ($root in $searchRoots) {
            $found = Get-ChildItem -Path $root -Filter 'Microsoft.AnalysisServices.Tabular.dll' -Recurse -File -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($found) { $tabularDll = $found.FullName; break }
        }
    }
    if (-not $tabularDll -or -not (Test-Path $tabularDll)) {
        throw "Could not find Microsoft.AnalysisServices.Tabular.dll in the running Desktop installation ('$binDir') or its package folder."
    }
    Write-Log "Refresh-LocalModelTable: loading Tabular Object Model from '$tabularDll'"

    # The Tabular DLL depends on other DLLs in the same folder, which .NET would not find on its
    # own. This AssemblyResolve handler is called whenever .NET cannot find a dependency; it looks
    # for "<name>.dll" in the Tabular DLL's folder and loads it from there.
    $script:TabularAssemblyDir = Split-Path $tabularDll -Parent
    $resolver = [System.ResolveEventHandler]{
        param($sender, $eventArgs)
        $requestedName = (New-Object System.Reflection.AssemblyName($eventArgs.Name)).Name + '.dll'
        $candidatePath = Join-Path $script:TabularAssemblyDir $requestedName
        if (Test-Path $candidatePath) { return [System.Reflection.Assembly]::LoadFrom($candidatePath) }
        return $null
    }
    [AppDomain]::CurrentDomain.add_AssemblyResolve($resolver)
    # Add-Type -Path loads the DLL so its classes (Microsoft.AnalysisServices.Tabular.*) can be used.
    # On failure the detailed error goes to the log and a shorter message is re-thrown.
    try {
        Add-Type -Path $tabularDll
    }
    catch {
        Write-Log "Refresh-LocalModelTable: Add-Type for Tabular DLL failed: $($_.Exception.Message)`n$($_.ScriptStackTrace)" 'ERROR'
        throw "Could not load Power BI Desktop's Tabular Object Model libraries: $($_.Exception.Message)"
    }

    # Step 2: find the port. Each open Desktop file starts its own Analysis Services instance in a
    # "workspace" folder and writes the chosen localhost port into msmdsrv.port.txt there.
    # Different install types (normal installer / Microsoft Store) use different folders, so all
    # known locations are searched.
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Power BI Desktop\AnalysisServicesWorkspaces'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Power BI Desktop Store App\AnalysisServicesWorkspaces'),
        (Join-Path $env:USERPROFILE 'Microsoft\Power BI Desktop Store App\AnalysisServicesWorkspaces')
    )
    $packagesRoot = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path $packagesRoot) {
        foreach ($package in (Get-ChildItem -Path $packagesRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*MicrosoftPowerBIDesktop*' })) {
            $roots += Join-Path $package.FullName 'LocalCache\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces'
        }
    }
    $portWatch = [System.Diagnostics.Stopwatch]::StartNew()
    $portFiles = @()
    foreach ($root in ($roots | Select-Object -Unique)) {
        if (Test-Path $root) {
            $portFiles += @(Get-ChildItem -Path $root -Filter 'msmdsrv.port.txt' -File -Recurse -Force -ErrorAction SilentlyContinue)
        }
    }
    # Newest first: old workspace folders can be left behind by closed Desktop sessions, and the
    # most recently written port file most likely belongs to the running instance.
    $portFiles = @($portFiles | Sort-Object LastWriteTime -Descending -Unique)
    Write-Log "Refresh-LocalModelTable: Analysis Services port lookup found $($portFiles.Count) msmdsrv.port.txt file(s) in $(Format-Elapsed $portWatch)"
    if ($portFiles.Count -eq 0) { throw 'Could not locate a local Power BI Desktop Analysis Services port file (msmdsrv.port.txt).' }

    # Step 3: try each port in turn. Errors are collected in $lastErrors (for the final message)
    # instead of stopping, because a stale port file simply points at a closed engine.
    # Note: the port files are not linked to $DesktopProcessId; the first model that contains a
    # table with this name wins.
    $lastErrors = @()
    foreach ($portFile in $portFiles) {
        $portText = (Get-Content -Path $portFile.FullName -Raw -ErrorAction SilentlyContinue).Trim()
        $port = 0
        if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
            Write-Log "Refresh-LocalModelTable: port file '$($portFile.FullName)' has no valid port; skipping" 'WARNING'
            continue
        }

        Write-Log "Refresh-LocalModelTable: connecting to Analysis Services at localhost:$port (port file '$($portFile.FullName)', modified $($portFile.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')))"
        # A Tabular "Server" object = a client connection to that Analysis Services engine.
        $server = New-Object Microsoft.AnalysisServices.Tabular.Server
        try {
            $server.Connect("localhost:$port")
            # Desktop normally has one database (= the open model) per engine; check them all.
            foreach ($database in $server.Databases) {
                $table = $null
                try { $table = $database.Model.Tables[$TargetTableName] } catch { $table = $null }
                if ($null -eq $table) {
                    Write-Log "Refresh-LocalModelTable: table '$TargetTableName' not in database '$($database.Name)' on port $port"
                    continue
                }

                Write-Log "Refresh-LocalModelTable: requesting Full refresh of '$($table.Name)' in database '$($database.Name)' on port $port"
                $refreshWatch = [System.Diagnostics.Stopwatch]::StartNew()
                # RequestRefresh only queues the request; SaveChanges() sends it to the engine and
                # waits while the rows are re-imported from MySQL. "Full" = reload all data.
                $table.RequestRefresh([Microsoft.AnalysisServices.Tabular.RefreshType]::Full)
                $null = $database.Model.SaveChanges()
                Write-Log "Refresh-LocalModelTable: SaveChanges (refresh) completed in $(Format-Elapsed $refreshWatch)"
                return [pscustomobject]@{
                    completed = $true
                    database = [string]$database.Name
                    table = [string]$table.Name
                    port = $port
                    refreshType = 'Full'
                    message = 'Target table refresh completed through the local Analysis Services model.'
                }
            }
            Write-Log "Refresh-LocalModelTable: no database on port $port contains table '$TargetTableName'; trying next port file" 'WARNING'
        }
        catch {
            Write-Log "Refresh-LocalModelTable: port ${port} failed; trying next port file: $($_.Exception.Message)`n$($_.ScriptStackTrace)" 'ERROR'
            $lastErrors += "port ${port}: $($_.Exception.Message)"
        }
        finally {
            # Always disconnect (also after the successful `return` above); ignore disconnect errors.
            try { $server.Disconnect() } catch { }
        }
    }

    # Every port tried and none worked: fail with the collected connection errors.
    $detail = if ($lastErrors.Count) { ' Connection details: ' + ($lastErrors -join ' | ') } else { '' }
    throw "Could not find and refresh table '$TargetTableName' in a running Power BI Desktop model.$detail"
}

# ===========================================================================
# MAIN PROGRAM - everything above only defined functions; execution starts here.
# The whole run is in one try/catch: any error anywhere ends in the catch block, which logs it,
# prints {"success": false, "error": ...} as the last stdout line and exits with code 1.
# ===========================================================================
Write-Log "Start: action='$Action' reportDir='$ReportDir' table='$TableName' timeoutSeconds=$TimeoutSeconds pid=$PID log='$($script:LogPath)'"
try {
    # Load the .NET UI Automation libraries (used by Find-SaveButton / Get-DesktopWindows).
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    Write-Log 'Step: locating target Power BI Desktop process via Bridge'
    $target = Find-TargetDesktop -TargetReportDir $ReportDir
    $desktopProcessId = [int]$target.ProcessId
    $state = $target.State
    Write-Log "Step: target Desktop PID $desktopProcessId, file '$([string]$state.currentFilePath)', hasUnsavedChanges=$([bool]$state.hasUnsavedChanges)"

    # Each branch only fills $output; it is printed as JSON after the switch.
    switch ($Action) {
        # status: no changes, just report what was found (useful for diagnostics).
        'status' {
            $output = [pscustomobject]@{
                success = $true; action = $Action; processId = $desktopProcessId
                currentFilePath = [string]$state.currentFilePath
                hasUnsavedChanges = [bool]$state.hasUnsavedChanges
            }
        }
        # prepare: called by PowerBIService.build_page() BEFORE the Python code writes new files.
        'prepare' {
            Write-Log 'Step: save Desktop if dirty'
            $saveResult = Save-DesktopIfDirty -DesktopProcessId $desktopProcessId
            Write-Log "Step: save result attempted=$($saveResult.attempted) saved=$($saveResult.saved): $($saveResult.message)"
            $output = [pscustomobject]@{
                success = $true; action = $Action; processId = $desktopProcessId
                currentFilePath = [string]$state.currentFilePath
                save = $saveResult
            }
        }
        # reload_refresh: called by PowerBIService.reload_refresh() AFTER the files were written.
        'reload_refresh' {
            # Safety check: if the user edited something since "prepare", reloading from disk
            # would silently discard those edits, so stop instead.
            Write-Log 'Step: re-checking Desktop state before reload'
            $freshState = Get-DesktopState -DesktopProcessId $desktopProcessId
            Write-Log "Step: fresh state hasUnsavedChanges=$([bool]$freshState.hasUnsavedChanges)"
            if ([bool]$freshState.hasUnsavedChanges) {
                throw 'Power BI Desktop became dirty after the pre-generation save. Refusing to reload because it could overwrite in-memory changes; do not click Save or reload manually until this is resolved.'
            }
            Write-Log 'Step: reloading project via Bridge file.reload/v1 (reloadModelDefinition=true)'
            # Ask Desktop to re-read the project folder. reloadModelDefinition = also re-read the
            # semantic model (TMDL), not only the report pages, so the new table is picked up.
            # This re-creates every visual, including the chatbot (hence it runs after the reply).
            $reloadWatch = [System.Diagnostics.Stopwatch]::StartNew()
            $reloadResult = Invoke-Bridge -DesktopProcessId $desktopProcessId -Method 'file.reload/v1' -Params @{ reloadModelDefinition = $true }
            if ($reloadResult -and $reloadResult.success -eq $false) { throw 'Power BI Desktop Bridge reported that external project reload failed.' }
            Write-Log "Step: reload returned in $(Format-Elapsed $reloadWatch); waiting 2 s before table refresh"
            # Short pause so the reloaded model is settled before we connect to its engine.
            Start-Sleep -Seconds 2
            Write-Log "Step: refreshing table '$TableName' through local Analysis Services"
            $refreshResult = Refresh-LocalModelTable -DesktopProcessId $desktopProcessId -TargetTableName $TableName
            Write-Log "Step: refresh completed (database '$($refreshResult.database)', table '$($refreshResult.table)', port $($refreshResult.port))"
            $output = [pscustomobject]@{
                success = $true; action = $Action; processId = $desktopProcessId
                currentFilePath = [string]$freshState.currentFilePath
                reload = @{ completed = $true; reloadModelDefinition = $true }
                refresh = $refreshResult
            }
        }
    }
    Write-Log "Finish: SUCCESS in $(Format-Elapsed $script:RunStopwatch)"
    # The ONLY stdout line: compact one-line JSON that powerbi_service.py parses.
    Write-Output ($output | ConvertTo-Json -Compress -Depth 10)
    exit 0
}
catch {
    # $_ is the error record; its message becomes "error" in the JSON, which the Python side
    # shows in backend_billing.log and in the PowerBIProjectError it raises.
    Write-Log "Failure: $($_.Exception.Message)`n$($_.ScriptStackTrace)" 'ERROR'
    Write-Log "Finish: FAILURE in $(Format-Elapsed $script:RunStopwatch)" 'ERROR'
    $failure = [pscustomobject]@{ success = $false; action = $Action; error = $_.Exception.Message }
    Write-Output ($failure | ConvertTo-Json -Compress -Depth 10)
    exit 1
}
