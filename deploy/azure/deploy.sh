#!/usr/bin/env bash
# Deploy the GLCC exception agent to Azure Container Apps.
#
# NOT TESTED by the author: this needs an Azure subscription and the Azure CLI, neither of which was available
# when the project was built. Read it, run it step by step the first time, and expect to adjust names or flags
# for your CLI version. See docs/AZURE_DEPLOYMENT.md for what each step does and what is left for you.
#
# Usage:
#   az login
#   export SUBSCRIPTION_ID=...            # required
#   export SUFFIX=glcc$RANDOM             # makes global names unique (ACR names are global)
#   # optional, to enable the LLM layer using an EXISTING Azure OpenAI resource:
#   export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
#   export AZURE_OPENAI_DEPLOYMENT=<deployment name>
#   export AZURE_OPENAI_RESOURCE_ID=/subscriptions/.../providers/Microsoft.CognitiveServices/accounts/<name>
#   ./deploy/azure/deploy.sh
set -euo pipefail

: "${SUBSCRIPTION_ID:?set SUBSCRIPTION_ID}"
: "${SUFFIX:?set SUFFIX to a short unique string}"
LOCATION="${LOCATION:-eastus}"
RG="${RG:-rg-glcc-$SUFFIX}"
ACR="${ACR:-acr${SUFFIX//[^a-z0-9]/}}"
ENV_NAME="${ENV_NAME:-cae-glcc-$SUFFIX}"
APP="${APP:-ca-glcc-$SUFFIX}"
IDENTITY="${IDENTITY:-id-glcc-$SUFFIX}"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)}"
IMAGE="glcc-exceptions:$TAG"
API_KEY="${GLCC_API_KEY:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"

az account set --subscription "$SUBSCRIPTION_ID"
az extension add --name containerapp --upgrade --only-show-errors

echo "==> resource group, registry, identity"
az group create -n "$RG" -l "$LOCATION" --only-show-errors >/dev/null
az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled false --only-show-errors >/dev/null
az identity create -n "$IDENTITY" -g "$RG" --only-show-errors >/dev/null
ID_RESOURCE=$(az identity show -n "$IDENTITY" -g "$RG" --query id -o tsv)
ID_PRINCIPAL=$(az identity show -n "$IDENTITY" -g "$RG" --query principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$ID_PRINCIPAL" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$ACR_ID" --only-show-errors >/dev/null

echo "==> build the image in ACR (no local Docker needed)"
az acr build -r "$ACR" -t "$IMAGE" . --only-show-errors

echo "==> container apps environment"
az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" --only-show-errors >/dev/null

# Offline (deterministic) mode unless an Azure OpenAI deployment was supplied.
ENV_VARS=("GLCC_DB=/tmp/glcc.db" "GLCC_API_KEY=secretref:api-key" "GLCC_ALLOW_BODY_IDENTITY=false")
if [[ -n "${AZURE_OPENAI_ENDPOINT:-}" && -n "${AZURE_OPENAI_DEPLOYMENT:-}" ]]; then
  ENV_VARS+=("GLCC_LLM_PROVIDER=azure_openai" "AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT" "AZURE_OPENAI_DEPLOYMENT=$AZURE_OPENAI_DEPLOYMENT")
  ID_CLIENT=$(az identity show -n "$IDENTITY" -g "$RG" --query clientId -o tsv)
  ENV_VARS+=("AZURE_CLIENT_ID=$ID_CLIENT")            # tells DefaultAzureCredential which user-assigned identity to use
  if [[ -n "${AZURE_OPENAI_RESOURCE_ID:-}" ]]; then
    az role assignment create --assignee-object-id "$ID_PRINCIPAL" --assignee-principal-type ServicePrincipal \
      --role "Cognitive Services OpenAI User" --scope "$AZURE_OPENAI_RESOURCE_ID" --only-show-errors >/dev/null
  else
    echo "!! AZURE_OPENAI_RESOURCE_ID not set: grant 'Cognitive Services OpenAI User' to identity $IDENTITY yourself."
  fi
else
  ENV_VARS+=("GLCC_LLM_PROVIDER=none")
fi

echo "==> container app (single replica: the demo store is SQLite, which is single-writer)"
az containerapp create -n "$APP" -g "$RG" --environment "$ENV_NAME" --image "$ACR.azurecr.io/$IMAGE" \
  --user-assigned "$ID_RESOURCE" --registry-server "$ACR.azurecr.io" --registry-identity "$ID_RESOURCE" \
  --target-port 8000 --ingress external --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1Gi \
  --secrets "api-key=$API_KEY" --env-vars "${ENV_VARS[@]}" --only-show-errors >/dev/null

FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
echo
echo "Deployed: https://$FQDN"
echo "API key (store it safely; shown once): $API_KEY"
echo "Try:  curl https://$FQDN/health"
echo "      curl -H 'X-API-Key: $API_KEY' -X POST https://$FQDN/cases/CASE-1001/analyze"
echo
echo "NEXT (required before real use): enable Microsoft Entra authentication on the app so approver identity"
echo "arrives in the X-MS-CLIENT-PRINCIPAL-NAME header. See docs/AZURE_DEPLOYMENT.md, section 'Identity'."
