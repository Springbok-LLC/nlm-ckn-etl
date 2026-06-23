# ADR 0001: Choosing Grafana Cloud for Workflow Observability

- **Status:** Proposed
- **Date:** 2026-06-22
- **Deciders:** NLM-CKN ETL team

## Context

The NLM-CKN ETL workload runs on two distinct AWS compute paths, both of which
already stream container logs to CloudWatch via the `awslogs` log driver:

1. **Release pipeline** — runs `release.py` end-to-end on **AWS Batch (EC2
   launch type)**, logging to the `/batch/nlm-ckn-release` CloudWatch log group.
   EC2 (not Fargate) is required because the pipeline mounts
   `/var/run/docker.sock`. See [`cloudformation/batch.yaml`](../cloudformation/batch.yaml).
2. **Scheduled fetch** — runs as a **daily ECS Fargate task** triggered by
   EventBridge Scheduler, logging to the `/ecs/nlm-ckn-fetch` CloudWatch log
   group. See [`cloudformation/fetch.yaml`](../cloudformation/fetch.yaml).

Workflow orchestration is already handled by **Prefect** flows
([`python/src/flows/`](../python/src/flows/) — `pipeline.py`, `fetch.py`,
`release.py`). Prefect gives us run-state tracking for the flows themselves, but
it does not provide a unified view of the underlying AWS infrastructure logs and
metrics. The baseline AWS CloudWatch console likewise lacks a unified dashboard
experience for rapidly correlating metrics and logs across the two log groups.

We require centralized observability to track script execution, monitor errors,
and visualize system behavior over time. We evaluated four primary paths:

1. **Prefect Cloud (Paid Tier):** We already run Prefect as our orchestrator;
   the paid tier would add AWS infrastructure connection features and a hosted
   UI, but locks those features behind a $100/mo paywall.
2. **New Relic:** A massive 100 GB/month free SaaS tier, but requires learning a
   completely proprietary query language (NRQL) and carries aggressive premium
   pricing per user seat if we scale.
3. **OpenObserve:** An ultra-efficient, highly cost-effective SQL-based logging
   platform, but requires operational overhead to self-host and maintain on our
   own EC2/S3 infrastructure.
4. **Grafana Cloud:** A fully managed, open-standards SaaS observability platform
   with a generous free tier (50 GB logs, 50 GB traces, 10k active metric
   series, 3 users, 14-day retention).

## Decision

We will adopt **Grafana Cloud** as our central observability and logging
platform for the AWS Batch release pipeline and the ECS Fargate fetch task.

Initially, we will connect Grafana Cloud to AWS using the **CloudWatch Data
Source (Pull Method)** with an IAM assume-role architecture, wiring in both
existing log groups (`/batch/nlm-ckn-release` and `/ecs/nlm-ckn-fetch`). This
follows the same OIDC / assume-role pattern this repo already uses for GitHub
Actions ([`cloudformation/github-oidc.yaml`](../cloudformation/github-oidc.yaml)),
so it introduces no new credential pattern. It lets us visualize logs
immediately without modifying our container code or managing log-shipping
infrastructure.

If log volumes scale or CloudWatch API costs become a factor, we will migrate to
pushing logs via **Amazon Data Firehose** directly into Grafana Loki.

> **Future / out of scope:** This ADR covers the ETL workload in this repo only.
> The deployed UI application lives in a separate repository (`nlm-ckn-ui`); the
> same Grafana Cloud instance and IAM assume-role pattern could be extended to
> ingest that application's logs, giving the project a single observability plane
> across ETL and UI. 

## Justification

Grafana Cloud strikes the ideal balance between cost, ease of implementation,
and feature completeness:

* **Zero Infrastructure Overhead:** Unlike self-hosting OpenObserve (or a
  Prefect Server), Grafana Cloud is fully managed. We incur no maintenance,
  patching, or host hosting costs.
* **Generous Free Tier:** The 50 GB log allowance easily covers our expected
  batch and fetch job volumes. We get a comprehensive dashboard experience
  without any initial financial commitment.
* **Native AWS Integration:** Grafana can query AWS CloudWatch directly
  out-of-the-box. Because both compute paths already ship logs to CloudWatch via
  the `awslogs` driver, the integration is purely cloud configuration — basic
  IAM roles, implementable in well under an hour.
* **Industry Standard Ecosystem:** Relying on Grafana means utilizing open
  standards (LogQL/PromQL). The vast library of pre-built community dashboards
  means we do not have to design monitoring views from scratch.

## Consequences

### Positive (Benefits)

* **Immediate Time-to-Value:** No modifications are required within our Python
  scripts or Docker images. Both stacks already emit to CloudWatch, so
  visibility relies purely on cloud configuration.
* **Cost Control:** We stay at $0/month while proving out our automation, with a
  predictable usage-based upgrade path if we exceed the free limits. On Grafana
  Cloud's Pro plan ($19/mo platform fee, which includes the 50 GB free
  allowance), logs ingested beyond the allowance run **$0.55/GB total**
  ($0.40/GB write + $0.10/GB retain + $0.05/GB process); metrics beyond 10k
  active series run $6.50 per 1k series. (Pricing verified June 2026.)
* **Team Scaling Protection:** Grafana Cloud allows 3 free users on its free
  tier, avoiding the strict single-user or expensive per-seat traps of
  alternative enterprise solutions.

### Negative (Trade-offs & Risks)

* **Modular Learning Curve:** Teams must interact with LogQL for logs and PromQL
  for metrics rather than a single unified language like SQL (OpenObserve) or
  NRQL (New Relic).
* **AWS API Query Costs:** Leaving logs in CloudWatch and using the "Pull Method"
  triggers AWS API fees when dashboards are refreshed. We must monitor our AWS
  bill to ensure these query fees don't eclipse the cost of setting up a
  real-time data push stream.
* **Separation of Concerns:** We retain Prefect for orchestration run-state and
  add Grafana for infrastructure logs/metrics, rather than the single pane of
  glass that a paid Prefect Cloud tier would provide. We accept jumping between
  the Prefect run view and Grafana dashboards for debugging.
