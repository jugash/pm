# DevOps / Platform Engineer — SFTP Platform (OpenShift)

**Location:** [Location / hybrid / remote — e.g. London, 2 days on-site]
**Employment type:** [Permanent / Fixed-term / Day-rate contract]
**Reporting to:** [Platform Team Lead / Head of Platform Engineering]
**Salary / rate:** [£— band or day rate]

---

## About the role

We're replacing our legacy Axway managed-file-transfer gateway with **SFTPGo running on Red Hat OpenShift (OCP)**, delivered as a shared, self-serviceable SFTP platform for internal and external clients. You'll be the engineer who builds and codifies that platform — the "paved road" that application teams then onboard onto themselves.

This is a hands-on infrastructure-as-code role. Everything ships as code: the OpenShift footprint in **Terraform**, the SFTPGo and PostgreSQL runtime as **Helm** charts, and client onboarding as **GitOps** pull requests reconciled to SFTPGo's API. You'll work alongside platform, network, security and migration engineers, and you'll set the standards that outlive the migration.

If you like turning a fragile, click-configured legacy system into something declarative, observable and reproducible, this is that job.

## What you'll do

- Build the SFTPGo platform on OpenShift as **Helm charts** — highly-available pods, a shared SSH host key, a PostgreSQL data provider, and S3/ODF object storage — parameterised cleanly across dev, test and prod.
- Codify the surrounding infrastructure in **Terraform** — OpenShift projects/namespaces, quotas, RBAC, network policies, object-storage buckets, DNS and the L4/LoadBalancer exposure that plain-TCP SFTP requires (a standard HTTP Route won't do it).
- Implement **GitOps** delivery (Argo CD or Flux) so the platform and its config are versioned, reviewable and self-healing.
- Build the **self-service onboarding pipeline**: a Git repo of account/flow definitions → CI validation against guardrails → applied to SFTPGo via its REST API, with secrets and SSH keys managed through Vault / External Secrets.
- Stand up **CI/CD** for the platform (image builds, chart linting/testing, policy-as-code, promotion between environments).
- Wire up **observability and operability** — Prometheus metrics, alerting, structured audit logs shipped to the SIEM, backup/restore for PostgreSQL, and runbooks.
- Harden the platform with security and network engineers — the SFTPGo Defender (brute-force blocklisting), TLS, host-key strategy, and separated ingress for external vs internal clients.
- Support **wave-based migration** off Axway: pre-stage configuration, help translate legacy routing into SFTPGo's Event Manager rules, and enable per-wave rollback.
- Write the docs, templates and golden paths that let application teams onboard a flow by pull request rather than a ticket.

## Essential skills & experience

- Strong hands-on **Kubernetes / OpenShift** experience running production workloads (deployments, services, ingress, RBAC, network policies, storage classes).
- **Helm** — authoring and maintaining non-trivial charts, not just installing others'.
- **Terraform** — writing reusable, reviewed modules and managing state for real environments.
- **GitOps** in practice (Argo CD or Flux) and Git-centred, PR-based workflows.
- **CI/CD** pipeline design (e.g. GitLab CI, GitHub Actions, Tekton, or Jenkins).
- Solid **Linux** fundamentals and scripting (Bash plus one of Python / Go).
- Comfortable with containers and registries, secrets management, and TLS/PKI basics.
- A declarative, automation-first mindset — allergic to undocumented manual changes.

## Desirable (a plus, not a barrier)

- Exposure to **SFTPGo, or MFT/B2B platforms** such as Axway, GoAnywhere, IBM Sterling, or MOVEit.
- Working knowledge of **SFTP / SSH** and network fundamentals (firewalls, NAT, DMZ, load balancing).
- **PostgreSQL** operations, ideally operator-managed (CloudNativePG, Crunchy, or similar).
- **S3-compatible / OpenShift Data Foundation (ODF)** object storage.
- **HashiCorp Vault** or External Secrets Operator; policy-as-code (OPA/Kyverno).
- Red Hat certification (EX280 / OpenShift) or CKA/CKAD.
- Experience migrating a legacy system into containers with parallel-run cutovers.

## What success looks like

- **First weeks:** environments defined in Terraform and a first SFTPGo deploy running on OCP from a Helm chart, in a lower environment.
- **By the pilot:** a repeatable, GitOps-managed platform with HA, monitoring, backups and L4 exposure proven end to end.
- **Through migration:** application teams onboarding their own flows by pull request against the templates and guardrails you've built — with the platform team no longer a bottleneck.

## How we work

We're a **platform-as-enabler** team practising **full left-shift**: we build and run the shared service, and we hand teams safe, self-service paths to consume it rather than doing their config for them. We value infrastructure as code, small reviewed changes, good documentation, and automation over toil. You'll have real ownership of the platform and a clear, high-visibility migration to deliver.

## How to apply

[Application instructions — link / email / recruiter contact]

*[Company] is an equal-opportunity employer and welcomes applicants from all backgrounds. We're happy to make reasonable adjustments throughout the hiring process — just let us know.*
