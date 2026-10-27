variable "provider_name" {
  description = "Must be linode: this root declares only the linode provider (per-provider failure boundary)."
  type        = string
  validation {
    condition     = var.provider_name == "linode"
    error_message = "this root manages linode only; provider_name must be linode."
  }
}

variable "slot" {
  description = "Capacity slot key (ops.capacity_slots)."
  type        = string
  validation {
    condition = contains(concat(
      ["warm", "interim-warm-digitalocean", "interim-warm-linode"],
      [for i in range(1, 6) : "elastic-do-${i}"],
      [for i in range(1, 6) : "elastic-linode-${i}"],
    ), var.slot)
    error_message = "slot must be one of the 13 capacity slot keys."
  }
  # The provider must own the slot (elastic-do-* / interim-warm-digitalocean are DigitalOcean, the linode ones Linode).
  validation {
    condition = (
      (startswith(var.slot, "elastic-do-") || var.slot == "interim-warm-digitalocean" ? var.provider_name == "digitalocean" : true) &&
      (startswith(var.slot, "elastic-linode-") || var.slot == "interim-warm-linode" ? var.provider_name == "linode" : true)
    )
    error_message = "slot does not belong to provider_name (elastic-do-* and interim-warm-digitalocean are digitalocean; elastic-linode-* and interim-warm-linode are linode)."
  }
}

variable "warm_provider" {
  description = "capacity.warm_provider. The warm slot key follows it, so for slot=warm it must equal provider_name. Null for every other slot."
  type        = string
  default     = null
  validation {
    condition     = var.warm_provider == null || contains(["digitalocean", "linode"], var.warm_provider)
    error_message = "warm_provider must be digitalocean or linode."
  }
  validation {
    condition     = var.slot != "warm" || var.warm_provider == var.provider_name
    error_message = "the warm slot requires warm_provider equal to provider_name."
  }
}

variable "exists" {
  description = "Declared desired existence. false deletes the instance through apply; never terraform destroy."
  type        = bool
}

variable "op" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.op))
    error_message = "op must be an operation UUID."
  }
}

variable "attempt" {
  type = number
  validation {
    condition     = var.attempt >= 1 && floor(var.attempt) == var.attempt
    error_message = "attempt must be an integer >= 1."
  }
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
  type = string
}

locals {
  tags = {
    project = "cdr"
    slot    = var.slot
    op      = var.op
    attempt = tostring(var.attempt)
  }
}

module "linode_instance" {
  source     = "../modules/linode_instance"
  count      = var.exists ? 1 : 0
  name       = var.slot
  region     = var.region
  size       = var.size
  image      = var.image
  ssh_key_id = var.ssh_key_id
  tags       = local.tags
}

locals {
  instance = one(module.linode_instance[*])
}

output "resource_id" {
  value = try(local.instance.resource_id, null)
}

output "public_ip" {
  value = try(local.instance.public_ip, null)
}

output "provider_state" {
  value = try(local.instance.provider_state, null)
}

output "tags" {
  value = local.tags
}

# The tag strings the provider resource actually carries (module-rendered), null while the instance does not exist.
output "provider_tags" {
  value = try(local.instance.provider_tags, null)
}

# The warm provider this apply was validated against (null for non-warm slots).
output "warm_provider" {
  value = var.warm_provider
}
