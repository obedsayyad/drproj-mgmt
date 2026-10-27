mock_provider "digitalocean" {}

variables {
  name       = "elastic-do-1"
  region     = "r1"
  size       = "s1"
  image      = "i1"
  ssh_key_id = "k1"
  tags = {
    project = "cdr"
    slot    = "elastic-do-1"
    op      = "11111111-1111-1111-1111-111111111111"
    attempt = "1"
  }
}

run "one_droplet_with_flat_tags" {
  command = plan

  assert {
    condition     = digitalocean_droplet.this.name == "elastic-do-1"
    error_message = "name must be the slot key"
  }
  assert {
    condition = toset(digitalocean_droplet.this.tags) == toset([
      "project:cdr", "slot:elastic-do-1", "op:11111111-1111-1111-1111-111111111111", "attempt:1",
    ])
    error_message = "all four project tags must be present"
  }
}
