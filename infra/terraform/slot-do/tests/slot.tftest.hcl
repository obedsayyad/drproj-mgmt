mock_provider "digitalocean" {}

variables {
  op         = "11111111-1111-1111-1111-111111111111"
  attempt    = 1
  region     = "r1"
  size       = "s1"
  image      = "i1"
  ssh_key_id = "k1"
}

run "exists_creates_exactly_one" {
  command = apply
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = true
  }
  assert {
    condition     = length(module.do_droplet) == 1
    error_message = "exactly one instance"
  }
  assert {
    condition     = output.resource_id != null && output.public_ip != null && output.provider_state != null && output.tags.slot == "elastic-do-2"
    error_message = "outputs expected when the instance exists"
  }
}

run "warm_slot_follows_warm_provider" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "warm"
    warm_provider = "digitalocean"
    exists        = true
  }
  assert {
    condition     = length(module.do_droplet) == 1 && output.warm_provider == "digitalocean"
    error_message = "warm slot on its own provider creates one instance"
  }
}

run "tags_carry_project_slot_op_attempt" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = true
    attempt       = 3
  }
  assert {
    condition = output.tags == {
      project = "cdr"
      slot    = "elastic-do-2"
      op      = "11111111-1111-1111-1111-111111111111"
      attempt = "3"
    }
    error_message = "tags must be project, slot, op, attempt"
  }
}

run "module_renders_flat_tag_list" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = true
    attempt       = 3
  }
  assert {
    condition     = output.provider_tags == toset(["attempt:3", "op:11111111-1111-1111-1111-111111111111", "project:cdr", "slot:elastic-do-2"])
    error_message = "provider tags must be project, slot, op, attempt as flat key:value strings"
  }
}

run "exists_false_creates_nothing" {
  command = apply
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = false
  }
  assert {
    condition     = length(module.do_droplet) == 0
    error_message = "no instance when exists=false"
  }
  assert {
    condition     = output.resource_id == null && output.public_ip == null && output.provider_state == null
    error_message = "outputs must be null when absent"
  }
}

run "rejects_the_other_provider_name" {
  command = plan
  variables {
    provider_name = "linode"
    slot          = "elastic-do-2"
    exists        = true
  }
  expect_failures = [var.provider_name]
}

run "rejects_unknown_provider" {
  command = plan
  variables {
    provider_name = "aws"
    slot          = "elastic-do-2"
    exists        = true
  }
  expect_failures = [var.provider_name]
}

run "rejects_a_slot_of_the_other_provider" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-linode-1"
    exists        = true
  }
  expect_failures = [var.slot]
}

run "rejects_unknown_slot" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-6"
    exists        = true
  }
  expect_failures = [var.slot]
}

run "rejects_warm_slot_on_the_other_provider" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "warm"
    warm_provider = "linode"
    exists        = true
  }
  expect_failures = [var.warm_provider]
}

run "rejects_warm_slot_without_warm_provider" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "warm"
    exists        = true
  }
  expect_failures = [var.warm_provider]
}

run "rejects_bad_op" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = true
    op            = "not-a-uuid"
  }
  expect_failures = [var.op]
}

run "rejects_attempt_zero" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = true
    attempt       = 0
  }
  expect_failures = [var.attempt]
}

run "rejects_fractional_attempt" {
  command = plan
  variables {
    provider_name = "digitalocean"
    slot          = "elastic-do-2"
    exists        = true
    attempt       = 1.5
  }
  expect_failures = [var.attempt]
}
