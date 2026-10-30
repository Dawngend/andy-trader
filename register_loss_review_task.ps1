# Registers the daily paper loss review task: once a day, hidden window.
$wd = "D:\Andy Trader"
$py = "C:\Python314\python.exe"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew -StartWhenAvailable
$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\conhost.exe" -Argument "--headless `"$py`" run_loss_review.py" -WorkingDirectory $wd
$trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM
Register-ScheduledTask -TaskName "AndyTrader-LossReview" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
