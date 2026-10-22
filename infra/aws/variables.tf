variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "closed-network-ai-lab"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name))
    error_message = "Use 3 to 31 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "region" {
  description = "AWS Region. Seoul by default, for data that has to stay in Korea."
  type        = string
  default     = "ap-northeast-2"
}

variable "availability_zones" {
  description = "Two or three Availability Zones in the Region; one private subnet of each tier per zone."
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]

  validation {
    condition     = length(var.availability_zones) >= 2 && length(var.availability_zones) <= 3
    error_message = "Give two or three Availability Zones."
  }
}

variable "vpc_cidr" {
  description = "Address range of the VPC; the subnets are /24 blocks inside it."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && split("/", var.vpc_cidr)[1] == "16"
    error_message = "Give an IPv4 /16 block: AWS allows no larger VPC, and the /24 subnets need all of it."
  }
}

variable "model_ids" {
  description = "Bedrock foundation model IDs the agent may invoke, for example an embedding model and a chat model. Look them up with `aws bedrock list-foundation-models --region <region>`; nothing else can be invoked through the endpoint."
  type        = list(string)

  validation {
    condition     = length(var.model_ids) > 0 && alltrue([for id in var.model_ids : can(regex("^[a-z0-9-]+\\.[a-z0-9.:-]+$", id))])
    error_message = "Give at least one foundation model ID such as provider.model-name:version."
  }
}

variable "db_engine_version" {
  description = "Aurora PostgreSQL version. 17.11 ships pgvector 0.8.2 (AWS release notes, checked 2026-10-22); the lab runs PostgreSQL 17.11."
  type        = string
  default     = "17.11"
}

variable "db_max_capacity" {
  description = "Upper bound of the Aurora Serverless v2 scaling range, in ACUs."
  type        = number
  default     = 2

  validation {
    condition     = var.db_max_capacity >= 1 && var.db_max_capacity <= 16 && floor(var.db_max_capacity * 2) == var.db_max_capacity * 2
    error_message = "Keep the upper bound between 1 and 16 ACUs, in steps of 0.5."
  }
}

variable "log_retention_days" {
  description = "How long every log group keeps its records."
  type        = number
  default     = 400

  validation {
    condition     = contains([365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "Use a CloudWatch Logs retention of one year or more."
  }
}

variable "alarm_emails" {
  description = "Addresses the alarm topic emails. Each must confirm its subscription before it receives anything."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for e in var.alarm_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", e))])
    error_message = "Give email addresses."
  }
}
