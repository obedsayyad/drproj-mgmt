terraform {
  required_version = "~> 1.17.0"

  # Path is supplied by scripts/tf-slot.sh: -backend-config=path=$TF_STATE_DIR/<provider>/<slot>/terraform.tfstate
  backend "local" {}

  required_providers {
    linode = {
      source  = "linode/linode"
      version = "4.8.0"
    }
  }
}

# The token comes only from LINODE_TOKEN in the environment.
provider "linode" {}
