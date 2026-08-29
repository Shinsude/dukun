$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:MODEL_API_KEY = [Environment]::GetEnvironmentVariable('MODEL_API_KEY', 'User')
$ws = Join-Path $env:TEMP 'mantra-console-live'
$msg = 'Create a Python file hello.py that defines h() returning hi. Then verify your work by running a Python command that imports hello, calls h, and asserts the result equals hi. Report the exact command you ran and its output.'
python console.py --workspace $ws --once $msg
"exit=$LASTEXITCODE"
"hello_py_exists=$(Test-Path (Join-Path $ws 'hello.py'))"
