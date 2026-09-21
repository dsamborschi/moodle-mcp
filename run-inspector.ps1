param(
    [string]$Server = "moodle"
)

$ErrorActionPreference = "Stop"

$env:npm_config_cache = Join-Path $PSScriptRoot ".npm-cache"
$env:Path = (Join-Path $PSScriptRoot ".tools\uv") + ";$env:Path"

$config = Join-Path $PSScriptRoot "inspector-config.json"

npx @modelcontextprotocol/inspector --config $config --server $Server
