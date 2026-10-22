# The VPC resolver answers queries for any name, and a security group cannot block traffic to it,
# so without this file DNS would be a way out of the private subnets. DNS Firewall answers only
# the names this design uses, refuses every other name, and stays closed if the firewall itself
# cannot answer.

resource "aws_route53_resolver_firewall_domain_list" "allowed" {
  name = "${var.name}-allowed"
  domains = [
    "bedrock-runtime.${var.region}.amazonaws.com",
    "logs.${var.region}.amazonaws.com",
    aws_rds_cluster.lab.endpoint,
    aws_rds_cluster.lab.reader_endpoint,
  ]
}

resource "aws_route53_resolver_firewall_domain_list" "everything" {
  name    = "${var.name}-everything"
  domains = ["*"]
}

resource "aws_route53_resolver_firewall_rule_group" "lab" {
  name = var.name
}

resource "aws_route53_resolver_firewall_rule" "allow_named" {
  name                    = "allow-named"
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.lab.id
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.allowed.id
  priority                = 100
  action                  = "ALLOW"

  # The database endpoints are CNAMEs to instance names; trust the chain an allowed name starts.
  firewall_domain_redirection_action = "TRUST_REDIRECTION_DOMAIN"
}

resource "aws_route53_resolver_firewall_rule" "block_the_rest" {
  name                    = "block-the-rest"
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.lab.id
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.everything.id
  priority                = 200
  action                  = "BLOCK"
  block_response          = "NXDOMAIN"
}

resource "aws_route53_resolver_firewall_rule_group_association" "lab" {
  name                   = var.name
  firewall_rule_group_id = aws_route53_resolver_firewall_rule_group.lab.id
  vpc_id                 = aws_vpc.lab.id
  priority               = 101
  mutation_protection    = "ENABLED"
}

resource "aws_route53_resolver_firewall_config" "lab" {
  resource_id        = aws_vpc.lab.id
  firewall_fail_open = "DISABLED"
}
