# Points the "Pricedge daily scrape" task at this project's run_daily.cmd and
# makes it reliable (runs on battery, catches up a missed run, wakes the PC),
# then disables the two old tasks that ran the copy in Downloads.
# Needs an elevated PowerShell (the tasks were created with admin rights).
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $dir "logs\fix_scheduled_task.txt"
New-Item -ItemType Directory -Force (Join-Path $dir "logs") | Out-Null
$log = @()
try {
    $a = New-ScheduledTaskAction -Execute (Join-Path $dir "run_daily.cmd") -WorkingDirectory $dir
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
         -WakeToRun -RunOnlyIfNetworkAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    Set-ScheduledTask -TaskName "Pricedge daily scrape" -Action $a -Settings $s -ErrorAction Stop | Out-Null
    $log += "OK  daily task now runs $dir\run_daily.cmd"
} catch { $log += "ERR daily task: $($_.Exception.Message)" }
foreach ($n in "Pricedge Morning Scrape", "Pricedge Nightly Scrape") {
    try { Disable-ScheduledTask -TaskName $n -ErrorAction Stop | Out-Null; $log += "OK  disabled $n" }
    catch { $log += "ERR $n : $($_.Exception.Message)" }
}
$log | Set-Content -Encoding utf8 $out
