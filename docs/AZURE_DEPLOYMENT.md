# Deploying to Azure

> **Status: written, not deployed.** I had no Azure subscription, no Azure CLI, and no Docker daemon while building this. Everything
> below follows documented Azure behavior but has not been run. The local implementation (`python -m glcc ...`) is the verified one.
> Run each step yourself the first time, and use `--help` on any command that has drifted in your CLI version.

## Target architecture

```
GitHub Actions --OIDC--> Azure
                          |
   az acr build  --->  Azure Container Registry
                          |  AcrPull (managed identity, no registry password)
                          v
              Azure Container Apps (1 replica) <--- Microsoft Entra ID (user sign-in, X-MS-CLIENT-PRINCIPAL-NAME)
                |        |
                |        +--> Azure OpenAI (optional; Entra ID auth, role: Cognitive Services OpenAI User)
                +--> stdout JSON logs --> Log Analytics
```

## Prerequisites

* An Azure subscription, the Azure CLI (`az login`), and permission to create resource groups and role assignments.
* Optionally an existing **Azure OpenAI** resource with a chat-model deployment (GPT-4-class models handle the JSON mode used here).

## Steps

### 1. Deploy the app (offline mode first)

```bash
export SUBSCRIPTION_ID=<your subscription id>
export SUFFIX=glcc$RANDOM
./deploy/azure/deploy.sh
```

This creates a resource group, a container registry, a user-assigned managed identity with `AcrPull`, builds the image **inside ACR** (no
local Docker needed), creates the Container Apps environment, and deploys one replica with a generated API key. It prints the URL.

```bash
curl https://<fqdn>/health
curl -H "X-API-Key: <key>" -X POST https://<fqdn>/cases/CASE-1001/analyze
```

### 2. Turn on the LLM layer (optional)

Re-run with `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, and `AZURE_OPENAI_RESOURCE_ID` exported. The script grants the app's managed
identity `Cognitive Services OpenAI User` on that resource and sets `GLCC_LLM_PROVIDER=azure_openai`. No API key is stored; the SDK uses
`DefaultAzureCredential`, which picks up the identity through `AZURE_CLIENT_ID`. Check with a one-off job or locally:

```bash
az login && GLCC_LLM_PROVIDER=azure_openai AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_DEPLOYMENT=... python -m glcc llm-check
```

Expect an access error until the role assignment propagates (a few minutes).

### 3. Identity (required before real use)

The approval workflow is only as trustworthy as the identity behind it. The app deliberately refuses to take an approver's name from a
request body unless `GLCC_ALLOW_BODY_IDENTITY=true`, which the deploy script sets to `false`. In Azure, enable built-in authentication so
Container Apps authenticates the user with Microsoft Entra ID and injects `X-MS-CLIENT-PRINCIPAL-NAME`:

```bash
APP_ID=$(az ad app create --display-name glcc-exceptions \
  --web-redirect-uris "https://$FQDN/.auth/login/aad/callback" --enable-id-token-issuance true --query appId -o tsv)
az containerapp auth microsoft update -n $APP -g $RG --client-id $APP_ID --client-secret <secret> --tenant-id <tenant> --yes
az containerapp auth update -n $APP -g $RG --enabled true --unauthenticated-client-action Return401
```

Exclude `/health` from authentication so probes work (see `az containerapp auth update --help` for the excluded-paths option in your version).

**Roles.** `config/roles.json` is a static demo directory. In production, map Entra **group membership** to the roles
`logistics_coordinator`, `underwriter`, `senior_underwriter`, `legal_counsel`, `compliance_officer`. The code consults the directory at
approval time **and again at execution time**, so removing someone from a group blocks a pending execution. Parsing the group claims
from `X-MS-CLIENT-PRINCIPAL` into that directory is the remaining piece (not implemented).

### 4. CI/CD

`.github/workflows/azure-deploy.yml` builds in ACR and rolls out a new revision after the tests pass, behind the `production` environment
(add required reviewers there for a manual gate). It authenticates with **OIDC federation**, so no Azure secret is stored in GitHub:

1. Create an Entra app registration and add a **federated credential** for `repo:<org>/<repo>:environment:production`.
2. Grant it `Contributor` on the resource group (and `AcrPush`).
3. Set repository variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`, `RESOURCE_GROUP`, `CONTAINER_APP`.

## Things to know before relying on it

| Topic | Detail |
|---|---|
| **Persistence** | The demo database is SQLite at `/tmp/glcc.db`, which is **ephemeral**: a restart loses proposals, approvals, and the audit log. For a demo, fine. For anything else, mount Azure Files or (better) move to Postgres |
| **Replicas** | Exactly one. SQLite is single-writer; the deploy script pins `--min-replicas 1 --max-replicas 1` |
| **Cold start** | `min-replicas 1` avoids it. Retrievers are built at startup (fast on this corpus) |
| **Costs** | One 0.5 vCPU / 1 GiB replica, a Basic ACR, and Log Analytics ingestion; plus Azure OpenAI token usage if enabled |
| **Secrets** | The API key is a Container Apps secret referenced by name. Prefer Key Vault references for anything real |
| **Audit anchor** | Periodically write the latest audit hash to an immutable Blob container (time-based retention) so truncation of the log's tail becomes detectable |

## Production substitutions (what each would replace)

| Today | Production | Seam |
|---|---|---|
| Lexical hybrid retrieval | Azure AI Search (keyword + vector + semantic ranker) with the same metadata filters | `glcc/retrieval/retrievers.py` |
| Images and scans flagged unreadable | Azure AI Document Intelligence (OCR, layout) | `glcc/ingest/cases.py: read_image`, and the contract parser |
| SQLite | Azure Database for PostgreSQL (or Cosmos DB) | `glcc/governance/store.py` (same method names) |
| Static role file | Entra groups | `glcc/config.py: load_directory` |
| Stub payment action | Your payment or customs API, with the proposal id as the idempotency key | `glcc/service.py: perform_action` |
| stdout logs | Application Insights / OpenTelemetry traces | the trace and audit structures already carry run ids |
| `GLCC_API_KEY` | Entra authentication only (drop the shared key) | `glcc/api/server.py` |
