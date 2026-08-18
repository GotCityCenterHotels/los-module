param(
    [string]$ResourceGroup = "RMSResurs1",
    [string]$FunctionApp = "los-functions",
    [ValidateRange(1, 1000)]
    [int]$MaximumInstances = 10,
    [ValidateRange(1, 1000)]
    [int]$HttpConcurrency = 4,
    [ValidateSet(512, 2048, 4096)]
    [int]$InstanceMemoryMB = 2048,
    [ValidateRange(0, 20)]
    [int]$AlwaysReadyHttpInstances = 0
)

# $ErrorActionPreference governs PowerShell cmdlets, NOT native executables. az
# is native, so a failed call here used to print its error and let the script
# carry on to the next one - and the summary at the bottom would then show the
# configuration it had failed to apply, which reads as success. Every az call is
# checked explicitly instead.
$ErrorActionPreference = "Stop"

function Invoke-Az {
    param([string]$What, [string[]]$Arguments)

    & az @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed: az exited $LASTEXITCODE"
    }
}

Invoke-Az "scale configuration" @(
    "functionapp", "scale", "config", "set",
    "--name", $FunctionApp,
    "--resource-group", $ResourceGroup,
    "--maximum-instance-count", $MaximumInstances,
    "--instance-memory", $InstanceMemoryMB,
    "--trigger-type", "http",
    "--trigger-settings", "perInstanceConcurrency=$HttpConcurrency",
    "--output", "none"
)

Invoke-Az "application settings" @(
    "functionapp", "config", "appsettings", "set",
    "--name", $FunctionApp,
    "--resource-group", $ResourceGroup,
    "--settings",
    "APP_DB_POOL_MAX_SIZE=4",
    "INTEGRATION_DB_POOL_MAX_SIZE=4",
    "APP_DB_POOL_MIN_SIZE=1",
    "INTEGRATION_DB_POOL_MIN_SIZE=1",
    "DB_POOL_ACQUIRE_TIMEOUT_SECONDS=10",
    "DB_POOL_MAX_WAITING=16",
    "DB_POOL_MAX_IDLE_SECONDS=1800",
    "DB_POOL_MAX_LIFETIME_SECONDS=1800",
    "DB_CONNECT_TIMEOUT_SECONDS=10",
    "COST_PUBLICATION_CACHE_SECONDS=5",
    "COST_DATA_RESPONSE_CACHE_MAX_ENTRIES=8",
    "LOS_PUBLICATION_CACHE_SECONDS=5",
    "LOS_FACTS_RESPONSE_CACHE_MAX_ENTRIES=4",
    "IMPORT_MAX_DEQUEUE_COUNT=3",
    "--output", "none"
)

# Always-ready is opt-in because it creates a standing Azure charge, and it is
# the only thing that removes cold start - 2-3s of platform allocation, blob
# mount, host start, and the azure.functions + psycopg import graph, of which
# only a few hundred ms is application code.
#
# On Flex Consumption this is its own command group. It is NOT a flag on
# `scale config set`: that call rejects --always-ready-instances as an
# unrecognized argument, which is how this script could be run with
# -AlwaysReadyHttpInstances 1 and quietly change nothing at all.
if ($AlwaysReadyHttpInstances -gt 0) {
    Invoke-Az "always-ready instances" @(
        "functionapp", "scale", "config", "always-ready", "set",
        "--name", $FunctionApp,
        "--resource-group", $ResourceGroup,
        "--settings", "http=$AlwaysReadyHttpInstances",
        "--output", "none"
    )
}
else {
    # Explicit, so re-running without the flag actually removes a previously
    # configured instance rather than silently leaving it billing.
    & az functionapp scale config always-ready delete `
        --name $FunctionApp `
        --resource-group $ResourceGroup `
        --setting-names "http" `
        --output none 2>$null
    $global:LASTEXITCODE = 0
}

# POOL SIZING. Both MAX_SIZE values equal $HttpConcurrency, but do NOT read that
# as "no request ever waits on a pool slot" - that invariant is false for
# Database A, and the comment here used to claim it.
#
# One Cost Data request is not one connection. services/cost_data_service.py
# fans its seven dataset queries across _dataset_workers, sized
# min(COST_DATA_QUERY_CONCURRENCY, APP_DB_POOL_MAX_SIZE - 1, 7) = 3, so a single
# /api/costdata/facts holds 3 of the 4 cost_pool connections while it runs, plus
# a checkout for the publication pointer and another for the rulebook. With
# perInstanceConcurrency=4 a second concurrent Cost Data request therefore
# contends, and a third can block up to DB_POOL_ACQUIRE_TIMEOUT_SECONDS.
#
# That is a deliberate ceiling, not an oversight: the fan-out cap exists so one
# request cannot take every connection and stall the other pages. But if you
# raise $HttpConcurrency, raise APP_DB_POOL_MAX_SIZE past it rather than to it,
# and recompute $MaximumInstances * APP_DB_POOL_MAX_SIZE against the server's
# max_connections (429 on the current server) before doing so.
#
# MIN_SIZE=1 is worth setting even with no always-ready instance. Both pools are
# constructed with open=True at module import (cost_database.py, database.py), so
# a min_size of 1 fills the first connection on a background thread while the
# rest of the import graph is still loading - the handshake overlaps startup
# instead of being charged to the first request. An always-ready instance is what
# makes it persist BETWEEN visits; it is not what makes it useful.
#
# Enable always-ready when the latency/cost tradeoff is approved:
#
#   ./infra/configure-performance.ps1 -AlwaysReadyHttpInstances 1

Write-Host "`nScale configuration:"
Invoke-Az "scale configuration read-back" @(
    "functionapp", "scale", "config", "show",
    "--name", $FunctionApp,
    "--resource-group", $ResourceGroup,
    "--output", "json"
)

Write-Host "`nAlways-ready instances:"
& az functionapp scale config always-ready list `
    --name $FunctionApp `
    --resource-group $ResourceGroup `
    --output json
