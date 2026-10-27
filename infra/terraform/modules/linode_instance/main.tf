terraform {
  required_version = "~> 1.17.0"

  required_providers {
    linode = {
      source  = "linode/linode"
      version = "4.8.0"
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
  description = "Label of a Linode SSH key already registered in the account."
  type        = string
}

variable "tags" {
  description = "project, slot, op, attempt; rendered as flat key:value provider tags."
  type        = map(string)
}

data "linode_sshkey" "this" {
  label = var.ssh_key_id
}

resource "linode_instance" "this" {
  label           = var.name
  region          = var.region
  type            = var.size
  image           = var.image
  authorized_keys = [data.linode_sshkey.this.ssh_key]
  tags            = sort([for k, v in var.tags : "${k}:${v}"])
}

output "resource_id" {
  value = linode_instance.this.id
}

output "public_ip" {
  value = one(linode_instance.this.ipv4)
}

output "provider_state" {
  value = linode_instance.this.status
}

output "provider_tags" {
  value = linode_instance.this.tags
}
