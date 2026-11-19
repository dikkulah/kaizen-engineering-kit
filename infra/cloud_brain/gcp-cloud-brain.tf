# Kaizen Cloud-Brain — Google Cloud Platform (Axion / Tau ARM64, 128 GiB RAM)
#
# Security Invariants:
#   - Zero public external IP address on the Compute Engine NIC.
#   - Firewall ingress restricted strictly to Google Identity-Aware Proxy (IAP)
#     TCP forwarding CIDR 35.235.240.0/20 targeting tag 'kaizen-cloud-brain'.
#   - Rootless Podman binds all 7 Cloud-Brain services to 127.0.0.1 inside the VM.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

variable "project_id" {
  type        = string
  description = "Target GCP project ID."
}

variable "region" {
  type        = string
  default     = "europe-west1"
  description = "GCP region for the Cloud-Brain instance."
}

variable "zone" {
  type        = string
  default     = "europe-west1-b"
  description = "GCP zone supporting ARM64 machine series (c4a-standard-32 or t2a-standard-32)."
}

variable "instance_name" {
  type        = string
  default     = "kaizen-cloud-brain"
  description = "Compute Engine instance name."
}

variable "network" {
  type        = string
  default     = "default"
  description = "VPC network name or self_link."
}

variable "subnetwork" {
  type        = string
  default     = ""
  description = "Optional VPC subnetwork name or self_link (uses network auto-subnet when empty)."
}

variable "machine_type" {
  type        = string
  default     = "c4a-standard-32"
  description = "ARM64 32-vCPU / 128 GB RAM SKU (c4a-standard-32 Axion or t2a-standard-32 Ampere Altra)."

  validation {
    condition = contains([
      "c4a-standard-32",
      "t2a-standard-32",
      "c4a-highmem-32",
      "n2-standard-32"
    ], var.machine_type)
    error_message = "machine_type must be a 128 GB RAM SKU such as c4a-standard-32 or t2a-standard-32."
  }
}

variable "disk_size_gb" {
  type        = number
  default     = 250
  description = "Boot SSD size in GB for GGUF Sparse-MoE weights, Zoekt index, and LanceDB."
}

variable "disk_type" {
  type        = string
  default     = "pd-ssd"
  description = "Boot disk type (pd-ssd for t2a/n2 series, or hyperdisk-balanced when required by c4a zones)."
}

variable "gcs_model_bucket" {
  type        = string
  default     = ""
  description = "Optional GCS bucket name containing pre-staged GGUF models under models/."
}

variable "enable_idle_guard" {
  type        = bool
  default     = true
  description = "Enable systemd kaizen-idle-guard.timer to stop the VM when tunnels and CPU are idle."
}

variable "idle_timeout_minutes" {
  type        = number
  default     = 45
  description = "Consecutive idle minutes before automatic clean shutdown."
}

variable "bootstrap_repo_url" {
  type        = string
  default     = "https://github.com/kaizen-kit/kaizen-engineering-kit.git"
  description = "Git repository URL hosting infra/cloud_brain/bootstrap-cloud-brain.sh."
}

variable "bootstrap_repo_ref" {
  type        = string
  default     = "main"
  description = "Git branch or tag to clone during startup."
}

resource "google_service_account" "cloud_brain" {
  account_id   = "${var.instance_name}-sa"
  display_name = "Kaizen Cloud-Brain Least-Privilege VM Service Account"
}

resource "google_project_iam_member" "cloud_brain_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloud_brain.email}"
}

resource "google_project_iam_member" "cloud_brain_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.cloud_brain.email}"
}

resource "google_storage_bucket_iam_member" "cloud_brain_models_reader" {
  count  = var.gcs_model_bucket != "" ? 1 : 0
  bucket = var.gcs_model_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.cloud_brain.email}"
}

resource "google_compute_firewall" "cloud_brain_iap_only" {
  name        = "${var.instance_name}-allow-iap-only"
  network     = var.network
  direction   = "INGRESS"
  priority    = 1000
  description = "Allow SSH and loopback-tunneled TCP strictly from Google Identity-Aware Proxy (IAP)."

  # Google Cloud IAP TCP forwarding range ONLY — no public CIDR blocks permitted.
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["kaizen-cloud-brain"]

  allow {
    protocol = "tcp"
    ports    = ["22", "4000", "6070", "8765", "8766", "8770", "18808", "24280"]
  }
}

resource "google_compute_instance" "cloud_brain" {
  name                      = var.instance_name
  machine_type              = var.machine_type
  zone                      = var.zone
  tags                      = ["kaizen-cloud-brain"]
  allow_stopping_for_update = true

  boot_disk {
    auto_delete = false
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-arm64"
      size  = var.disk_size_gb
      type  = var.disk_type
    }
  }

  # Private VPC interface only (no access_config block => zero public IP).
  network_interface {
    network    = var.network
    subnetwork = var.subnetwork != "" ? var.subnetwork : null
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
  }

  metadata_startup_script = <<-EOT
    #!/usr/bin/env bash
    set -Eeuo pipefail
    if [[ -f /var/lib/kaizen/.bootstrap-complete ]]; then
      exit 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends ca-certificates curl git

    STAGE_DIR="/opt/kaizen-cloud-brain-src"
    rm -rf "$${STAGE_DIR}"
    git clone --depth 1 --branch "${var.bootstrap_repo_ref}" "${var.bootstrap_repo_url}" "$${STAGE_DIR}"

    export KAIZEN_BIND_ADDR="127.0.0.1"
    export KAIZEN_WORKSPACE="/work-space"
    export KAIZEN_MODELS_DIR="/var/lib/kaizen/models"
    export KAIZEN_GCS_MODEL_BUCKET="${var.gcs_model_bucket}"
    export KAIZEN_ENABLE_IDLE_GUARD="${var.enable_idle_guard ? "true" : "false"}"
    export KAIZEN_IDLE_MINUTES="${var.idle_timeout_minutes}"
    export KAIZEN_REPO_URL="${var.bootstrap_repo_url}"
    export KAIZEN_REPO_REF="${var.bootstrap_repo_ref}"

    bash "$${STAGE_DIR}/infra/cloud_brain/bootstrap-cloud-brain.sh"
    install -d -m 0755 /var/lib/kaizen
    touch /var/lib/kaizen/.bootstrap-complete
  EOT

  service_account {
    email  = google_service_account.cloud_brain.email
    scopes = ["cloud-platform"]
  }

  labels = {
    role       = "kaizen-cloud-brain"
    managed_by = "kaizen-engineering-kit"
  }
}

output "instance_name" {
  description = "Compute Engine instance name."
  value       = google_compute_instance.cloud_brain.name
}

output "internal_ip" {
  description = "Private VPC IPv4 address of the Cloud-Brain VM."
  value       = google_compute_instance.cloud_brain.network_interface[0].network_ip
}

output "service_account_email" {
  description = "Dedicated service account email attached to the Cloud-Brain VM."
  value       = google_service_account.cloud_brain.email
}

output "iap_tunnel_7_ports_command" {
  description = "Forward all 7 loopback Cloud-Brain ports over a single GCP IAP SSH session."
  value = join(" ", [
    "gcloud compute ssh ${google_compute_instance.cloud_brain.name}",
    "--project=${var.project_id}",
    "--zone=${var.zone}",
    "--tunnel-through-iap",
    "-- -N",
    "-L 127.0.0.1:4000:127.0.0.1:4000",
    "-L 127.0.0.1:6070:127.0.0.1:6070",
    "-L 127.0.0.1:8765:127.0.0.1:8765",
    "-L 127.0.0.1:8766:127.0.0.1:8766",
    "-L 127.0.0.1:8770:127.0.0.1:8770",
    "-L 127.0.0.1:18808:127.0.0.1:18808",
    "-L 127.0.0.1:24280:127.0.0.1:24280"
  ])
}
