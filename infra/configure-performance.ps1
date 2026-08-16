param(
    [string]$ResourceGroup = "RMSResurs1",
    [string]$FunctionApp = "los-functions",
    [ValidateRange(1, 1000)]
    [int]$MaximumInstances = 10,
    [ValidateRange(1, 1000)]
    [int]$HttpConcurrency = 4,
    [ValidateSet(512, 2048, 4096)]
    [int]$InstanceMemoryMB = 2048
)

$ErrorActionPreference = "Stop"

az functionapp scale config set `
    --name $FunctionApp `
    --resource-group $ResourceGroup `
    --maximum-instance-count $MaximumInstances `
    --instance-memory $InstanceMemoryMB `
    --trigger-type http `
    --trigger-settings "perInstanceConcurrency=$HttpConcurrency" `
    --output none

az functionapp config appsettings set `
    --name $FunctionApp `
    --resource-group $ResourceGroup `
    --settings `
        APP_DB_POOL_MAX_SIZE=4 `
        INTEGRATION_DB_POOL_MAX_SIZE=4 `
        APP_DB_POOL_MIN_SIZE=1 `
        INTEGRATION_DB_POOL_MIN_SIZE=1 `
        DB_POOL_ACQUIRE_TIMEOUT_SECONDS=10 `
        DB_POOL_MAX_WAITING=16 `
        DB_POOL_MAX_IDLE_SECONDS=1800 `
        DB_POOL_MAX_LIFETIME_SECONDS=1800 `
        DB_CONNECT_TIMEOUT_SECONDS=10 `
        IMPORT_MAX_DEQUEUE_COUNT=3 `
    --output none

# Both MAX_SIZE values deliberately equal $HttpConcurrency. Each in-flight HTTP
# request holds at most one connection per database, so matching the two means
# no request ever waits on a pool slot, and the ceiling across the app stays
# $MaximumInstances * 4 per database. Raise $HttpConcurrency and these must rise
# with it, or a burst starts timing out at DB_POOL_ACQUIRE_TIMEOUT_SECONDS
# instead of queueing at the front door.
#
# MIN_SIZE=1 keeps one connection warm so an ordinary page load does not pay the
# TLS+SCRAM handshake, and MAX_IDLE 1800 stops it being reaped between visits.
# It only pays off with an always-ready instance, which the Flex Consumption
# plan does not have here - without a resident worker there is no process to
# hold the warm connection. Set one to collect the benefit:
#
#   az functionapp scale config set --name $FunctionApp `
#       --resource-group $ResourceGroup --always-ready-instances http=1

az functionapp scale config show `
    --name $FunctionApp `
    --resource-group $ResourceGroup `
    --output json
