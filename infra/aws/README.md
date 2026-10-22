# The lab's layout on AWS (Terraform, not deployed)

The same design as the local lab, written for AWS in the Seoul Region: private subnets with no
route out, DNS that answers only the names the design uses, a VPC interface endpoint whose policy
is the model allowlist, Aurora PostgreSQL with pgvector as the vector store, and logs without
prompt bodies.

Nothing here has been deployed. Every check below runs offline, with no AWS account, no
credentials and no cost.

| Local lab | Here |
|---|---|
| Docker `internal: true` network `lab_internal` | Private subnets whose route table holds only the local route; no internet gateway, no NAT gateway (`network.tf`) |
| No way out by name either | The VPC resolver is reachable from any subnet and a security group cannot block it, so DNS would carry data out. DNS Firewall answers only the two endpoint names and the two database endpoints, returns NXDOMAIN for every other name, and fails closed; every query, refused ones included, is logged to its own group (`dns.tf`) |
| `model-gateway`, the only way to the models | A `bedrock-runtime` interface endpoint with private DNS (`endpoints.tf`) |
| Gateway path and model allowlist | Endpoint policy: the agent role only, `InvokeModel` and `InvokeModelWithResponseStream` only, the foundation models in `model_ids` only |
| A copied gateway URL is useless outside the lab network | The agent role's own permission to invoke carries `aws:SourceVpce`, so its credentials cannot invoke from outside this VPC's endpoint (`iam.tf`) |
| Audit log: sizes and decisions, never prompt bodies | Bedrock invocation logging with text, image, embedding and video delivery all off (`logging.tf`); what a record then holds is not documented (see Limits), so the per-request decision log stays the agent's own. VPC flow logs record accepted and rejected traffic of the whole VPC as aggregated records |
| `vector-db`: PostgreSQL with pgvector on `lab_internal` | Aurora PostgreSQL Serverless v2 17.11 (pgvector 0.8.2) in the data subnets, TLS required, storage encrypted (`database.tf`) |
| Database password as a compose secret file | RDS generates and keeps the master password in Secrets Manager, so it is never in the code or the state; the agent signs in as `rag` with an IAM token |
| Services may talk only on named ports | Security groups reference each other: the agent tasks reach the endpoints on 443 and the database on 5432, and nothing else; the default group has no rules |

One customer managed KMS key, with rotation on, encrypts the five log groups, the database
storage, its master secret and Performance Insights. One of the five is the group Aurora exports
the PostgreSQL log to; it is created here first, so it gets the key and a retention instead of
RDS's defaults. Every role that writes to a log group may use the key only through CloudWatch
Logs (`kms:ViaService`). Logs are kept 400 days by default.

## Check

Needs Terraform 1.16 or later and trivy. The provider is pinned (`hashicorp/aws` 6.67.0) and its
checksums for macOS arm64 and Linux amd64 and arm64 are in `.terraform.lock.hcl`.

```
cd infra/aws
./check.sh
```

`check.sh` runs `terraform fmt -check`, `init -backend=false`, `validate` and `test`, then trivy's
misconfiguration checks with the rules built into the binary (no rule download). The trivy result
goes to `evidence/iac-check-<UTC>.json`, and any HIGH or CRITICAL finding fails the script.

`terraform test` runs `tests/design.tftest.hcl` against a mock AWS provider. The mock creates
nothing and needs no credentials; it lets the tests read the planned values. Policies are
compared whole, so an added statement fails like a changed one, and every role, key and log group
a policy names has its own fixed ARN in the test, so naming the wrong one fails too. The eight
runs assert:

- the Bedrock endpoint lets the agent role invoke the listed models and nothing else, the logs
  endpoint lets it write its own log group only, both are interface endpoints with private DNS in
  the app subnets, and the role's own permissions are exactly four statements, the model and log ones
  each tied to their endpoint;
- nothing hands out or takes a public address, the store is encrypted with the lab key, uses IAM
  sign-in, keeps its master password under the lab key, resists deletion and uses the parameter
  group that refuses connections without TLS, loads pgAudit for DDL and role changes next to
  `pg_stat_statements` and logs session ends, Performance Insights is on under the lab key, all
  five log groups use the lab key and keep a year or more, and the key rotates and its policy
  admits only the account and CloudWatch Logs for those five groups;
- every subnet uses the route table with only the local route and a network ACL that admits only
  the VPC's own range, port by port, DNS answers only the named list
  and fails closed, and flow logs go to their own group through their own role, which may write
  that group only;
- the security group rules have the named groups, protocol and ports at both ends, and the
  default group stays empty;
- invocation logs carry no prompt, output, image, embedding or video, and go to their group
  through a role that may write that log stream only;
- an empty model list and a VPC larger than AWS allows are refused;
- every DNS query from the VPC is logged to the resolver-queries group, which only the log
  delivery service, acting for this account, may write.

`tests/break_the_design.py` checks that the tests can fail. It breaks a fresh copy of the design
33 ways, one at a time. A break counts as caught only if the copy is still valid Terraform, the
run that covers it fails and the runs before it pass:

```
python3 -I tests/break_the_design.py
```

On 2026-10-22, all 33 breaks were caught. trivy's HIGH and CRITICAL checks caught 4 of the 33: a
public database, an unencrypted database, a subnet handing out public addresses and a database
rule open to any address. The other 29 break this design's own rules, such as which models the
endpoint allows or which names DNS answers, which a general scanner has no way to know.

## Limits

- Not deployed. The checks show what the code says, not what AWS does with it.
- The tests read the resources they name. A new resource added elsewhere, such as an extra
  security group rule, is not seen by them; trivy flags one open to any address, as the break
  cases show.
- Compute is out of scope. The agent role trusts ECS tasks, but there is no task definition or
  cluster. Running tasks in these subnets also needs ECR (`ecr.api`, `ecr.dkr`) interface
  endpoints, an S3 gateway endpoint with an egress rule to its prefix list, a task execution role,
  and those names in the DNS allowlist; none of them is here. The logs endpoint admits the agent
  role only, so the `awslogs` driver would need its principal added.
- The agent role may create log streams and put events, nothing else. `PutLogEvents` now ignores
  sequence tokens, so a writer that creates its stream and treats "already exists" as success needs
  no more; one that calls `DescribeLogStreams` first needs that action added to both the role and
  the logs endpoint policy.
- There is no administrative way in. Setting up the database needs one, such as a one-off task in
  the app subnets that connects as the master user.
- Only foundation-model ARNs are allowed. Models that the Seoul Region serves only through a
  cross-Region inference profile cannot be invoked with this policy, and allowing a profile also
  allows requests to be served from other Regions.
- Look up `model_ids` with `aws bedrock list-foundation-models --region ap-northeast-2`. Which
  embedding models Seoul offers was not checked; the default has no model IDs, so nothing is
  assumed.
- The user guide documents the fields of an invocation record (request id, operation, model id,
  the caller's ARN, token counts, and the input and output bodies) but not which of them remain
  when every body delivery is off. That was not observed, because nothing was invoked.
- The Bedrock API also has an audio delivery setting. Provider 6.67.0 cannot set it, so it stays
  at the API's default, which the API reference does not state.
- Flow logs aggregate traffic, can skip records, and do not record DNS queries to the VPC resolver
  or instance metadata traffic. The Resolver query log covers DNS, but it does not record every
  answer served from cache, and a refused name can itself carry data out, so that group is as
  sensitive as the agent's own log.
- The RDS documentation does not say whether Aurora can export to a log group encrypted with a
  customer managed key. If the export fails on deployment, that group's key is the first thing
  to check.
- pgvector is installed per database: an `rds_superuser` (the master user) runs
  `CREATE EXTENSION vector`, creates the `rag` role with LOGIN, grants it `rds_iam` and the
  privileges on the schema and the table. The lab's `rag/store.py` creates the extension as
  `rag`, which works in the local lab only. pgAudit also needs `CREATE EXTENSION pgaudit` in the
  database, after the reboot that loads the library, and before that setup runs.
- pgAudit records DDL and role changes only. Reads and writes of the vector table are not
  audited; object auditing on the RAG tables can be added once the schema exists, but the
  statement text it logs can hold the data itself.
- The identity that deploys this needs `kms:DescribeKey`, `kms:Decrypt`, `kms:GenerateDataKey`
  and `kms:CreateGrant` on the lab key for RDS to keep the master password under it (Aurora user
  guide, Secrets Manager integration). The key policy's account statement lets IAM grant these;
  the deploying identity's policy is not part of this design.
- Nothing alerts. DNS Firewall publishes blocked-query counts to CloudWatch
  (`FirewallRuleQueryVolume` per domain list) and flow logs record rejects, but no alarm watches
  them. CloudTrail, GuardDuty and CloudTrail network activity events (which record requests a
  VPC endpoint policy denied) are assumed at the account level, not created here.
- Bedrock invocation logging is one setting per account and Region. Applied in a shared account,
  this replaces whatever is there: deploy into a dedicated account, or check
  `GetModelInvocationLoggingConfiguration` first.
- One key covers the logs, the database storage, the master secret and Performance Insights.
  Separate keys per kind of data would narrow what one policy mistake exposes; for a lab this size
  one key was chosen.
- `rds.force_ssl` refuses plaintext. The minimum TLS version stays at the engine default, which
  was not looked up for Aurora PostgreSQL 17 (PostgreSQL's own default is TLSv1.2, and AWS shows
  TLSv1.2 for an earlier Aurora version); setting
  it blindly could loosen a stricter default. The server does not make clients verify its
  certificate; the agent should connect with `verify-full`.
- Any ECS task in the account whose definition names the agent role runs as it. The trust cannot
  be narrowed to one cluster; who may pass the role (`iam:PassRole`) and run tasks is where to
  limit it.
- The agent may write anything to its own log group, so whoever reads that group should be
  treated as able to read what the agent reads.
- No incident-response access is set up in advance: no role for reading the logs or reaching the
  database during an incident.
- Bedrock Guardrails are not used. The lab asks a person before a tool has side effects; that
  does not filter what the model reads or writes, and the agent that asks is out of scope here.
- Two Availability Zones by default, one database instance in each. Cost was not estimated.

## Sources

Checked 2026-10-22.

- Bedrock interface endpoints and endpoint policies:
  <https://docs.aws.amazon.com/bedrock/latest/userguide/vpc-interface-endpoints.html>
- Endpoint policies and `aws:SourceVpce`:
  <https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-access.html>
- Bedrock model invocation logging:
  <https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html>
- Aurora master password in Secrets Manager under a customer managed key:
  <https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/rds-secrets-manager.html>
- KMS permissions for roles that write encrypted log groups:
  <https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html>
- The flow log role's minimum permissions:
  <https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-iam-role.html>
- DNS Firewall rules and fail-closed default:
  <https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-dns-firewall-rule-settings.html>,
  <https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-dns-firewall-vpc-configuration.html>
- Traffic network ACLs do not filter, the Amazon DNS server among it:
  <https://docs.aws.amazon.com/vpc/latest/userguide/vpc-network-acls.html>
- `PutLogEvents` ignoring sequence tokens:
  <https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutLogEvents.html>
- pgvector versions in Aurora PostgreSQL 17.11:
  <https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Extensions.html>

## How this was built

With an AI coding agent (Claude Code), as with the rest of the lab. The owner reruns the checks
before publication.
