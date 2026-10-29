# Runner image for the StackStorm actionrunner (docs/07 "Runner packaging", docs/08).
# Pins mirror versions.* in docs/config/deployment-inputs.example.json. The build refuses empty values,
# so nothing is silently "latest". Build context is the drproj-mgmt repo root:
#   docker build --platform linux/amd64 -f automation/runner/Dockerfile -t drproj-runner:local .
# Provider plugins are pre-fetched into a filesystem mirror at build time (the only network step);
# at runtime `terraform init` resolves them from the mirror, so the runner needs no registry access.
ARG ST2_TAG=3.9.0
FROM stackstorm/st2actionrunner:${ST2_TAG}

ARG TERRAFORM_VERSION=1.17.0
ARG TERRAFORM_SHA256=c168cab7c637a8769fdb90089ddd19de701bc6ff8aca64dc2e2185fe46f59f67
ARG ANSIBLE_CORE_VERSION=2.17.14
ARG PG_CLIENT_MAJOR=17

USER root
RUN test -n "$TERRAFORM_VERSION" && test -n "$TERRAFORM_SHA256" && test -n "$ANSIBLE_CORE_VERSION" && test -n "$PG_CLIENT_MAJOR" \
 || { echo "set TERRAFORM_VERSION, TERRAFORM_SHA256, ANSIBLE_CORE_VERSION and PG_CLIENT_MAJOR (versions.* in deployment inputs)" >&2; exit 1; }

# Terraform: verify the release zip against the pinned checksum before installing (linux_amd64 only).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip gnupg lsb-release python3-venv openssh-client \
 && curl -fsSLo /tmp/tf.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
 && echo "${TERRAFORM_SHA256}  /tmp/tf.zip" | sha256sum -c - \
 && unzip -q /tmp/tf.zip -d /usr/local/bin terraform && rm /tmp/tf.zip

# Provider plugins: mirror exactly the versions pinned in both per-provider slot roots (the mirror holds both; each root loads only its own),
# then forbid direct downloads.
COPY infra/terraform/slot-do/versions.tf /tmp/tfmirror/do/versions.tf
COPY infra/terraform/slot-linode/versions.tf /tmp/tfmirror/linode/versions.tf
RUN for d in do linode; do (cd /tmp/tfmirror/$d && terraform providers mirror -platform=linux_amd64 /opt/terraform/plugins); done \
 && rm -rf /tmp/tfmirror \
 && printf 'provider_installation {\n  filesystem_mirror {\n    path    = "/opt/terraform/plugins"\n    include = ["registry.terraform.io/*/*"]\n  }\n  direct {\n    exclude = ["registry.terraform.io/*/*"]\n  }\n}\n' > /etc/terraform.rc \
 && chmod -R a+rX /opt/terraform /etc/terraform.rc
ENV TF_CLI_CONFIG_FILE=/etc/terraform.rc TF_IN_AUTOMATION=1

# Ansible in its own venv (st2's venv is left alone). ansible-core must support the image's Python (3.10).
COPY automation/runner/requirements.yml /tmp/requirements.yml
RUN python3 -m venv /opt/ansible && /opt/ansible/bin/pip install --no-cache-dir "ansible-core==${ANSIBLE_CORE_VERSION}" "psycopg[binary]==3.3.6" \
 && /opt/ansible/bin/ansible-galaxy collection install -p /opt/ansible/collections -r /tmp/requirements.yml \
 && rm /tmp/requirements.yml \
 && for b in ansible ansible-playbook ansible-galaxy ansible-inventory; do ln -s /opt/ansible/bin/$b /usr/local/bin/$b; done \
 && chmod -R a+rX /opt/ansible
ENV ANSIBLE_COLLECTIONS_PATH=/opt/ansible/collections
# Playbooks, roles and the cloud_app dynamic inventory (psycopg above is for inventory/cloud_app.py). Pack setting real_adapter.ansible.playbook_dir.
COPY infra/ansible /opt/drproj/ansible
RUN chmod -R a+rX /opt/drproj && chmod a+x /opt/drproj/ansible/inventory/cloud_app.py

# Guarded Terraform wrapper and the two per-provider slot roots (+ modules). tf-slot.sh resolves ../infra/terraform/<root> from its own path, so
# the layout under /opt/drproj mirrors the repository. `terraform init` writes .terraform/ into the root it runs in, so st2 owns the roots; the
# state itself lives in the TF_STATE_DIR mount (compose.ansible.yaml), never in the image.
COPY scripts/tf-slot.sh /opt/drproj/scripts/tf-slot.sh
COPY infra/terraform /opt/drproj/infra/terraform
RUN chmod a+x /opt/drproj/scripts/tf-slot.sh && rm -rf /opt/drproj/infra/terraform/*/tests /opt/drproj/infra/terraform/modules/*/tests \
 && chown -R st2:st2 /opt/drproj/infra/terraform && chmod -R a+rX /opt/drproj

# PostgreSQL client from PGDG, same major as the Supabase server (postgres_server_major) for pg_dump/pg_restore.
RUN install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSLo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends "postgresql-client-${PG_CLIENT_MAJOR}" \
 && rm -rf /var/lib/apt/lists/*

USER st2
