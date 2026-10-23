# Production Guide: Entra ID SSO, Multiple MCP Providers, Virtual Servers, Helm on AWS

This guide walks end to end through a production ContextForge deployment where:

- People sign in with **Microsoft Entra ID** (browser SSO for the Admin UI).
- MCP clients such as **Claude Code** reach the gateway using **Entra access tokens**,
  through the OAuth flow that Claude Code runs itself. No ContextForge tokens are pasted into config.
- Admins register **upstream MCP servers from several providers** (GitHub, Atlassian, an internal
  Entra-protected MCP, a wrapped stdio server, and so on).
- Admins combine tools from those upstreams into **virtual MCP servers**.
- **Entra groups decide who can see and call which MCP servers**, through ContextForge teams and RBAC.
- The gateway runs on **EKS** with the `mcp-stack` **Helm** chart, **RDS PostgreSQL**,
  **ElastiCache Redis**, and secrets held in **AWS Secrets Manager**.

It is based on a working local setup (Docker Compose behind nginx), and every step was checked against the
ContextForge 1.1.0 source. Sections marked **Verified locally** were run end to end. Sections marked
**Not yet verified** are derived from the code but were not run against a live deployment. Test them in
staging first.

!!! warning "Read the known limitations first"
    Several behaviours shape the design and are easy to miss. They are collected in
    [Known limitations and gotchas](#14-known-limitations-and-gotchas). The most important ones:

    - Entra role mapping (`SSO_ENTRA_ROLE_MAPPINGS`, `SSO_ENTRA_DEFAULT_ROLE`) **cannot grant
      team-scoped roles** such as `developer` or `viewer`. Use **group-to-team mapping** instead.
    - A user must **sign in through the browser once** before their Entra access token works on an MCP
      endpoint. The MCP endpoint does not create users.
    - The Helm chart has **no `existingSecret`** for the gateway and **no External Secrets support**.
      Use `extraEnvFrom` with a Secret you create yourself.

---

## Table of contents

1. [Architecture](#1-architecture)
2. [Prerequisites](#2-prerequisites)
3. [Entra ID: app registrations](#3-entra-id-app-registrations)
4. [Entra ID: security groups](#4-entra-id-security-groups)
5. [AWS: infrastructure and secrets](#5-aws-infrastructure-and-secrets)
6. [Helm: values and install](#6-helm-values-and-install)
7. [Post-install: SSO provider API trust](#7-post-install-sso-provider-api-trust)
8. [Teams and group-to-team mapping](#8-teams-and-group-to-team-mapping)
9. [Register upstream MCP servers](#9-register-upstream-mcp-servers)
10. [Create virtual servers](#10-create-virtual-servers)
11. [Connect Claude Code and other MCP clients](#11-connect-claude-code-and-other-mcp-clients)
12. [Role-based access model](#12-role-based-access-model)
13. [Verification checklist](#13-verification-checklist)
14. [Known limitations and gotchas](#14-known-limitations-and-gotchas)
15. [Operations](#15-operations)
16. [Troubleshooting](#16-troubleshooting)
17. [Appendix: what we did in the local setup](#17-appendix-what-we-did-in-the-local-setup)

---

## 1. Architecture

```text
                         ┌───────────────────────────────────────────┐
  Browser (Admin UI) ───▶│                                           │
                         │   AWS ALB (HTTPS, ACM cert)               │
  Claude Code ──────────▶│   gateway.example.com                     │
  (Entra access token)   └──────────────┬────────────────────────────┘
                                        │ X-Forwarded-Proto / Host
                         ┌──────────────▼────────────────────────────┐
                         │  EKS: ContextForge gateway pods (Helm)    │
                         │   /auth/sso/*        browser SSO           │
                         │   /servers/{id}/mcp  virtual MCP servers   │
                         │   /oauth/callback    upstream OAuth        │
                         └───┬───────────┬───────────┬───────────────┘
                             │           │           │
               RDS Postgres ◀┘  ElastiCache Redis    │  outbound HTTPS
                                                     ▼
          ┌──────────────┬──────────────┬─────────────────────┬─────────────────┐
          │ GitHub MCP   │ Atlassian    │ Internal MCP        │ stdio server via │
          │ (PAT/bearer) │ (OAuth code) │ (Entra client creds)│ translate (in-   │
          │              │              │                     │ cluster)         │
          └──────────────┴──────────────┴─────────────────────┴─────────────────┘

  Microsoft Entra ID:
    App 1  "ContextForge"       confidential web app: browser SSO + the API that MCP clients call
    App 2  "ContextForge-CLI"   public client: Claude Code and other MCP clients get tokens with it
    Groups "CF-*"               mapped to ContextForge teams (visibility) and the admin flag
```

### Two separate authentication paths

| Path | Who | Token | How the gateway validates it |
|------|-----|-------|------------------------------|
| **Browser SSO** | People using the Admin UI | Entra ID token (exchanged for a ContextForge session) | OIDC code flow on `/auth/sso/callback/entra`. Groups from the ID token drive team and admin sync. |
| **MCP client OAuth** | Claude Code, other MCP clients | Entra **access token** for App 1's API | Bearer token on `/servers/{id}/mcp`. Checked against the virtual server's `oauth_config` (issuer, audience, JWKS). |
| *(optional)* REST API | Scripts and automation | ContextForge API token, or an Entra access token | `/tools`, `/servers`, and so on. Entra tokens need `SSO_API_TOKEN_AUTH_ENABLED` and the provider trust settings. |

The MCP client path **does not read groups from the access token**. It looks the user up in the
ContextForge database and uses the team memberships synced at their **last browser sign-in**. That is
why users must sign in through the browser first, and again after their group membership changes.

---

## 2. Prerequisites

- An Entra ID tenant where you can create app registrations and security groups. Granting admin
  consent needs the *Cloud Application Administrator* role or higher. Without consent rights, each
  user sees a consent prompt the first time.
- A DNS name for the gateway, for example `gateway.example.com`, and an ACM certificate for it.
- An EKS cluster with the **AWS Load Balancer Controller** and the **External Secrets Operator** installed.
- RDS for PostgreSQL (16 or 17) and ElastiCache for Redis (TLS on).
- `kubectl`, `helm` 3.x, `aws` CLI, and `az` CLI (optional, used in the examples below).
- ContextForge 1.1.0. The Helm chart is `mcp-stack` 1.1.0, published at
  `oci://ghcr.io/ibm/mcp-context-forge/mcp-stack`.

Throughout this guide, replace the placeholders:

| Placeholder | Meaning |
|-------------|---------|
| `<TENANT_ID>` | Entra Directory (tenant) ID |
| `<APP_ID>` | Application (client) ID of App 1 "ContextForge" |
| `<CLI_APP_ID>` | Application (client) ID of App 2 "ContextForge-CLI" |
| `gateway.example.com` | Public hostname of the gateway |
| `<SERVER_ID>` | ID of a ContextForge virtual server |

---

## 3. Entra ID: app registrations

You need **two** app registrations. Keeping them separate lets the CLI client be a public client (no
secret), while the gateway app stays confidential.

### 3.1 App 1: "ContextForge" (web app and API)

**Why:** this app does browser SSO for the gateway. It also exposes the API scope that MCP clients
request. Access tokens for that scope are what the gateway accepts on MCP endpoints.

1. **Microsoft Entra ID → App registrations → New registration**
    - Name: `ContextForge`
    - Supported account types: **Single tenant**
    - Redirect URI: platform **Web**, `https://gateway.example.com/auth/sso/callback/entra`
2. **Overview:** copy the **Application (client) ID** (`<APP_ID>`) and the **Directory (tenant) ID** (`<TENANT_ID>`).
3. **Certificates & secrets → Client secrets → New client secret.**
    - Copy the **Value**, not the Secret ID. It is shown only once.
    - Pick a 6 to 12 month expiry and set a reminder to rotate it.
    - Store it in AWS Secrets Manager (section 5). Do not put it in a file in git.
4. **API permissions → Microsoft Graph → Delegated:** `openid`, `profile`, `email`, `User.Read`.
   Then **Grant admin consent**.
5. **Token configuration**
    - **Add optional claim → ID token:** `email`.
    - **Add groups claim:** choose **Security groups**, format **Group ID**, and tick the **ID** token type.

      The ID token is required. ContextForge reads groups from the ID token, not from userinfo.
6. **Authentication**
    - Leave the implicit grant checkboxes **unchecked**.
    - Optional: set the front-channel logout URL to `https://gateway.example.com/admin/logout`.
7. **Expose an API**
    - **Application ID URI:** set it to `api://<APP_ID>`.
    - **Add a scope:** name `access_as_user`, who can consent **Admins and users**, state **Enabled**.
    - **Add the virtual server URL as a second identifier URI.** See the warning below and section 11.4.
8. **Manifest:** set `"requestedAccessTokenVersion": 2`.

    **Why:** without this, Entra issues **v1** access tokens with issuer `https://sts.windows.net/<TENANT_ID>/`.
    The gateway is configured for the v2 issuer, so v1 tokens are rejected. With v2, the access token
    `aud` claim is `<APP_ID>` (the GUID), not `api://<APP_ID>`.

!!! warning "Identifier URI per virtual server (Claude Code requirement)"
    Claude Code sends the RFC 8707 `resource` parameter in the authorization request, set to the virtual
    server's URL. Entra requires the requested scope to belong to the app named in `resource`. If it
    doesn't, sign-in fails with `AADSTS9010010`.

    The fix is to add each virtual server URL as an extra **Application ID URI** on App 1, for example
    `https://gateway.example.com/servers/<SERVER_ID>/mcp`. Then point the client's scope at it (section 11).

    In production, Entra only accepts `https://` identifier URIs on a **verified domain** of your tenant.
    The gateway hostname must therefore be on a domain your tenant has verified.
    `http://localhost` identifiers are accepted for local testing only.

With the `az` CLI:

```bash
APP_ID=<APP_ID>
az ad app update --id $APP_ID \
  --web-redirect-uris https://gateway.example.com/auth/sso/callback/entra \
  --optional-claims '{"idToken":[{"name":"email","essential":false}]}' \
  --set groupMembershipClaims=SecurityGroup
az ad app update --id $APP_ID --set api.requestedAccessTokenVersion=2
az ad app update --id $APP_ID --identifier-uris "api://$APP_ID"
# Expose the access_as_user scope in the portal (Expose an API → Add a scope).
```

### 3.2 App 2: "ContextForge-CLI" (public client for MCP clients)

**Why:** Entra has no dynamic client registration, so every MCP client needs a pre-registered client ID.
A dedicated public client means:

- You don't have to pre-authorize Microsoft's Azure CLI for your API. That would let every Azure CLI
  user in the tenant get tokens for your gateway.
- No secret is needed, which is correct for software running on users' laptops.

1. **New registration:** name `ContextForge-CLI`, **Single tenant**, no redirect URI yet.
2. **Authentication → Add a platform → Mobile and desktop applications.** Add these redirect URIs:
    - `http://localhost:33418/callback`, used by Claude Code. The port must match `callbackPort` in the client config.
    - `http://localhost`, used by the device code and loopback flows of other tools.
3. **Authentication → Advanced settings → Allow public client flows: Yes.**
4. **Certificates & secrets:** create **none**.
5. **API permissions → Add a permission → My APIs → ContextForge → Delegated → `access_as_user`.**
   Then **Grant admin consent** if you can. If you can't, each user sees a consent prompt the first time.

```bash
CLI_APP_ID=<CLI_APP_ID>
az ad app update --id $CLI_APP_ID \
  --public-client-redirect-uris http://localhost http://localhost:33418/callback \
  --is-fallback-public-client true
```

!!! note "Optional: restrict who can sign in"
    By default, any user in the tenant can sign in to an app. To restrict this, set
    **Enterprise applications → ContextForge → Properties → Assignment required = Yes** and assign only
    the `CF-*` groups. Do the same for ContextForge-CLI. This is in addition to `SSO_TRUSTED_DOMAINS`
    and team mapping.

---

## 4. Entra ID: security groups

Create one Entra security group per access tier. Group names are for people; the gateway uses the
**Object ID**.

| Entra group | Purpose in ContextForge |
|-------------|-------------------------|
| `CF-Platform-Admins` | `is_admin=true` and global `platform_admin` (full access). Keep this group small. |
| `CF-Team-Engineering` | Member of the ContextForge team `engineering` (sees and calls engineering MCP servers) |
| `CF-Team-Support` | Member of the ContextForge team `support` |
| `CF-Team-Engineering-Owners` | Owner of the `engineering` team (`team_admin` in that team: can register gateways and servers for it) |

```bash
for name in CF-Platform-Admins CF-Team-Engineering CF-Team-Support CF-Team-Engineering-Owners; do
  az rest --method post --url https://graph.microsoft.com/v1.0/groups \
    --headers Content-Type=application/json \
    --body "{\"displayName\":\"$name\",\"mailEnabled\":false,\"mailNickname\":\"$name\",\"securityEnabled\":true}" \
    --query "{name:displayName,id:id}" -o json
done

# Add a user to a group
az rest --method post \
  --url "https://graph.microsoft.com/v1.0/groups/<GROUP_OBJECT_ID>/members/\$ref" \
  --headers Content-Type=application/json \
  --body '{"@odata.id":"https://graph.microsoft.com/v1.0/directoryObjects/<USER_OBJECT_ID>"}'
```

Record each group's Object ID. You need them in sections 6 and 8.

!!! note "Users in more than 200 groups"
    Entra drops the `groups` claim and sends an "overage" marker when a user is in more than about 200
    groups. ContextForge then calls Microsoft Graph `getMemberObjects` (`SSO_ENTRA_GRAPH_API_ENABLED=true`,
    the default). That call needs a delegated Graph permission on App 1 that allows reading the user's
    group membership, for example `GroupMember.Read.All`, with admin consent. Test this with a heavy-group
    user before go-live.

---

## 5. AWS: infrastructure and secrets

### 5.1 Database and cache

- **RDS PostgreSQL:** create a database `mcp` and a user `mcpgateway`. Use a private subnet and allow
  access only from the EKS node or pod security group.
- **ElastiCache Redis:** enable TLS in transit and AUTH, or RBAC users. The gateway URL is then `rediss://...`.

### 5.2 What goes in Secrets Manager

The Helm chart **cannot reference a gateway Secret by name** (no `existingSecret` for gateway config),
and it has **no External Secrets or CSI templates**. The recommended pattern:

1. Store the values in AWS Secrets Manager.
2. Use the **External Secrets Operator (ESO)** to sync them into Kubernetes Secrets in the gateway namespace.
3. Reference those Kubernetes Secrets from the chart:
    - Gateway secrets go in through `mcpContextForge.extraEnvFrom` (`secretRef`).
    - The database goes in through `postgres.external.existingSecret`.
    - Redis goes in through `redis.external.existingSecret`.

Use `extraEnvFrom`, not `extraEnv`, for gateway secrets. **The migration Job only receives `extraEnvFrom`.**
It needs `AUTH_ENCRYPTION_SECRET` and the `PLATFORM_ADMIN_*` values to bootstrap.

| Secrets Manager secret | JSON keys | Synced K8s Secret | Used by |
|------------------------|-----------|-------------------|---------|
| `contextforge/prod/gateway` | `JWT_SECRET_KEY`, `AUTH_ENCRYPTION_SECRET`, `PLATFORM_ADMIN_PASSWORD`, `DEFAULT_USER_PASSWORD`, `BASIC_AUTH_PASSWORD` | `cf-gateway-secrets` | `extraEnvFrom` |
| `contextforge/prod/entra` | `SSO_ENTRA_CLIENT_SECRET` | `cf-entra-secrets` | `extraEnvFrom` |
| `contextforge/prod/postgres` | `host`, `port`, `dbname`, `user`, `password` | `cf-postgres` | `postgres.external.existingSecret` |
| `contextforge/prod/redis` | `REDIS_URL` (`rediss://:<pw>@<host>:6379/0`) | `cf-redis` | `redis.external.existingSecret` |

Generate the gateway secrets with the built-in script:

```bash
python3 -m mcpgateway.scripts.init_secrets --stdout
```

Copy the generated values into Secrets Manager, and don't keep them on disk. For RDS, you can use the
RDS-managed master secret, but its JSON keys (`username`, `password`) differ. Either remap them in the
ExternalSecret (below) or set `postgres.external.userKey` and the other `*Key` values to match.

!!! danger "AUTH_ENCRYPTION_SECRET is not rotatable in place"
    `AUTH_ENCRYPTION_SECRET` encrypts stored upstream credentials and OAuth tokens. Changing it makes every
    stored secret unreadable, and each gateway's credentials then have to be re-entered. Back it up, and
    never let ESO generate a new value.

### 5.3 IAM for External Secrets Operator

Use **EKS Pod Identity** or **IRSA** to give the **ESO service account** (not the gateway) read access to
these secrets only:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
      "Resource": "arn:aws:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:contextforge/prod/*"
    },
    {
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:<REGION>:<ACCOUNT_ID>:key/<KMS_KEY_ID>"
    }
  ]
}
```

The gateway pods themselves need no AWS permissions. Keep the chart default
`serviceAccount.automountServiceAccountToken: false`. Only set it to `true` if you later give the gateway
its own IRSA role.

### 5.4 ExternalSecret manifests

```yaml
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: aws-secrets-manager
  namespace: contextforge
spec:
  provider:
    aws:
      service: SecretsManager
      region: <REGION>
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets   # ESO SA with the IAM role above (IRSA); omit auth for Pod Identity
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: cf-gateway-secrets
  namespace: contextforge
spec:
  refreshInterval: 1h
  secretStoreRef: {name: aws-secrets-manager, kind: SecretStore}
  target: {name: cf-gateway-secrets, creationPolicy: Owner}
  dataFrom:
    - extract: {key: contextforge/prod/gateway}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: cf-entra-secrets
  namespace: contextforge
spec:
  refreshInterval: 1h
  secretStoreRef: {name: aws-secrets-manager, kind: SecretStore}
  target: {name: cf-entra-secrets, creationPolicy: Owner}
  dataFrom:
    - extract: {key: contextforge/prod/entra}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: cf-postgres
  namespace: contextforge
spec:
  refreshInterval: 1h
  secretStoreRef: {name: aws-secrets-manager, kind: SecretStore}
  target: {name: cf-postgres, creationPolicy: Owner}
  dataFrom:
    - extract: {key: contextforge/prod/postgres}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: cf-redis
  namespace: contextforge
spec:
  refreshInterval: 1h
  secretStoreRef: {name: aws-secrets-manager, kind: SecretStore}
  target: {name: cf-redis, creationPolicy: Owner}
  dataFrom:
    - extract: {key: contextforge/prod/redis}
```

Check the `apiVersion` against your ESO release. Older releases use `external-secrets.io/v1beta1`.

Apply these before installing the chart, and confirm the four Secrets exist:

```bash
kubectl create namespace contextforge
kubectl apply -f external-secrets.yaml
kubectl -n contextforge get secret cf-gateway-secrets cf-entra-secrets cf-postgres cf-redis
```

!!! note "Secret changes do not restart pods"
    The chart has no checksum annotation. When ESO updates a Secret, run
    `kubectl -n contextforge rollout restart deploy/<release>-mcp-stack-mcpgateway` to pick it up.

---

## 6. Helm: values and install

### 6.1 How chart values become gateway settings

| Chart key | Becomes | Use for |
|-----------|---------|---------|
| `mcpContextForge.config.<KEY>` | ConfigMap `<fullname>-gateway-config` | Non-secret settings |
| `mcpContextForge.secret.<KEY>` | Secret `<fullname>-gateway-secret` (always created) | Avoid for real secrets. Null out the defaults. |
| `mcpContextForge.extraEnvFrom` | Extra `envFrom` sources (**wins over both of the above**, and reaches the migration Job) | The ESO-synced Secrets |
| `mcpContextForge.extraEnv` | Explicit `env` entries (gateway pod only) | Rare overrides |

Rules to remember:

- Any setting from `.env.example` can be set this way. Values must be **strings**, so quote JSON.
- The chart default puts `SSO_ENABLED` and `SSO_ENTRA_ENABLED` under `secret`. If you also set them under
  `config`, the ConfigMap wins. **Define each key in one place only.**
- `DATABASE_URL` and `REDIS_URL` are built by the chart from `postgres.external` and `redis.external`.
  Setting them under `secret` or `config` has no effect.

### 6.2 `values-prod.yaml`

```yaml
global:
  fullnameOverride: contextforge

serviceAccount:
  create: true
  automountServiceAccountToken: false

mcpContextForge:
  image:
    repository: ghcr.io/ibm/mcp-context-forge
    tag: v1.1.0
    pullPolicy: IfNotPresent
  replicaCount: 3
  hpa:
    enabled: true
    minReplicas: 3
    maxReplicas: 10

  ingress:
    enabled: true
    className: alb
    host: gateway.example.com
    path: /
    pathType: Prefix
    tls:
      enabled: false              # TLS is terminated at the ALB with ACM
    annotations:
      alb.ingress.kubernetes.io/scheme: internet-facing      # or internal
      alb.ingress.kubernetes.io/target-type: ip
      alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
      alb.ingress.kubernetes.io/certificate-arn: arn:aws:acm:<REGION>:<ACCOUNT_ID>:certificate/<CERT_ID>
      alb.ingress.kubernetes.io/ssl-policy: ELBSecurityPolicy-TLS13-1-2-2021-06
      alb.ingress.kubernetes.io/healthcheck-path: /health
      # Long-lived MCP/SSE streams:
      alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=3600
      # Restrict source ranges if the gateway is not meant for the open internet:
      # alb.ingress.kubernetes.io/inbound-cidrs: 10.0.0.0/8

  # ── Non-secret settings ────────────────────────────────────────────
  config:
    APP_DOMAIN: https://gateway.example.com           # exact public origin; drives SSO, OAuth callback, MCP audience
    ALLOWED_ORIGINS: '["https://gateway.example.com"]'
    SECURE_COOKIES: "true"
    CACHE_TYPE: redis

    # UI and admin API: needed for browser SSO and for the admin endpoints in this guide
    MCPGATEWAY_UI_ENABLED: "true"
    MCPGATEWAY_ADMIN_API_ENABLED: "true"

    # Auth
    AUTH_REQUIRED: "true"
    PLATFORM_ADMIN_EMAIL: admin@example.com

    # ── Entra SSO ──
    SSO_ENABLED: "true"
    SSO_ENTRA_ENABLED: "true"
    SSO_ENTRA_CLIENT_ID: "<APP_ID>"
    SSO_ENTRA_TENANT_ID: "<TENANT_ID>"
    SSO_ENTRA_GROUPS_CLAIM: groups
    SSO_ENTRA_ADMIN_GROUPS: '["<CF-Platform-Admins object id>"]'
    SSO_ENTRA_SYNC_ROLES_ON_LOGIN: "true"
    SSO_ENTRA_GRAPH_API_ENABLED: "true"
    SSO_TRUSTED_DOMAINS: '["example.com"]'
    SSO_AUTO_CREATE_USERS: "true"
    SSO_PRESERVE_ADMIN_AUTH: "true"                   # keep local admin login as break-glass
    SSO_ISSUERS: '["https://login.microsoftonline.com/<TENANT_ID>/v2.0"]'

    # Entra access tokens on the REST API (scripts). Not needed for MCP endpoints.
    SSO_API_TOKEN_AUTH_ENABLED: "true"

    # ── Teams / RBAC defaults ──
    AUTO_CREATE_PERSONAL_TEAMS: "true"
    ALLOW_TEAM_CREATION: "false"                      # only platform admins create teams
    DEFAULT_TEAM_MEMBER_ROLE: viewer                  # team members can use servers and call tools
    DEFAULT_TEAM_OWNER_ROLE: team_admin

    # ── Upstream reachability (SSRF) ──
    SSRF_PROTECTION_ENABLED: "true"
    # Allow in-cluster upstreams (translate bridges, internal MCPs). Use your VPC/pod CIDRs only.
    SSRF_ALLOWED_NETWORKS: '["10.0.0.0/16"]'

    # Leave these off unless you have a reason:
    ENABLE_HEADER_PASSTHROUGH: "false"
    INSECURE_ALLOW_QUERYPARAM_AUTH: "false"

  # Null out chart-default secret placeholders; real values come from extraEnvFrom
  secret:
    JWT_SECRET_KEY: null
    AUTH_ENCRYPTION_SECRET: null
    BASIC_AUTH_PASSWORD: null
    PLATFORM_ADMIN_PASSWORD: null
    DEFAULT_USER_PASSWORD: null
    SSO_ENABLED: null                # set in config above instead
    SSO_ENTRA_ENABLED: null

  extraEnvFrom:
    - secretRef: {name: cf-gateway-secrets}
    - secretRef: {name: cf-entra-secrets}

postgres:
  enabled: false
  external:
    enabled: true
    existingSecret: cf-postgres
    hostKey: host
    portKey: port
    databaseKey: dbname
    userKey: user
    passwordKey: password

redis:
  external:
    enabled: true
    existingSecret: cf-redis
    urlKey: REDIS_URL

migration:
  enabled: true
  hookPhase: "pre-install,pre-upgrade"   # external DB already exists before install
  image:
    tag: v1.1.0                           # must match mcpContextForge.image.tag

# Not for production
mcpFastTimeServer:
  enabled: false
  ingress:
    enabled: false
```

!!! warning "Proxy trust"
    The gateway trusts `X-Forwarded-*` headers from **any** source (`trusted_hosts="*"`). It uses them to
    build public URLs: the RFC 9728 metadata, SSO redirect checks, and Host. Keep the gateway Service
    `ClusterIP`, reachable only through the ALB. Use NetworkPolicies or security groups so nothing else
    can reach port 4444.

!!! note "Admin UI and admin API in production"
    `MCPGATEWAY_UI_ENABLED` and `MCPGATEWAY_ADMIN_API_ENABLED` default to `false`. The general hardening
    advice is to keep them off. In this design they are needed:

    - Browser SSO sign-in is served from `/admin/login`.
    - Users must sign in there once before their MCP access works.
    - Interactive upstream OAuth ("Authorize") uses the UI.

    Restrict exposure at the ALB if needed: an internal ALB, `inbound-cidrs`, or an AWS WAF rule on `/admin`.

### 6.3 Install

```bash
helm upgrade --install contextforge oci://ghcr.io/ibm/mcp-context-forge/mcp-stack \
  --version 1.1.0 \
  --namespace contextforge \
  -f values-prod.yaml \
  --wait --timeout 15m
```

Check that it worked:

```bash
kubectl -n contextforge get pods,ingress
kubectl -n contextforge logs job/contextforge-migration          # alembic + bootstrap
curl -s https://gateway.example.com/health
curl -s https://gateway.example.com/auth/sso/providers          # expect an "entra" entry
```

Upgrades use the same command with a new `--version` and image tags. The migration Job runs
`pre-upgrade`. Gateway pods wait in a startup probe until the schema is at head.

!!! note "Docs drift"
    `docs/docs/deployment/helm.md` shows `mcpContextForge.envFrom` and `rbac.create`. Neither key exists in
    chart 1.1.0. Use `mcpContextForge.extraEnvFrom`.

---

## 7. Post-install: SSO provider API trust

Some settings live only in the database, on the `entra` SSO provider record. **They have no environment
variable**, so you set them once through the admin API:

| Field | Value | Why |
|-------|-------|-----|
| `trusted_for_api_auth` | `true` | Accept Entra access tokens on REST endpoints |
| `api_audience` | `<APP_ID>` | The v2 access token `aud`. Required whenever `trusted_for_api_auth` is true (prevents confused-deputy acceptance). |
| `team_mapping` | see section 8 | Entra group to team membership |

The `issuer` is set automatically from `SSO_ENTRA_TENANT_ID`
(`https://login.microsoftonline.com/<TENANT_ID>/v2.0`).

These DB values **survive restarts**: bootstrap preserves `team_mapping` and the API trust fields.
Re-apply them after restoring an empty database.

Get an admin token. Do this from a trusted machine, and never paste `JWT_SECRET_KEY` into a shared shell:

```bash
# Option A: log in as the bootstrap platform admin (break-glass account)
TOKEN=$(curl -s https://gateway.example.com/auth/email/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<PLATFORM_ADMIN_PASSWORD>"}' | jq -r .access_token)

# Option B: sign in to the Admin UI via Entra (you are in CF-Platform-Admins) and create an
# API token under "Tokens" with a short expiry.
```

Set the API trust:

```bash
curl -s -X PUT https://gateway.example.com/auth/sso/admin/providers/entra \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"trusted_for_api_auth": true, "api_audience": "<APP_ID>"}'

curl -s https://gateway.example.com/auth/sso/admin/providers/entra -H "Authorization: Bearer $TOKEN" \
  | jq '{issuer, trusted_for_api_auth, api_audience, team_mapping}'
```

Use the single-provider endpoint to check. The list endpoint `GET /auth/sso/admin/providers` does not
return `issuer`.

---

## 8. Teams and group-to-team mapping

### 8.1 Why teams, not role mappings

ContextForge has two layers:

1. **Visibility (Layer 1):** what a user can **see**, through team membership. Gateways, tools, and
   virtual servers have `visibility` (`private`, `team`, `public`) and a `team_id`.
2. **RBAC (Layer 2):** what a user can **do**, through roles such as `viewer`, `developer`,
   `team_admin` (team-scoped), and `platform_admin` (global).

Entra **role mapping** (`SSO_ENTRA_ROLE_MAPPINGS`, `SSO_ENTRA_DEFAULT_ROLE`) only works for **global** roles.
Mapping a group to `developer` or `viewer` (team-scoped) silently fails at login. The role needs a team,
and none is given. The log shows `scope_id required for team-scoped assignments`.

Entra **team mapping** (`team_mapping` on the provider) gives you both layers at once:

- It adds the user to the team (visibility).
- It grants `DEFAULT_TEAM_MEMBER_ROLE` (default `viewer`) or `DEFAULT_TEAM_OWNER_ROLE` (default
  `team_admin`) scoped to that team (RBAC).
- It re-syncs on **every browser sign-in**. Memberships from removed groups are revoked.
- Memberships added by hand are never touched.

So in production:

- Use `SSO_ENTRA_ADMIN_GROUPS` for platform admins.
- Use `team_mapping` for everyone else.
- Leave `SSO_ENTRA_ROLE_MAPPINGS` and `SSO_ENTRA_DEFAULT_ROLE` **unset**. The local setup had
  `SSO_ENTRA_DEFAULT_ROLE=viewer`, which did nothing, because `viewer` is team-scoped.

### 8.2 Create the teams

Only platform admins can create teams when `ALLOW_TEAM_CREATION=false`:

```bash
for t in engineering support; do
  curl -s -X POST https://gateway.example.com/teams \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$t\",\"visibility\":\"private\"}" | jq '{id, name}'
done
```

Record each team ID.

### 8.3 Map Entra groups to teams

```bash
curl -s -X PUT https://gateway.example.com/auth/sso/admin/providers/entra \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "team_mapping": {
      "<CF-Team-Engineering object id>":        {"team_id": "<engineering team id>", "role": "member"},
      "<CF-Team-Engineering-Owners object id>": {"team_id": "<engineering team id>", "role": "owner"},
      "<CF-Team-Support object id>":            {"team_id": "<support team id>",     "role": "member"}
    }
  }'
```

- Keys are Entra group Object IDs, or app-role values if you use app roles. Matching is case-insensitive.
- `role` is `member` or `owner`. Any other value is treated as `member`.
- A plain string value (`"<group>": "<team id>"`) means `member`.
- The teams must already exist. Personal teams cannot be mapped.
- `docs/docs/manage/sso.md` shows a `conditions` key. It is ignored.

---

## 9. Register upstream MCP servers

Register each upstream with `POST /gateways`. Prefer `/v1/gateways` in new automation; the
unversioned routes are deprecated aliases.

Key rules:

- `transport` must be `STREAMABLEHTTP` or `SSE`. `HTTP` and `STDIO` are rejected.
- Set `team_id` and `"visibility": "team"` **at creation**. Tools, resources, and prompts inherit both.
  Changing `team_id` later does not move existing tools (use the ownership-transfer action).
- The default `visibility` is **`public`**: every signed-in user would see it. Always set it explicitly.
- Tool names become `<gateway-slug>-<tool-name>`, for example `github-create-issue`.
- Credentials are encrypted at rest with `AUTH_ENCRYPTION_SECRET`.

### 9.1 GitHub remote MCP (bearer token)

```bash
curl -s -X POST https://gateway.example.com/v1/gateways \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "name": "github",
    "url": "https://api.githubcopilot.com/mcp/",
    "transport": "STREAMABLEHTTP",
    "auth_type": "bearer",
    "auth_token": "<GitHub PAT or GitHub App token>",
    "team_id": "<engineering team id>",
    "visibility": "team"
  }'
```

Every user of this gateway acts as the identity of that token. For per-user GitHub identity, register
GitHub as an OAuth `authorization_code` upstream instead (9.2).

### 9.2 Atlassian (per-user OAuth, authorization code)

Each user authorizes once. ContextForge stores one token per user per gateway, encrypted.

1. In the Atlassian developer console, set the callback URL to
   **`https://gateway.example.com/oauth/callback`**. This is one global callback for all upstream OAuth
   providers, built from `APP_DOMAIN`.
2. Register the gateway:

```bash
curl -s -X POST https://gateway.example.com/v1/gateways \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "name": "atlassian",
    "url": "https://mcp.atlassian.com/v1/sse",
    "transport": "SSE",
    "auth_type": "oauth",
    "oauth_config": {
      "grant_type": "authorization_code",
      "client_id": "<atlassian client id>",
      "client_secret": "<atlassian client secret>",
      "authorization_url": "https://auth.atlassian.com/authorize",
      "token_url": "https://auth.atlassian.com/oauth/token",
      "redirect_uri": "https://gateway.example.com/oauth/callback",
      "audience": "api.atlassian.com",
      "scopes": ["read:jira-work", "offline_access"]
    },
    "team_id": "<support team id>",
    "visibility": "team"
  }'
```

3. The gateway starts with **no tools**. A user with access opens
   `https://gateway.example.com/oauth/authorize/<GATEWAY_ID>` (or clicks **Authorize** in the Admin UI)
   and signs in to Atlassian.
4. Load the tools: `POST /oauth/fetch-tools/<GATEWAY_ID>` or `POST /v1/gateways/<GATEWAY_ID>/tools/refresh`.

### 9.3 Internal MCP server protected by Entra (client credentials)

For an internal MCP server that validates Entra tokens:

1. Give it its own app registration, `internal-mcp`, with **Expose an API** (`api://<INTERNAL_APP_ID>`)
   and an **app role**, for example `MCP.Invoke`.
2. Give ContextForge a client identity. Use App 1, or a dedicated `ContextForge-Upstream` app. Grant it
   **API permissions → Application → `MCP.Invoke`** on `internal-mcp`, plus admin consent.
3. Register:

```bash
curl -s -X POST https://gateway.example.com/v1/gateways \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "name": "internal-tools",
    "url": "https://internal-mcp.example.com/mcp",
    "transport": "STREAMABLEHTTP",
    "auth_type": "oauth",
    "oauth_config": {
      "grant_type": "client_credentials",
      "client_id": "<ContextForge-Upstream app id>",
      "client_secret": "<secret>",
      "token_url": "https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token",
      "scopes": ["api://<INTERNAL_APP_ID>/.default"]
    },
    "team_id": "<engineering team id>",
    "visibility": "team"
  }'
```

For a stronger setup, use a certificate instead of a secret: `"token_endpoint_auth_method": "private_key_jwt"`
with `private_key` (PEM) and `private_key_jwt_kid`. See `docs/docs/manage/oauth.md`.

!!! note "Per-user identity to internal Entra APIs"
    Entra's native on-behalf-of flow (the `jwt-bearer` grant) is **not implemented** in ContextForge. The
    `token-exchange` grant type is RFC 8693, which Entra does not support. For per-user identity,
    choose one:

    - Register the internal MCP as an `authorization_code` upstream against Entra v2 endpoints. Each user
      authorizes once.
    - Enable **identity propagation**, and have the upstream verify the HMAC-signed
      `X-Forwarded-User-*` headers. See `docs/docs/manage/identity-propagation.md`.

### 9.4 stdio MCP server, wrapped with `mcpgateway.translate`

The translate bridge has **no inbound authentication**. Run it as a **ClusterIP-only** Deployment in the
same namespace. Never expose it through an ingress.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: mcp-git-bridge, namespace: contextforge}
spec:
  replicas: 1
  selector: {matchLabels: {app: mcp-git-bridge}}
  template:
    metadata: {labels: {app: mcp-git-bridge}}
    spec:
      containers:
        - name: bridge
          image: ghcr.io/ibm/mcp-context-forge:v1.1.0
          command: ["python3", "-m", "mcpgateway.translate",
                    "--stdio", "uvx mcp-server-git",
                    "--expose-streamable-http", "--stateless",
                    "--host", "0.0.0.0", "--port", "9000"]
          ports: [{containerPort: 9000}]
          readinessProbe: {httpGet: {path: /healthz, port: 9000}}
---
apiVersion: v1
kind: Service
metadata: {name: mcp-git-bridge, namespace: contextforge}
spec:
  selector: {app: mcp-git-bridge}
  ports: [{port: 9000, targetPort: 9000}]
```

Add a NetworkPolicy so that only gateway pods can reach port 9000. Then register the bridge:

```bash
curl -s -X POST https://gateway.example.com/v1/gateways \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "name": "git",
    "url": "http://mcp-git-bridge.contextforge.svc.cluster.local:9000/mcp",
    "transport": "STREAMABLEHTTP",
    "auth_type": "authheaders",
    "auth_headers": [{"key": "X-Bridge-Key", "value": "<random value>"}],
    "team_id": "<engineering team id>",
    "visibility": "team"
  }'
```

- In-cluster URLs fail with `422` unless `SSRF_ALLOWED_NETWORKS` includes the cluster or VPC CIDR (section 6.2).
- Don't use `auth_type: none`. With `none`, the gateway **forwards the caller's own `Authorization`
  header** upstream, and that header carries the user's ContextForge or Entra token. Any non-empty
  `auth_type` stops this. The bridge ignores the dummy header.
- The image must include the stdio server's runtime (here `uvx` and `mcp-server-git`). Build a small
  derived image if it doesn't.

### 9.5 After registering

```bash
# Discover or refresh tools
curl -s -X POST "https://gateway.example.com/v1/gateways/<GATEWAY_ID>/tools/refresh" -H "Authorization: Bearer $TOKEN"

# List tool IDs for a gateway (needed for virtual servers)
curl -s "https://gateway.example.com/v1/tools?gateway_id=<GATEWAY_ID>" -H "Authorization: Bearer $TOKEN" \
  | jq '.[] | {id, name}'
```

Optional automatic refresh: `AUTO_REFRESH_SERVERS=true` and `GATEWAY_AUTO_REFRESH_INTERVAL=300`.

The chart has **no values-driven way** to pre-register gateways. Script these calls, or run them as a Helm
post-install Job. Use an admin API token held in Secrets Manager, not a JWT you minted yourself.

---

## 10. Create virtual servers

A virtual server is an MCP endpoint that exposes a chosen set of tools, possibly from several upstreams.

```bash
curl -s -X POST https://gateway.example.com/v1/servers \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "name": "engineering-tools",
    "description": "GitHub + git + internal tools for engineering",
    "associated_tools": ["<tool uuid>", "<tool uuid>", "<tool uuid>"],
    "team_id": "<engineering team id>",
    "visibility": "public",
    "oauth_enabled": true,
    "oauth_config": {
      "authorization_servers": ["https://login.microsoftonline.com/<TENANT_ID>/v2.0"],
      "scopes_supported": ["api://<APP_ID>/access_as_user", "openid", "profile", "email"],
      "resource": "<APP_ID>"
    }
  }' | jq '{id, name, url}'
```

Field by field:

- **`associated_tools`:** tool **UUIDs**, not names. Names are rejected.
- **`oauth_enabled`, `oauth_config`:** required for Entra access tokens on this server's MCP endpoint.
  Without them, the endpoint accepts only ContextForge-issued tokens and returns 401 for Entra tokens.
    - `authorization_servers`: the Entra v2 issuer. Tokens from other issuers fall through to internal JWT checks.
    - `resource`: the expected `aud`. For Entra v2 access tokens, that is `<APP_ID>` (the GUID).
      **The Admin UI form has no field for it. Set it through the API.** Without it, the gateway expects
      `aud` = `APP_DOMAIN/servers/<id>/mcp`, which Entra never issues.
    - `scopes_supported`: published in the RFC 9728 metadata for clients.
- **`visibility`:** see the next section.

### 10.1 Visibility for OAuth-enabled virtual servers

The RFC 9728 metadata endpoint
(`/.well-known/oauth-protected-resource/servers/<id>/mcp`) returns **404 unless the server is
`public`**. This prevents leaking that private or team servers exist. That gives you two choices:

| Server visibility | Discovery by MCP clients | Who can connect |
|-------------------|--------------------------|-----------------|
| `public` | Works (metadata published) | Any signed-in user with `servers.use`. **Tool access is still filtered per tool**: users only see and call tools whose own visibility allows them. |
| `team` | Metadata returns 404, so clients need full manual OAuth config (section 11) | Team members only |

The recommended pattern:

- Make the **server** `public`, so clients can discover it.
- Make the **gateways and tools** `team`, so access is controlled where the data lives.
- A user outside the team sees an empty tool list on that server, even though they can connect.

If you don't want server names visible to everyone, use `team` and configure clients manually.

A `public` server that also has a `team_id` requires `ALLOW_PUBLIC_VISIBILITY=true`, which is the default.
If you set it to `false`, create the public server without a `team_id`.

!!! warning "A virtual server can reference any tool, even ones its members can't see"
    Attaching tools only checks that the IDs exist. At runtime, `tools/list` and `tools/call` re-check each
    tool's own visibility for the caller. Membership in the server's team does **not** grant access to
    tools from other teams.

### 10.2 Per-server Entra identifier URI

For each OAuth-enabled virtual server that Claude Code uses, add its URL as an **Application ID URI** on
App 1 (see the warning in 3.1):

```bash
az ad app update --id <APP_ID> --identifier-uris \
  "api://<APP_ID>" \
  "https://gateway.example.com/servers/<SERVER_1_ID>/mcp" \
  "https://gateway.example.com/servers/<SERVER_2_ID>/mcp"
```

`--identifier-uris` **replaces the whole list**. Always pass every existing URI.

### 10.3 Server IDs and restarts

In production you create servers once and they persist. Locally, the compose `register_fast_time` job
**deleted and recreated** its demo server on every restart, which reset `oauth_enabled` to false. Don't
attach production clients to servers that a registration job manages.

---

## 11. Connect Claude Code and other MCP clients

### 11.1 Users: sign in through the browser first

Each user signs in **once** at `https://gateway.example.com/admin/login` with **Continue with Microsoft**.
This step:

- Creates the ContextForge user. **The MCP endpoint does not auto-create users.** Without this step the
  user gets `User not registered in ContextForge. Please log in via SSO first.`
- Syncs team memberships from Entra groups.

After a group change in Entra, the user signs in through the browser again to re-sync. MCP access tokens
alone do not update memberships.

### 11.2 Claude Code configuration

Each user runs this once, with the virtual server's URL and `<SERVER_ID>`:

```bash
claude mcp add-json --scope user engineering-tools '{
  "type": "http",
  "url": "https://gateway.example.com/servers/<SERVER_ID>/mcp",
  "oauth": {
    "clientId": "<CLI_APP_ID>",
    "callbackPort": 33418,
    "authServerMetadataUrl": "https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration",
    "scopes": "https://gateway.example.com/servers/<SERVER_ID>/mcp/access_as_user offline_access"
  }
}'
```

Then sign in. Use `/mcp` → the server → **Authenticate** inside Claude Code, or run
`claude mcp login engineering-tools` in an interactive terminal.

What each field does:

| Field | Value | Why |
|-------|-------|-----|
| `clientId` | `<CLI_APP_ID>` | Entra has no dynamic client registration |
| `callbackPort` | `33418` | Claude Code redirects to `http://localhost:<port>/callback`, which must match App 2 |
| `authServerMetadataUrl` | Entra v2 `openid-configuration` | Entra only serves metadata at `/<tenant>/v2.0/.well-known/openid-configuration` (the RFC 8414 paths return 404) |
| `scopes` | `<server URL>/access_as_user offline_access` | Matches the `resource` that Claude Code sends (the server URL), which avoids `AADSTS9010010`. `offline_access` gives a refresh token, so users aren't asked to sign in every hour. |

To roll this out to a team, commit a project-scoped `.mcp.json` with the same JSON under `mcpServers`.
No secrets are involved.

### 11.3 Other MCP clients

- **Clients that support OAuth with a fixed client ID:** same settings as above. Register their exact
  redirect URI on App 2.
- **Clients that only support a static bearer header:** use a **ContextForge API token** scoped to the
  server. Don't use an Entra token; it expires in about an hour.

  ```bash
  curl -s -X POST https://gateway.example.com/tokens -H "Authorization: Bearer <user session or token>" \
    -H 'Content-Type: application/json' \
    -d '{"name":"cline-engineering","expires_in_days":30,"team_id":"<engineering team id>",
         "scope":{"server_id":"<SERVER_ID>","permissions":["tools.read","tools.execute"]}}'
  ```

  Use `Authorization: Bearer <token>` with the URL `https://gateway.example.com/servers/<SERVER_ID>/mcp`.
  The scope lists can only narrow what the user already has.

### 11.4 Why the extra identifier URI was needed

**Not yet verified end to end.** Locally, Claude Code's first sign-in failed with `AADSTS9010010`.
That fits Claude Code sending `resource=<server URL>` (RFC 8707) with a scope under `api://<APP_ID>`.
Entra v2 requires the scope to belong to the app identified by `resource`. Adding the server URL as an
identifier URI on App 1, and requesting the scope through that URI, should make them consistent.

Confirm in staging that the Claude Code sign-in completes and that the issued token has `aud=<APP_ID>`.
That `aud` must match the server's `oauth_config.resource`. If `aud` comes back as something else, set
`oauth_config.resource` to the value you observe.

What **was** verified locally: an Entra v2 access token from App 2 (device code flow, scope
`api://<APP_ID>/access_as_user`) was accepted on `/servers/<id>/mcp`, and `initialize` and `tools/list`
returned 200. Unauthenticated and tampered tokens returned 401.

---

## 12. Role-based access model

### 12.1 Built-in roles

| Role | Scope | Key permissions |
|------|-------|-----------------|
| `platform_admin` | global | `*` |
| `team_admin` | team | Create, update, delete gateways, servers, tools, resources, prompts. Manage team members. `servers.use`, `tools.execute`. |
| `developer` | team | Like `team_admin`, without team management |
| `viewer` | team | Read, `servers.use`, `tools.execute` |
| `platform_viewer` | global | Read and `servers.use`. **No `tools.execute`.** Every new user gets this by default (`DEFAULT_USER_ROLE`). |

### 12.2 What each action needs

| Action | Requirement |
|--------|-------------|
| Connect to `/servers/<id>/mcp` | `servers.use`, and the server is visible to the user |
| `tools/list` | Returns only tools on the server **and** visible to the user |
| `tools/call` | `tools.execute` from a **non-personal team role** (for example `viewer` from team mapping) or a global role, **and** the tool is visible to the user |
| Register a gateway or server for a team | `gateways.create` / `servers.create` in that team (`team_admin` or `developer`) |

A user with only `platform_viewer`, meaning no team mapping, can connect to a public server, but
`tools/call` is denied.

### 12.3 Worked example

| Person | Entra groups | ContextForge result | Engineering server | Support server |
|--------|--------------|---------------------|--------------------|----------------|
| Alice | CF-Team-Engineering | `engineering` member → `viewer` | Sees and calls engineering tools | Connects, sees no tools |
| Bob | CF-Team-Support | `support` member → `viewer` | Connects, sees no tools | Sees and calls Atlassian tools (after authorizing) |
| Carol | CF-Team-Engineering-Owners | `engineering` owner → `team_admin` | Calls tools. Can register gateways and servers for engineering. | No tools |
| Dana | CF-Platform-Admins | `is_admin`, `platform_admin` | Everything | Everything |
| Eve | none | `platform_viewer` only | Connects, no tools, no calls | Same |

This assumes the servers are `public` and the gateways and tools are `team` (see 10.1).

### 12.4 Custom roles

To grant a capability across teams, define a global custom role through `POST /rbac/roles`. Or bootstrap
roles from a file with `MCPGATEWAY_BOOTSTRAP_ROLES_IN_DB_ENABLED=true` and
`MCPGATEWAY_BOOTSTRAP_ROLES_IN_DB_FILE`. Map Entra groups to it with `SSO_ENTRA_ROLE_MAPPINGS`, which works
for **global** roles.

Global roles grant **permissions**, not **visibility**. A user still needs team membership, or public
items, to see tools.

---

## 13. Verification checklist

Run these after install and after every Entra or gateway change.

```bash
GW=https://gateway.example.com
SID=<SERVER_ID>

# Platform
curl -s $GW/health
curl -s $GW/auth/sso/providers                                     # entra listed

# Server OAuth metadata (public servers only)
curl -s $GW/.well-known/oauth-protected-resource/servers/$SID/mcp | jq

# Deny paths (must all be 401)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $GW/servers/$SID/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer not.a.token' $GW/v1/tools
```

Check access per user:

- [ ] A user with no Entra groups can sign in but **cannot call tools**.
- [ ] An engineering member sees **only** engineering tools on the engineering server.
- [ ] A support member sees **no** engineering tools.
- [ ] Removing a user from an Entra group, followed by a browser sign-in, removes their team membership.
- [ ] A tampered or expired Entra token returns 401.
- [ ] A token for a **different** Entra app (wrong `aud`) returns 401.
- [ ] `claude mcp list` shows the server as Connected after `/mcp` → Authenticate.

---

## 14. Known limitations and gotchas

| # | Limitation | Impact | What to do |
|---|------------|--------|------------|
| 1 | Team-scoped roles in `SSO_ENTRA_ROLE_MAPPINGS` and `SSO_ENTRA_DEFAULT_ROLE` are never assigned | Users get no team permissions | Use `team_mapping` (section 8) |
| 2 | The MCP endpoint does not create users, and does not read groups from access tokens | First use fails, and group changes lag | Browser sign-in first, and after group changes |
| 3 | `trusted_for_api_auth`, `api_audience`, and `team_mapping` have no env vars | Not declarative in Helm | One-time API calls or a post-install Job (section 7) |
| 4 | RFC 9728 metadata only for `public` servers | Team servers aren't discoverable | Public server, team-scoped tools (10.1) |
| 5 | Admin UI form has no `resource` field for servers | UI-created OAuth servers reject Entra v2 tokens | Set `oauth_config.resource` through the API |
| 6 | Claude Code sends RFC 8707 `resource`; Entra v2 rejects mismatches | `AADSTS9010010` | One identifier URI per server (10.2) |
| 7 | Production identifier URIs must be https on a verified tenant domain | Can't use an unverified gateway domain | Host the gateway on a verified domain |
| 8 | Entra on-behalf-of is not implemented | No per-user Entra token to upstreams | `authorization_code` upstreams or identity propagation |
| 9 | `auth_type: none` forwards the caller's `Authorization` header | User tokens leak to the upstream | Always set an auth type (9.4) |
| 10 | Chart: no `existingSecret` or ESO for gateway; `extraEnv` not passed to migration | Secrets plumbing is manual | ESO plus `extraEnvFrom` (section 5) |
| 11 | Chart: config changes don't restart pods | Stale settings | `kubectl rollout restart` |
| 12 | Gateway trusts all `X-Forwarded-*` | URL spoofing if reachable directly | Only reachable through the ALB |
| 13 | `AUTH_ENCRYPTION_SECRET` rotation breaks stored credentials | Re-enter every upstream credential | Treat it as permanent. Back it up. |
| 14 | Changing a gateway's `team_id` doesn't move its tools | Tools keep the old team | Set the team at creation, or use ownership transfer |

---

## 15. Operations

| Task | How |
|------|-----|
| Rotate the Entra client secret (App 1) | Create a new secret in Entra. Update `contextforge/prod/entra` in Secrets Manager. Let ESO sync. `rollout restart`. Delete the old secret in Entra. |
| Upstream credential rotation | `PUT /v1/gateways/<id>` with the new credential |
| Add a team or access tier | Create the Entra group. `POST /teams`. Add the mapping with `PUT /auth/sso/admin/providers/entra`, sending the **full** `team_mapping`, because it replaces the old one. |
| Add a virtual server for Claude Code | `POST /v1/servers` with `oauth_config.resource`. Add its identifier URI to App 1 (10.2). Give users the client config. |
| Offboard a user | Remove them from the Entra groups, or disable them in Entra. Their access tokens expire within about an hour. Also deactivate the ContextForge user (Admin UI → Users) for immediate effect. |
| Break-glass | Local `PLATFORM_ADMIN_EMAIL` login stays enabled (`SSO_PRESERVE_ADMIN_AUTH=true`). Store its password in Secrets Manager with restricted access. |
| Backups | RDS automated backups. Back up `AUTH_ENCRYPTION_SECRET` and `JWT_SECRET_KEY` separately. Without them, a restored DB is unusable. |
| Monitoring | Watch for `401` spikes on `/servers/*/mcp`. Watch gateway logs for `Failed to assign role` (misconfigured role mappings) and `team_mapping` errors. |

---

## 16. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `AADSTS50011` redirect URI mismatch | Redirect URI not registered exactly | Browser SSO: `https://<host>/auth/sso/callback/entra` on App 1. Claude Code: `http://localhost:<port>/callback` on App 2. |
| `AADSTS65001` consent required | No admin consent | Grant consent, or accept the prompt |
| `AADSTS650057` invalid resource | The client app has no permission for the API | Add `access_as_user` to App 2's API permissions |
| `AADSTS9010010` resource doesn't match scopes | The client sent `resource=<server URL>` and the scope uses `api://<APP_ID>` | Add the server URL as an identifier URI. Use `<server URL>/access_as_user`. |
| Claude Code: "rejected them on reconnect" | The gateway rejected the Entra token | Check the server's `oauth_enabled` and `oauth_config.resource`, the token `iss` (must be v2), and that the user exists |
| MCP 401 `Invalid authentication credentials` | Server `oauth_enabled=false`, or the issuer isn't in `authorization_servers` | Re-apply the server's `oauth_config` |
| MCP 401 `User not registered in ContextForge` | User never signed in through the browser | Browser sign-in once |
| MCP 403 `Access denied` | Missing `servers.use` | Check the user's team mapping and roles |
| Tools list empty | User not in the tools' team | Check the group, the `team_mapping`, and a fresh browser sign-in |
| `tools/call` denied | Only `platform_viewer` | Team mapping, which grants `viewer` with `tools.execute` |
| Token `iss` is `https://sts.windows.net/...` | v1 tokens | Set `requestedAccessTokenVersion: 2` on App 1 |
| Gateway registration `422` for an in-cluster URL | SSRF protection | Add the CIDR to `SSRF_ALLOWED_NETWORKS` |
| SSO login: "SSO authentication is disabled" or no Microsoft button | `SSO_ENABLED` or `SSO_ENTRA_ENABLED` not set, or set in both `config` and `secret` | Define each key in one place. `rollout restart`. |
| Settings changed but no effect | Pods not restarted | `kubectl rollout restart deploy/contextforge-mcpgateway` |

Debugging tip: decode the access token claims without sending the token anywhere.

```bash
python3 -c 'import sys,json,base64; p=sys.argv[1].split(".")[1]; p+="="*(-len(p)%4); \
print(json.dumps({k:v for k,v in json.loads(base64.urlsafe_b64decode(p)).items() \
if k in ("iss","aud","ver","azp","scp","preferred_username","groups")}, indent=1))' "$TOKEN"
```

---

## 17. Appendix: what we did in the local setup

This is the sequence that produced a working local setup (Docker Compose, nginx on `:8080`). Use it as a
reference for the production steps above.

| # | Step | Production equivalent |
|---|------|-----------------------|
| 1 | Created App 1 `MY-ENTRA`. Added redirect `http://localhost:8080/auth/sso/callback/entra`, optional claim `email`, `groupMembershipClaims=SecurityGroup`. | 3.1 |
| 2 | `.env`: `SSO_ENABLED`, `SSO_ENTRA_ENABLED`, client ID, secret, tenant ID, `SSO_TRUSTED_DOMAINS` | 6.2 `config` + Secrets Manager |
| 3 | **`docker-compose.yml` did not pass `SSO_ENTRA_*` to the gateway.** We added pass-throughs for `SSO_ENTRA_ENABLED`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_TENANT_ID`, `_DEFAULT_ROLE`, `_GROUPS_CLAIM`, `_ADMIN_GROUPS`, `_ROLE_MAPPINGS`, `_SYNC_ROLES_ON_LOGIN`, and `SSO_TRUSTED_DOMAINS`. | Not needed with Helm (any key works) |
| 4 | `docker compose up -d` after every `.env` change. `restart` does not re-read `.env`. | `helm upgrade` + `rollout restart` |
| 5 | Created Entra groups `ContextForge-Platform-Admins`, `-Developers`, `-Viewers`. Set `SSO_ENTRA_ADMIN_GROUPS` and `SSO_ENTRA_ROLE_MAPPINGS`. | Section 4 + **team mapping** (section 8). The developer and viewer role mappings did not work, because those roles are team-scoped (limitation 1). |
| 6 | Exposed API `api://<APP_ID>` with scope `access_as_user`. Set `requestedAccessTokenVersion=2`. | 3.1 steps 7 and 8 |
| 7 | `SSO_API_TOKEN_AUTH_ENABLED=true`. `PUT /auth/sso/admin/providers/entra` with `trusted_for_api_auth=true`, `api_audience=<APP_ID>`. | Section 7 |
| 8 | Azure CLI as a client failed (`AADSTS65001`, then `AADSTS650057`). We created App 2 `ContextForge-CLI` (public client) instead. | 3.2 |
| 9 | Device code token from App 2 → `GET /tools` 200 → user auto-created as `platform_admin` through the admin group. | Verification |
| 10 | MCP endpoint 401 with an Entra token until the virtual server had `oauth_enabled` + `oauth_config` + `resource=<APP_ID>` | Section 10 |
| 11 | Claude Code with `clientId`, `callbackPort`, `authServerMetadataUrl`, `scopes` | 11.2 |
| 12 | `AADSTS9010010` → added the server URL as an identifier URI and changed the scope to `<server URL>/access_as_user`. The sign-in after this change has **not yet been confirmed**. | 3.1 warning, 10.2, 11.4 |
| 13 | The compose `register_fast_time` job recreated the demo server on restart, which reset its OAuth settings | 10.3: use your own servers |

---

## Related documentation

- [Microsoft Entra ID SSO tutorial](../manage/sso-microsoft-entra-id-tutorial.md)
- [Entra role mapping](../manage/sso-entra-role-mapping.md)
- [RBAC](../manage/rbac.md) and [Teams](../manage/teams.md)
- [Multi-tenancy architecture](../architecture/multitenancy.md)
- [OAuth for upstream gateways](../manage/oauth.md) and [OAuth resource configuration](../manage/oauth-resource-configuration.md)
- [Identity propagation](../manage/identity-propagation.md)
- [Helm deployment](helm.md) and [AWS](aws.md)
- [mcpgateway.translate](../using/mcpgateway-translate.md)
