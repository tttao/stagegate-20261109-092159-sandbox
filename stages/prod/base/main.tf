variable "marker" {
  type = string
}

variable "generation" {
  type = string
}

# Created once and then stable.
resource "random_pet" "catalog" {
  length = 2
}

resource "terraform_data" "marker" {
  input = var.marker
}

resource "terraform_data" "generation" {
  input = var.generation
}

output "catalog" {
  value = random_pet.catalog.id
}

output "generation" {
  value = terraform_data.generation.input
}
