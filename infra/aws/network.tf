# Private subnets only: no internet gateway, no NAT gateway, and no route beyond the VPC.
# This is the lab's internal: true network; the endpoints are its model-gateway, and dns.tf
# closes the one path a route table does not cover.

resource "aws_vpc" "lab" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

# The VPC's default security group gets no rules, so nothing can use it by accident.
resource "aws_default_security_group" "lab" {
  vpc_id = aws_vpc.lab.id
}

# Every subnet here uses the default network ACL, which AWS creates open to 0.0.0.0/0. It now
# admits only the VPC's own range: HTTPS to the endpoints, and the high ports that carry
# PostgreSQL and every reply (an ACL keeps no state). With no route out this changes nothing
# today; it is the layer that still holds if a gateway is added. ACLs do not filter traffic to
# the Amazon DNS server, so the Resolver and its firewall are unaffected.
resource "aws_default_network_acl" "lab" {
  default_network_acl_id = aws_vpc.lab.default_network_acl_id
  subnet_ids             = concat(aws_subnet.app[*].id, aws_subnet.data[*].id)

  ingress {
    rule_no    = 100
    protocol   = "tcp"
    action     = "allow"
    cidr_block = var.vpc_cidr
    from_port  = 443
    to_port    = 443
  }

  ingress {
    rule_no    = 110
    protocol   = "tcp"
    action     = "allow"
    cidr_block = var.vpc_cidr
    from_port  = 1024
    to_port    = 65535
  }

  egress {
    rule_no    = 100
    protocol   = "tcp"
    action     = "allow"
    cidr_block = var.vpc_cidr
    from_port  = 443
    to_port    = 443
  }

  egress {
    rule_no    = 110
    protocol   = "tcp"
    action     = "allow"
    cidr_block = var.vpc_cidr
    from_port  = 1024
    to_port    = 65535
  }
}

resource "aws_subnet" "app" {
  count = length(var.availability_zones)

  vpc_id                  = aws_vpc.lab.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-app-${var.availability_zones[count.index]}" }
}

resource "aws_subnet" "data" {
  count = length(var.availability_zones)

  vpc_id                  = aws_vpc.lab.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-data-${var.availability_zones[count.index]}" }
}

# A route table with only the implicit local route.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.lab.id

  tags = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "app" {
  count = length(aws_subnet.app)

  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "data" {
  count = length(aws_subnet.data)

  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_flow_log" "lab" {
  vpc_id               = aws_vpc.lab.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.lab["flow"].arn
  iam_role_arn         = aws_iam_role.flow_logs.arn

  depends_on = [aws_iam_role_policy.flow_logs]
}

# Security groups reference each other, so every allowed flow is named at both ends.
resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Agent tasks: out to the endpoints and the database only"
  vpc_id      = aws_vpc.lab.id
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-endpoints"
  description = "Interface endpoints: HTTPS in from the agent tasks only"
  vpc_id      = aws_vpc.lab.id
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Aurora: PostgreSQL in from the agent tasks only"
  vpc_id      = aws_vpc.lab.id
}

resource "aws_vpc_security_group_egress_rule" "app_to_endpoints" {
  security_group_id            = aws_security_group.app.id
  description                  = "HTTPS to the interface endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

resource "aws_vpc_security_group_egress_rule" "app_to_db" {
  security_group_id            = aws_security_group.app.id
  description                  = "PostgreSQL to Aurora"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_app" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from the agent tasks"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the agent tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}
