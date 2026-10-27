terraform {
  required_version = "~> 1.17.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "2.106.0"
    }
  }
}

variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "size" {
  type = string
}

variable "image" {
  type = string
}

variable "ssh_key_id" {
  description = "DigitalOcean SSH key id or fingerprint already registered in the account."
  type        = string
}

variable "tags" {
  description = "project, slot, op, attempt; rendered as flat key:value provider tags."
  type        = map(string)
}

resource "digitalocean_droplet" "this" {
  name     = var.name
  region   = var.region
  size     = var.size
  image    = var.image
  ssh_keys = [var.ssh_key_id]
  tags     = sort([for k, v in var.tags : "${k}:${v}"])
}

output "resource_id" {
  value = digitalocean_droplet.this.id
}

output "public_ip" {
  value = digitalocean_droplet.this.ipv4_address
}

output "provider_state" {
  value = digitalocean_droplet.this.status
}

output "provider_tags" {
  value = digitalocean_droplet.this.tags
}
