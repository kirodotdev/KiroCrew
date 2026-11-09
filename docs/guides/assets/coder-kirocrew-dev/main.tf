# kirocrew-dev: a Coder template for developing the Kiro Crew repository from a
# Session Environments chat. It runs one Docker container from build/Dockerfile
# (Python, Node and Semgrep at the repository's own versions) with a persistent
# home volume. Guide: docs/guides/coder-kirocrew-dev-template.md.
#
# Push it:  coder templates push kirocrew-dev -d docs/guides/assets/coder-kirocrew-dev
#
# Kiro Crew creates workspaces with `coder create --use-parameter-defaults`, so
# the template has no parameter without a default.

terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = "~> 2.20"
    }
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.6"
    }
  }
}

variable "docker_socket" {
  default     = ""
  description = "(Optional) Docker socket URI"
  type        = string
}

provider "docker" {
  host = var.docker_socket != "" ? var.docker_socket : null
}

data "coder_provisioner" "me" {}
data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

resource "coder_agent" "main" {
  arch = data.coder_provisioner.me.arch
  os   = "linux"
  dir  = "/home/coder"

  # Commits made in the workspace carry the Coder user's name and email. No
  # credential is set here: git reaches GitHub through the Kiro Crew gateway.
  env = {
    GIT_AUTHOR_NAME     = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_AUTHOR_EMAIL    = data.coder_workspace_owner.me.email
    GIT_COMMITTER_NAME  = coalesce(data.coder_workspace_owner.me.full_name, data.coder_workspace_owner.me.name)
    GIT_COMMITTER_EMAIL = data.coder_workspace_owner.me.email
  }

  metadata {
    display_name = "Home Disk"
    key          = "home_disk"
    script       = "coder stat disk --path $${HOME}"
    interval     = 60
    timeout      = 1
  }
}

# One image per build/ content, shared by every workspace on the Docker host.
# The tag changes whenever a file under build/ changes, so a pushed template
# version with a new Dockerfile or lock rebuilds instead of reusing a stale tag.
locals {
  build_hash = sha1(join("", [for f in sort(fileset("${path.module}/build", "**")) : filesha1("${path.module}/build/${f}")]))
}

resource "docker_image" "main" {
  name = "kirocrew-dev:${substr(local.build_hash, 0, 12)}"
  build {
    context = "${path.module}/build"
  }
  triggers = {
    build_hash = local.build_hash
  }
  keep_locally = true
}

# /home/coder survives stop and start (the checkout, .venv, node_modules); the
# toolchain lives in the image, outside it.
resource "docker_volume" "home_volume" {
  name = "coder-${data.coder_workspace.me.id}-home"
  lifecycle {
    ignore_changes = all
  }
  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.owner_id"
    value = data.coder_workspace_owner.me.id
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
}

resource "docker_container" "workspace" {
  count    = data.coder_workspace.me.start_count
  image    = docker_image.main.image_id
  name     = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  hostname = data.coder_workspace.me.name
  # The image's user is coder (uid 1000); the agent runs as that user.
  entrypoint = ["sh", "-c", replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal")]
  env        = ["CODER_AGENT_TOKEN=${coder_agent.main.token}"]
  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }
  volumes {
    container_path = "/home/coder"
    volume_name    = docker_volume.home_volume.name
    read_only      = false
  }
  labels {
    label = "coder.owner"
    value = data.coder_workspace_owner.me.name
  }
  labels {
    label = "coder.owner_id"
    value = data.coder_workspace_owner.me.id
  }
  labels {
    label = "coder.workspace_id"
    value = data.coder_workspace.me.id
  }
  labels {
    label = "coder.workspace_name"
    value = data.coder_workspace.me.name
  }
}
