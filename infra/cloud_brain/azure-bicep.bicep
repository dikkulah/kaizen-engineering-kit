// Kaizen Cloud-Brain — Azure Bicep Template (Ampere Altra ARM64, 128 GiB RAM)
//
// Security Invariants:
//   - Zero Public IP resources; private VNet NIC only.
//   - Network Security Group blocks all inbound public Internet traffic (no public SSH).
//   - Optional Bastion-subnet-only SSH rule for private 7-port loopback forwarding.
//   - Managed Identity + 250 GB Premium SSD + cloud-init rootless Podman bootstrap.

targetScope = 'resourceGroup'

@description('Azure region for all Cloud-Brain resources.')
param location string = resourceGroup().location

@description('Virtual Machine resource name.')
param vmName string = 'kaizen-cloud-brain'

@description('ARM64 128 GiB RAM VM size (Standard_E16pls_v5: 16 vCPU / 128 GiB, or Standard_D32pls_v5: 32 vCPU / 128 GiB).')
@allowed([
  'Standard_E16pls_v5'
  'Standard_D32pls_v5'
  'Standard_E32pls_v5'
  'Standard_D32ps_v5'
])
param vmSize string = 'Standard_E16pls_v5'

@description('Optional existing Subnet resource ID; creates a dedicated private VNet + Subnet when empty.')
param existingSubnetId string = ''

@description('Private VNet CIDR used when existingSubnetId is empty.')
param vnetAddressPrefix string = '10.240.0.0/16'

@description('Private workload Subnet CIDR used when existingSubnetId is empty.')
param subnetAddressPrefix string = '10.240.1.0/24'

@description('Optional AzureBastionSubnet CIDR (e.g. 10.240.254.0/26) permitted to initiate private SSH tunnels; leave empty for strict VNet-only rules.')
param bastionSubnetPrefix string = ''

@description('Linux administrator username (SSH public-key authentication only).')
param adminUsername string = 'kaizen'

@description('SSH Ed25519 or RSA public key for private Bastion / VNet tunnel authentication.')
@secure()
param sshPublicKey string

@description('Encrypted Premium SSD OS disk size in GiB for GGUF Sparse-MoE weights and RAG indices.')
@minValue(100)
@maxValue(2048)
param osDiskSizeGb int = 250

@description('Enable systemd kaizen-idle-guard.timer to power down when all 7 tunnels and CPU are idle.')
param enableIdleGuard bool = true

@description('Consecutive idle minutes before automatic clean shutdown.')
@minValue(15)
@maxValue(720)
param idleTimeoutMinutes int = 45

@description('Git repository URL hosting infra/cloud_brain/bootstrap-cloud-brain.sh.')
param bootstrapRepoUrl string = 'https://github.com/kaizen-kit/kaizen-engineering-kit.git'

@description('Git branch or tag checked out during cloud-init provisioning.')
param bootstrapRepoRef string = 'main'

var nsgName = '${vmName}-nsg'
var vnetName = '${vmName}-vnet'
var subnetName = 'snet-cloud-brain'
var nicName = '${vmName}-nic'
var identityName = '${vmName}-identity'
var createPrivateVnet = empty(existingSubnetId)

var bastionAllowRule = empty(bastionSubnetPrefix)
  ? []
  : [
      {
        name: 'AllowPrivateBastionSshOnly'
        properties: {
          description: 'Allow TCP 22 strictly from AzureBastionSubnet for private 7-port loopback forwarding.'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '22'
          sourceAddressPrefix: bastionSubnetPrefix
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 100
          direction: 'Inbound'
        }
      }
    ]

var denyPublicInboundRules = [
  {
    name: 'DenyAllInternetInbound'
    properties: {
      description: 'Explicitly block all public Internet inbound traffic (zero public SSH or API exposure).'
      protocol: '*'
      sourcePortRange: '*'
      destinationPortRange: '*'
      sourceAddressPrefix: 'Internet'
      destinationAddressPrefix: '*'
      access: 'Deny'
      priority: 4000
      direction: 'Inbound'
    }
  }
]

var cloudInitCustomData = '''#cloud-config
package_update: true
packages:
  - ca-certificates
  - curl
  - git

runcmd:
  - |
    set -Eeuo pipefail
    STAGE_DIR="/opt/kaizen-cloud-brain-src"
    rm -rf "${STAGE_DIR}"
    git clone --depth 1 --branch "__REPO_REF__" "__REPO_URL__" "${STAGE_DIR}"
    export KAIZEN_BIND_ADDR="127.0.0.1"
    export KAIZEN_WORKSPACE="/work-space"
    export KAIZEN_MODELS_DIR="/var/lib/kaizen/models"
    export KAIZEN_ENABLE_IDLE_GUARD="__ENABLE_IDLE_GUARD__"
    export KAIZEN_IDLE_MINUTES="__IDLE_MINUTES__"
    export KAIZEN_REPO_URL="__REPO_URL__"
    export KAIZEN_REPO_REF="__REPO_REF__"
    bash "${STAGE_DIR}/infra/cloud_brain/bootstrap-cloud-brain.sh"
'''

var renderedCloudInit = replace(
  replace(
    replace(
      replace(cloudInitCustomData, '__REPO_URL__', bootstrapRepoUrl),
      '__REPO_REF__',
      bootstrapRepoRef
    ),
    '__ENABLE_IDLE_GUARD__',
    enableIdleGuard ? 'true' : 'false'
  ),
  '__IDLE_MINUTES__',
  string(idleTimeoutMinutes)
)

resource userIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: {
    role: 'kaizen-cloud-brain'
    managedBy: 'kaizen-engineering-kit'
  }
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: nsgName
  location: location
  properties: {
    securityRules: concat(bastionAllowRule, denyPublicInboundRules)
  }
  tags: {
    role: 'kaizen-cloud-brain'
    managedBy: 'kaizen-engineering-kit'
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (createPrivateVnet) {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: subnetName
        properties: {
          addressPrefix: subnetAddressPrefix
          networkSecurityGroup: {
            id: nsg.id
          }
        }
      }
    ]
  }
  tags: {
    role: 'kaizen-cloud-brain'
    managedBy: 'kaizen-engineering-kit'
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-11-01' = {
  name: nicName
  location: location
  properties: {
    networkSecurityGroup: {
      id: nsg.id
    }
    ipConfigurations: [
      {
        name: 'ipconfig-private'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: createPrivateVnet ? vnet.properties.subnets[0].id : existingSubnetId
          }
          // publicIPAddress intentionally omitted: zero public IP exposure.
        }
      }
    ]
  }
  tags: {
    role: 'kaizen-cloud-brain'
    managedBy: 'kaizen-engineering-kit'
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-03-01' = {
  name: vmName
  location: location
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${userIdentity.id}': {}
    }
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server-arm64'
        version: 'latest'
      }
      osDisk: {
        name: '${vmName}-osdisk'
        createOption: 'FromImage'
        caching: 'ReadWrite'
        diskSizeGB: osDiskSizeGb
        deleteOption: 'Detach'
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(renderedCloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: sshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
  tags: {
    role: 'kaizen-cloud-brain'
    managedBy: 'kaizen-engineering-kit'
  }
}

output vmId string = vm.id
output deployedVmName string = vm.name
output privateIpAddress string = nic.properties.ipConfigurations[0].properties.privateIPAddress
output nsgId string = nsg.id
output userAssignedIdentityId string = userIdentity.id
output systemAssignedPrincipalId string = vm.identity.principalId
output bastionSshTunnelCommand string = 'az network bastion ssh --name <bastion-name> --resource-group ${resourceGroup().name} --target-resource-id ${vm.id} --auth-type ssh-key --username ${adminUsername} --ssh-key ~/.ssh/id_ed25519 -- -N -L 127.0.0.1:4000:127.0.0.1:4000 -L 127.0.0.1:6070:127.0.0.1:6070 -L 127.0.0.1:8765:127.0.0.1:8765 -L 127.0.0.1:8766:127.0.0.1:8766 -L 127.0.0.1:8770:127.0.0.1:8770 -L 127.0.0.1:18808:127.0.0.1:18808 -L 127.0.0.1:24280:127.0.0.1:24280'
