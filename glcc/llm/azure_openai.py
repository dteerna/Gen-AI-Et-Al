"""Azure OpenAI adapter (chat completions with JSON output).

NOT exercised by the automated tests (they need an Azure subscription). It is a thin wrapper around the
official `openai` SDK; verify it in your environment with `python -m glcc llm-check`.

Authentication: an API key if AZURE_OPENAI_API_KEY is set, otherwise Microsoft Entra ID through
DefaultAzureCredential (managed identity in Azure, `az login` locally). Prefer Entra ID in production.
The identity needs the "Cognitive Services OpenAI User" role on the resource.
"""

from __future__ import annotations

import json

from .base import LLMError

SCOPE = "https://cognitiveservices.azure.com/.default"


class AzureOpenAIClient:
    def __init__(self, endpoint: str, deployment: str, api_version: str = "2024-10-21", api_key: str | None = None, timeout_s: float = 30.0):
        try:
            from openai import AzureOpenAI
        except ImportError as exc:
            raise LLMError("The 'openai' package is required for Azure OpenAI: pip install -r requirements-azure.txt") from exc
        if api_key:
            self._client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version, timeout=timeout_s)
        else:
            try:
                from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            except ImportError as exc:
                raise LLMError("Entra ID auth needs 'azure-identity': pip install -r requirements-azure.txt") from exc
            provider = get_bearer_token_provider(DefaultAzureCredential(), SCOPE)
            self._client = AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=provider, api_version=api_version, timeout=timeout_s)
        self.deployment = deployment

    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 1500) -> dict:
        resp = self._client.chat.completions.create(
            model=self.deployment,                        # the Azure deployment name, not the base model name
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0, max_tokens=max_tokens, response_format={"type": "json_object"})
        text = resp.choices[0].message.content or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise LLMError("model returned JSON that is not an object")
        return data
