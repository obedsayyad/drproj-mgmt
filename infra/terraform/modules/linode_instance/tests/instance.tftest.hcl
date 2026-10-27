mock_provider "linode" {}

variables {
  name       = "elastic-linode-1"
  region     = "r1"
  size       = "s1"
  image      = "i1"
  ssh_key_id = "k1"
  tags = {
    project = "cdr"
    slot    = "elastic-linode-1"
    op      = "11111111-1111-1111-1111-111111111111"
    attempt = "2"
  }
}

run "one_instance_with_flat_tags" {
  command = plan

  assert {
    condition     = linode_instance.this.label == "elastic-linode-1"
    error_message = "label must be the slot key"
  }
  assert {
    condition = toset(linode_instance.this.tags) == toset([
      "project:cdr", "slot:elastic-linode-1", "op:11111111-1111-1111-1111-111111111111", "attempt:2",
    ])
    error_message = "all four project tags must be present"
  }
}
