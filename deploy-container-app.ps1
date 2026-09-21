param(
    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    [Parameter(Mandatory)]
    [string]$Location,

    [Parameter(Mandatory)]
    [string]$McpAuthToken,

    [string]$MoodleToken = "",
    [string]$AppName = "moodle-mcp-$((Get-Random -Minimum 10000 -Maximum 99999))",
    [string]$EnvironmentName = "$AppName-env",
    [string]$MoodleUrl = "https://lt1.insuranceinstitute.ca"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it from https://aka.ms/installazurecliwindows and run az login."
}

# az.exe returns a non-zero exit code on failure but PowerShell does not treat
# that as a terminating error by default, so check $LASTEXITCODE explicitly.
function Invoke-Az {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "az $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

Invoke-Az @("account", "show", "--output", "none")

# The current identity may only have Contributor scoped to specific existing
# resource groups, so avoid a group-create call unless the group is missing.
$groupExists = (az group exists --name $ResourceGroup) -eq "true"
if (-not $groupExists) {
    Invoke-Az @("group", "create", "--name", $ResourceGroup, "--location", $Location, "--output", "none")
}

# Builds the Dockerfile remotely and creates the Container Apps environment if needed.
# 'up' manages its own progress output and does not accept --output.
Invoke-Az @(
    "containerapp", "up",
    "--name", $AppName,
    "--resource-group", $ResourceGroup,
    "--location", $Location,
    "--environment", $EnvironmentName,
    "--source", ".",
    "--ingress", "external",
    "--target-port", "8000",
    "--env-vars", "MCP_TRANSPORT=streamable-http", "MOODLE_URL=$MoodleUrl"
)

# min-replicas 0 + smallest CPU/memory keeps this on the Consumption plan's free
# monthly grant (180k vCPU-s, 360k GiB-s, 2M requests) for light/intermittent use.
Invoke-Az @(
    "containerapp", "update",
    "--name", $AppName,
    "--resource-group", $ResourceGroup,
    "--min-replicas", "0",
    "--max-replicas", "1",
    "--cpu", "0.25",
    "--memory", "0.5Gi",
    "--output", "none"
)

# MoodleToken is optional: when omitted, leave the existing moodle-token secret
# (e.g. one set manually via the Azure portal) completely untouched.
# NOTE: --set-env-vars REPLACES the whole env var list, so every call below
# must always include MCP_TRANSPORT/MOODLE_URL or they get silently dropped.
if ($MoodleToken) {
    Invoke-Az @(
        "containerapp", "secret", "set",
        "--name", $AppName,
        "--resource-group", $ResourceGroup,
        "--secrets", "moodle-token=$MoodleToken", "mcp-auth-token=$McpAuthToken",
        "--output", "none"
    )

    Invoke-Az @(
        "containerapp", "update",
        "--name", $AppName,
        "--resource-group", $ResourceGroup,
        "--set-env-vars",
        "MCP_TRANSPORT=streamable-http",
        "MOODLE_URL=$MoodleUrl",
        "MOODLE_TOKEN=secretref:moodle-token",
        "MCP_AUTH_TOKEN=secretref:mcp-auth-token",
        "--output", "none"
    )
} else {
    Invoke-Az @(
        "containerapp", "secret", "set",
        "--name", $AppName,
        "--resource-group", $ResourceGroup,
        "--secrets", "mcp-auth-token=$McpAuthToken",
        "--output", "none"
    )

    # Preserve whatever moodle-token secret reference already exists (e.g. set
    # manually via the portal) instead of assuming it is present.
    $existingSecretRefs = az containerapp show --name $AppName --resource-group $ResourceGroup --query "properties.configuration.secrets[].name" --output tsv
    $envVars = @("MCP_TRANSPORT=streamable-http", "MOODLE_URL=$MoodleUrl", "MCP_AUTH_TOKEN=secretref:mcp-auth-token")
    if ($existingSecretRefs -contains "moodle-token") {
        $envVars += "MOODLE_TOKEN=secretref:moodle-token"
    }

    Invoke-Az (@(
        "containerapp", "update",
        "--name", $AppName,
        "--resource-group", $ResourceGroup,
        "--set-env-vars"
    ) + $envVars + @("--output", "none"))
}

$fqdn = az containerapp show `
    --name $AppName `
    --resource-group $ResourceGroup `
    --query "properties.configuration.ingress.fqdn" `
    --output tsv
if ($LASTEXITCODE -ne 0 -or -not $fqdn) {
    throw "Could not read the container app's FQDN; deployment may have failed."
}

Write-Host "MCP endpoint: https://$fqdn/mcp"