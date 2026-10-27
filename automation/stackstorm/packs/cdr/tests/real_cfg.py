"""Minimal valid `real_adapter` pack settings for tests that only need the real adapter to load (no call reaches a provider)."""
REAL_CFG = {
    "tf_slot_script": "/nonexistent/tf-slot.sh", "tf_state_dir": "/nonexistent/state",
    "digitalocean": {"token_env": "env://X_DO", "region": "r", "size": "s", "image": "i", "ssh_key_id_env": "env://X_DOK"},
    "linode": {"token_env": "env://X_LI", "region": "r", "size": "s", "image": "i", "ssh_key_id_env": "env://X_LIK"},
}
