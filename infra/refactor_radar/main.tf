terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  default = "eu-west-1"
}

variable "subnet_id" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "instance_type" {
  default = "g5.2xlarge"
}

variable "spot_max_price" {
  default = "0.75"
}

variable "auto_shutdown_minutes" {
  default = 45
}

resource "aws_s3_bucket" "capsules" {
  bucket_prefix = "refactor-radar-capsules-"
  force_destroy = false
}

resource "aws_s3_bucket_server_side_encryption_configuration" "capsules_enc" {
  bucket = aws_s3_bucket.capsules.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_iam_role" "radar_ec2_role" {
  name_prefix = "refactor-radar-ec2-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.radar_ec2_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "s3_capsule_rw" {
  name = "s3-capsule-rw"
  role = aws_iam_role.radar_ec2_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
      Resource = [aws_s3_bucket.capsules.arn, "${aws_s3_bucket.capsules.arn}/*"]
    }]
  })
}

resource "aws_iam_instance_profile" "radar_profile" {
  name_prefix = "refactor-radar-profile-"
  role        = aws_iam_role.radar_ec2_role.name
}

resource "aws_security_group" "radar_sg" {
  name_prefix = "refactor-radar-isolated-"
  description = "Zero inbound; outbound HTTPS for container/model pull and S3/SSM"
  vpc_id      = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_ami" "dl_gpu_ubuntu" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04) *"]
  }
}

resource "aws_instance" "radar_worker" {
  ami                                  = data.aws_ami.dl_gpu_ubuntu.id
  instance_type                        = var.instance_type
  subnet_id                            = var.subnet_id
  vpc_security_group_ids               = [aws_security_group.radar_sg.id]
  iam_instance_profile                 = aws_iam_instance_profile.radar_profile.name
  instance_initiated_shutdown_behavior = "terminate"

  instance_market_options {
    market_type = "spot"
    spot_options {
      max_price                      = var.spot_max_price
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  root_block_device {
    volume_size = 120
    volume_type = "gp3"
    iops        = 6000
    throughput  = 400
    encrypted   = true
  }

  user_data = <<-EOF
    #!/usr/bin/env bash
    set -euo pipefail
    systemd-run --on-active=${var.auto_shutdown_minutes}m /sbin/shutdown -h now || true
  EOF

  tags = {
    Name    = "refactor-radar-gpu-spot"
    Project = "refactor-radar"
  }
}

output "capsule_bucket" {
  value = aws_s3_bucket.capsules.bucket
}

output "ssm_connect_command" {
  value = "aws ssm start-session --target ${aws_instance.radar_worker.id}"
}

