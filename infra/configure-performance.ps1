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
        DB_POOL_ACQUIRE_TIMEOUT_SECONDS=10 `
        DB_POOL_MAX_WAITING=16 `
        DB_POOL_MAX_IDLE_SECONDS=300 `
        DB_POOL_MAX_LIFETIME_SECONDS=1800 `
        DB_CONNECT_TIMEOUT_SECONDS=10 `
        IMPORT_MAX_DEQUEUE_COUNT=3 `
    --output none

az functionapp scale config show `
    --name $FunctionApp `
    --resource-group $ResourceGroup `
    --output json
