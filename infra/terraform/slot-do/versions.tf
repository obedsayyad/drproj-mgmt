terraform {
  required_version = "~> 1.17.0"

  # Path is supplied by scripts/tf-slot.sh: -backend-config=path=$TF_STATE_DIR/<provider>/<slot>/terraform.tfstate
  backend "local" {}

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "2.106.0"
    }
  }
}

# The token comes only from DIGITALOCEAN_TOKEN in the environment.
provider "digitalocean" {}
