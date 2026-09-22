import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict



class Settings(BaseSettings):
    # Cosmos DB Logging/Storage
    cosmosdb_api_url: str

    # Mongo API
    mongo_api_url: str | None = None
    mongo_uri: str | None = None
    mongo_db_name: str | None = Field(default=None, alias="TABLEAU_MONGO_DB_NAME")

    # Upstream Services
    parsing_results_base_url: str | None = None
    data_layer_results_base_url: str | None = None

    # Groq
    groq_api_key: str | None = Field(default=None, alias="TABLEAU_GROQ_API_KEY")
    groq_model: str = Field(default="groq/compound-mini", alias="TABLEAU_GROQ_MODEL")

    # Shared LLM Configuration (aligned with Qlik)
    llm_max_input_tokens: int = Field(default=3000, alias="LLM_MAX_INPUT_TOKENS")
    llm_max_output_tokens: int = Field(default=1000, alias="LLM_MAX_OUTPUT_TOKENS")
    llm_max_concurrent_requests: int = Field(default=5, alias="LLM_MAX_CONCURRENT_REQUESTS")
    llm_max_retries: int = Field(default=1, alias="LLM_MAX_RETRIES")
    llm_timeout: float = Field(default=10.0, alias="LLM_TIMEOUT")
    llm_retry_delay: float = Field(default=1.0, alias="LLM_RETRY_DELAY")

    # Azure OpenAI
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_deployment_name: str | None = None
    azure_openai_api_version: str | None = None

    # Auth
    auth_jwks_url: str | None = None
    auth_issuer: str | None = None
    auth_audience: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

# Post-init setup for dependent variables if missing
import os
if not settings.groq_api_key:
    settings.groq_api_key = os.getenv("TABLEAU_GROQ_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("QLIK_GROQ_API_KEY")
if not os.getenv("TABLEAU_GROQ_MODEL") and os.getenv("GROQ_MODEL"):
    settings.groq_model = os.getenv("GROQ_MODEL")

if not settings.parsing_results_base_url:
    settings.parsing_results_base_url = f"{settings.cosmosdb_api_url.rstrip('/')}/api/records/parsing"
if not settings.data_layer_results_base_url:
    settings.data_layer_results_base_url = f"{settings.cosmosdb_api_url.rstrip('/')}/api/records/datalayer"

