# Allow only this Sender executable to accept traffic from the local subnet.
# Called by the installer's explicit, elevated local-network setup task.
[CmdletBinding(SupportsShouldProcess = $true)]
param([Parameter(Mandatory = $true)][string]$AppPath)

$ErrorActionPreference = 'Stop'
$senderPath = [System.IO.Path]::GetFullPath($AppPath)
if (-not (Test-Path -LiteralPath $senderPath -PathType Leaf)) {
    throw "Sender executable was not found at $senderPath"
}
if (-not $WhatIfPreference) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Local network setup needs Windows administrator approval. Run this installer task again.'
    }
}

$specifications = @(
    @{ Name = 'SimCoaches-Sender-Local-Web'; DisplayName = 'Sim Coaches Sender - local iPad and simulator web'; Protocol = 'TCP'; LocalPort = @('5001', '5101-5105') },
    @{ Name = 'SimCoaches-Sender-Local-Discovery'; DisplayName = 'Sim Coaches Sender - local simulator discovery'; Protocol = 'UDP'; LocalPort = @('5003', '15003', '25003') }
)
foreach ($specification in $specifications) {
    $parameters = @{
        Name = $specification.Name
        DisplayName = $specification.DisplayName
        Description = 'Lap Time Sender connections from the local subnet only.'
        Program = $senderPath
        Direction = 'Inbound'
        Action = 'Allow'
        Enabled = 'True'
        Profile = 'Any'
        RemoteAddress = 'LocalSubnet'
        Protocol = $specification.Protocol
        LocalPort = $specification.LocalPort
    }
    if ($PSCmdlet.ShouldProcess($senderPath, "Configure local-subnet $($specification.Protocol) ports $($specification.LocalPort -join ', ')") ) {
        $existing = Get-NetFirewallRule -Name $specification.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Set-NetFirewallRule @parameters | Out-Null
        } else {
            New-NetFirewallRule @parameters | Out-Null
        }
    }
}
