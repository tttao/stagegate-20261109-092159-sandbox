variable "tag" {
  type = string
}

variable "grants" {
  type = object({
    catalog         = string
    base_generation = string
    principal       = string
    privileges      = list(string)
  })
}

resource "terraform_data" "grants" {
  input = {
    tag    = var.tag
    grants = var.grants
  }
}
