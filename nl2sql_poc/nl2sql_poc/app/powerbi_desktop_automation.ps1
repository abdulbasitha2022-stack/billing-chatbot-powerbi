[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('status', 'prepare', 'reload_refresh')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$ReportDir,

    [string]$TableName,

    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'

function Read-BridgeFrame {
    param([System.IO.Stream]$Pipe)

    $header = New-Object System.Text.StringBuilder
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

    $match = [regex]::Match($header.ToString(), '(?im)^Content-Length:\s*(\d+)\s*$')
    if (-not $match.Success) { throw 'Power BI Desktop Bridge response omitted Content-Length.' }
    $length = [int]$match.Groups[1].Value
    if ($length -lt 1 -or $length -gt 10000000) { throw "Unexpected Power BI Desktop Bridge response length: $length" }

    $buffer = New-Object byte[] $length
    $offset = 0
    while ($offset -lt $length) {
        $read = $Pipe.Read($buffer, $offset, $length - $offset)
        if ($read -le 0) { throw 'Power BI Desktop Bridge closed the pipe before returning the response body.' }
        $offset += $read
    }
    return ([System.Text.Encoding]::UTF8.GetString($buffer) | ConvertFrom-Json)
}

function Invoke-Bridge {
    param(
        [int]$DesktopProcessId,
        [string]$Method,
        [hashtable]$Params = @{}
    )

    $pipeName = "pbi-desktop-bridge-$DesktopProcessId"
    $pipe = [System.IO.Pipes.NamedPipeClientStream]::new('.', $pipeName, [System.IO.Pipes.PipeDirection]::InOut)
    try {
        $pipe.Connect(7000)
        $requestId = [guid]::NewGuid().ToString('N')
        $requestObject = @{ jsonrpc = '2.0'; id = $requestId; method = $Method; params = $Params }
        $json = $requestObject | ConvertTo-Json -Compress -Depth 12
        $body = [System.Text.Encoding]::UTF8.GetBytes($json)
        $head = [System.Text.Encoding]::ASCII.GetBytes("Content-Length: $($body.Length)`r`n`r`n")
        $pipe.Write($head, 0, $head.Length)
        $pipe.Write($body, 0, $body.Length)
        $pipe.Flush()

        $response = Read-BridgeFrame -Pipe $pipe
        if ($response.error) {
            throw "Desktop Bridge method '$Method' failed: $($response.error.message)"
        }
        return $response.result
    }
    finally {
        $pipe.Dispose()
    }
}

function Get-DesktopState {
    param([int]$DesktopProcessId)
    return (Invoke-Bridge -DesktopProcessId $DesktopProcessId -Method 'application.state.get/v1' -Params @{})
}

function Find-TargetDesktop {
    param([string]$TargetReportDir)

    $fullReportDir = [System.IO.Path]::GetFullPath($TargetReportDir).TrimEnd('\', '/')
    $expectedParent = [System.IO.Path]::GetDirectoryName($fullReportDir).TrimEnd('\', '/')
    $reportLeaf = [System.IO.Path]::GetFileName($fullReportDir)
    $expectedStem = $reportLeaf -replace '\.Report$', ''
    $matches = @()

    foreach ($proc in @(Get-Process -Name 'PBIDesktop' -ErrorAction SilentlyContinue)) {
        try {
            $state = Get-DesktopState -DesktopProcessId $proc.Id
            $current = [string]$state.currentFilePath
            if ([string]::IsNullOrWhiteSpace($current)) { continue }
            if ($current.IndexOf($expectedParent, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $current.IndexOf($expectedStem, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $matches += [pscustomobject]@{ ProcessId = [int]$proc.Id; State = $state }
            }
        }
        catch {
            # Ignore Desktop processes where the local Bridge is unavailable; match only an exact project path.
        }
    }

    if ($matches.Count -eq 0) {
        throw "Could not find an open Power BI Desktop Bridge instance for '$expectedStem'. Open the matching .pbip project and confirm 'Enable external tool access to Power BI Desktop through secure local APIs' is enabled."
    }
    if ($matches.Count -gt 1) {
        throw "More than one Power BI Desktop window matches '$expectedStem'. Close duplicate windows so the automation cannot update the wrong report."
    }
    return $matches[0]
}

function Get-DesktopWindows {
    param([int]$DesktopProcessId)
    $ae = [System.Windows.Automation.AutomationElement]
    $condition = New-Object System.Windows.Automation.PropertyCondition($ae::ProcessIdProperty, $DesktopProcessId)
    return @($ae::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $condition))
}

function Find-SaveButton {
    param([int]$DesktopProcessId)
    $ae = [System.Windows.Automation.AutomationElement]
    $idCondition = New-Object System.Windows.Automation.PropertyCondition($ae::AutomationIdProperty, 'save')
    $typeCondition = New-Object System.Windows.Automation.PropertyCondition($ae::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
    $condition = New-Object System.Windows.Automation.AndCondition($idCondition, $typeCondition)
    foreach ($window in (Get-DesktopWindows -DesktopProcessId $DesktopProcessId)) {
        foreach ($button in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($button.Current.IsEnabled -and -not $button.Current.IsOffscreen) { return $button }
        }
    }
    return $null
}

function Save-DesktopIfDirty {
    param([int]$DesktopProcessId)

    $state = Get-DesktopState -DesktopProcessId $DesktopProcessId
    if (-not [bool]$state.hasUnsavedChanges) {
        return [pscustomobject]@{ attempted = $false; saved = $true; message = 'No unsaved Desktop changes.' }
    }
    if ([string]::IsNullOrWhiteSpace([string]$state.currentFilePath)) {
        throw 'Power BI Desktop has unsaved changes but no saved file path. Save the PBIP project once manually before using automatic mode.'
    }

    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $saveButton = Find-SaveButton -DesktopProcessId $DesktopProcessId
    if (-not $saveButton) {
        throw 'Power BI Desktop has unsaved changes, but the Save button could not be invoked through Windows UI Automation. Save the project manually once, then retry.'
    }
    $savePattern = $saveButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $savePattern.Invoke()

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        try {
            $state = Get-DesktopState -DesktopProcessId $DesktopProcessId
            if (-not [bool]$state.hasUnsavedChanges) {
                return [pscustomobject]@{ attempted = $true; saved = $true; message = 'Saved pending Desktop changes before modifying project files.' }
            }
        }
        catch {
            # Desktop Bridge can be temporarily busy while the save completes.
        }
    }
    throw "Power BI Desktop did not confirm the save within $TimeoutSeconds seconds; refusing to edit the project files."
}

function Refresh-LocalModelTable {
    param(
        [int]$DesktopProcessId,
        [string]$TargetTableName
    )

    if ([string]::IsNullOrWhiteSpace($TargetTableName)) { throw 'TableName is required for reload_refresh.' }

    $proc = Get-Process -Id $DesktopProcessId -ErrorAction Stop
    $exePath = $null
    try { $exePath = $proc.Path } catch { }
    if ([string]::IsNullOrWhiteSpace($exePath)) {
        try { $exePath = (Get-CimInstance Win32_Process -Filter "ProcessId=$DesktopProcessId").ExecutablePath } catch { }
    }
    if ([string]::IsNullOrWhiteSpace($exePath)) {
        throw 'Could not locate the running Power BI Desktop executable to load its Analysis Services libraries.'
    }

    $binDir = Split-Path $exePath -Parent
    $tabularDll = Join-Path $binDir 'Microsoft.AnalysisServices.Tabular.dll'
    if (-not (Test-Path $tabularDll)) {
        $fallbacks = @(
            (Join-Path $env:ProgramFiles 'Microsoft Power BI Desktop\bin\Microsoft.AnalysisServices.Tabular.dll'),
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Power BI Desktop\bin\Microsoft.AnalysisServices.Tabular.dll')
        )
        $tabularDll = $fallbacks | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $tabularDll -or -not (Test-Path $tabularDll)) {
        # Microsoft Store installs keep the libraries elsewhere inside the package folder.
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

    $script:TabularAssemblyDir = Split-Path $tabularDll -Parent
    $resolver = [System.ResolveEventHandler]{
        param($sender, $eventArgs)
        $requestedName = (New-Object System.Reflection.AssemblyName($eventArgs.Name)).Name + '.dll'
        $candidatePath = Join-Path $script:TabularAssemblyDir $requestedName
        if (Test-Path $candidatePath) { return [System.Reflection.Assembly]::LoadFrom($candidatePath) }
        return $null
    }
    [AppDomain]::CurrentDomain.add_AssemblyResolve($resolver)
    try {
        Add-Type -Path $tabularDll
    }
    catch {
        throw "Could not load Power BI Desktop's Tabular Object Model libraries: $($_.Exception.Message)"
    }

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
    $portFiles = @()
    foreach ($root in ($roots | Select-Object -Unique)) {
        if (Test-Path $root) {
            $portFiles += @(Get-ChildItem -Path $root -Filter 'msmdsrv.port.txt' -File -Recurse -Force -ErrorAction SilentlyContinue)
        }
    }
    $portFiles = @($portFiles | Sort-Object LastWriteTime -Descending -Unique)
    if ($portFiles.Count -eq 0) { throw 'Could not locate a local Power BI Desktop Analysis Services port file (msmdsrv.port.txt).' }

    $lastErrors = @()
    foreach ($portFile in $portFiles) {
        $portText = (Get-Content -Path $portFile.FullName -Raw -ErrorAction SilentlyContinue).Trim()
        $port = 0
        if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) { continue }

        $server = New-Object Microsoft.AnalysisServices.Tabular.Server
        try {
            $server.Connect("localhost:$port")
            foreach ($database in $server.Databases) {
                $table = $null
                try { $table = $database.Model.Tables[$TargetTableName] } catch { $table = $null }
                if ($null -eq $table) { continue }

                $table.RequestRefresh([Microsoft.AnalysisServices.Tabular.RefreshType]::Full)
                $null = $database.Model.SaveChanges()
                return [pscustomobject]@{
                    completed = $true
                    database = [string]$database.Name
                    table = [string]$table.Name
                    port = $port
                    refreshType = 'Full'
                    message = 'Target table refresh completed through the local Analysis Services model.'
                }
            }
        }
        catch {
            $lastErrors += "port ${port}: $($_.Exception.Message)"
        }
        finally {
            try { $server.Disconnect() } catch { }
        }
    }

    $detail = if ($lastErrors.Count) { ' Connection details: ' + ($lastErrors -join ' | ') } else { '' }
    throw "Could not find and refresh table '$TargetTableName' in a running Power BI Desktop model.$detail"
}

try {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $target = Find-TargetDesktop -TargetReportDir $ReportDir
    $desktopProcessId = [int]$target.ProcessId
    $state = $target.State

    switch ($Action) {
        'status' {
            $output = [pscustomobject]@{
                success = $true; action = $Action; processId = $desktopProcessId
                currentFilePath = [string]$state.currentFilePath
                hasUnsavedChanges = [bool]$state.hasUnsavedChanges
            }
        }
        'prepare' {
            $saveResult = Save-DesktopIfDirty -DesktopProcessId $desktopProcessId
            $output = [pscustomobject]@{
                success = $true; action = $Action; processId = $desktopProcessId
                currentFilePath = [string]$state.currentFilePath
                save = $saveResult
            }
        }
        'reload_refresh' {
            $freshState = Get-DesktopState -DesktopProcessId $desktopProcessId
            if ([bool]$freshState.hasUnsavedChanges) {
                throw 'Power BI Desktop became dirty after the pre-generation save. Refusing to reload because it could overwrite in-memory changes; do not click Save or reload manually until this is resolved.'
            }
            $reloadResult = Invoke-Bridge -DesktopProcessId $desktopProcessId -Method 'file.reload/v1' -Params @{ reloadModelDefinition = $true }
            if ($reloadResult -and $reloadResult.success -eq $false) { throw 'Power BI Desktop Bridge reported that external project reload failed.' }
            Start-Sleep -Seconds 2
            $refreshResult = Refresh-LocalModelTable -DesktopProcessId $desktopProcessId -TargetTableName $TableName
            $output = [pscustomobject]@{
                success = $true; action = $Action; processId = $desktopProcessId
                currentFilePath = [string]$freshState.currentFilePath
                reload = @{ completed = $true; reloadModelDefinition = $true }
                refresh = $refreshResult
            }
        }
    }
    Write-Output ($output | ConvertTo-Json -Compress -Depth 10)
    exit 0
}
catch {
    $failure = [pscustomobject]@{ success = $false; action = $Action; error = $_.Exception.Message }
    Write-Output ($failure | ConvertTo-Json -Compress -Depth 10)
    exit 1
}
