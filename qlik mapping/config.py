import os
import logging
try:
    from cachetools import TTLCache
except ImportError:
    class TTLCache(dict):
        def __init__(self, maxsize=100, ttl=300):
            super().__init__()

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    def load_dotenv():
        pass

logger = logging.getLogger(__name__)

def _clean_url(val):
    return str(val or "").strip().rstrip("/")

# BASE_API_URL is the primary parent URL; fallbacks are checked only if BASE_API_URL is empty
PRIMARY_BASE_URL = (
    _clean_url(os.getenv("BASE_API_URL"))
    or _clean_url(os.getenv("AGENT_ACTIONS_API_URL"))
    or _clean_url(os.getenv("COSMOS_BASE_API"))
    or _clean_url(os.getenv("MONGO_API_URL"))
    or _clean_url(os.getenv("QLIK_MONGO_API_URL"))
    or ""
)
COSMOS_DB_API = PRIMARY_BASE_URL
TARGET_API_URL = os.getenv("TARGET_API_URL", "")

TENANT_ID = os.getenv("AZURE_AD_TENANT_ID", os.getenv("TENANT_ID"))
EXPECTED_AUDIENCE = os.getenv("AZURE_AD_EXPECTED_AUDIENCE")
ISSUER = f"https://sts.windows.net/{TENANT_ID}/" if TENANT_ID else ""
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys" if TENANT_ID else ""
ENABLE_AUTH = os.getenv("ENABLE_AUTH", "true").lower() != "false"

HEADERS = {'Content-Type': 'application/json'}
get_results_cache = TTLCache(maxsize=100, ttl=3600)


class Config:
    MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
    GROQ_API_KEY = os.getenv("QLIK_GROQ_API_KEY") or os.getenv("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "groq/compound-mini")
    USE_GROQ = True

    BASE_API_URL = COSMOS_DB_API
    AGENT_ACTIONS_API_URL = _clean_url(os.getenv("AGENT_ACTIONS_API_URL")) or PRIMARY_BASE_URL
    RETRY_DELAY = float(os.getenv("LLM_RETRY_DELAY", "1.0"))
    LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENT_REQUESTS", os.getenv("LLM_MAX_CONCURRENCY", "1")))
    LLM_MAX_BATCH_SIZE = int(os.getenv("LLM_MAX_BATCH_SIZE", "5"))

    # Token limits
    LLM_MAX_INPUT_TOKENS = int(os.getenv("LLM_MAX_INPUT_TOKENS", "3000"))
    LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1000"))

    # Deduplication and in-memory caching
    LLM_CACHE_ENABLED = os.getenv("LLM_CACHE_ENABLED", "true").lower() != "false"
    LLM_CACHE_TTL = int(os.getenv("LLM_CACHE_TTL", "86400"))
    LLM_CACHE_MAX_SIZE = int(os.getenv("LLM_CACHE_MAX_SIZE", "1000"))

    # --- LLM-assisted conversion configuration ---
    # Core measures and custom objects use LLM when needed;
    # Deterministic columns & M-queries are handled by connection_mapper without wasting rate limits
    USE_LLM_MEASURES = True
    USE_LLM_DIMENSIONS = False
    USE_LLM_VISUALS = True
    USE_LLM_VARIABLES = False
    USE_LLM_COLUMNS = False
    USE_LLM_MQUERY = os.getenv("USE_LLM_MQUERY", "true").lower() == "true"

    # Character budget for rules block injected into system prompt
    LLM_RULES_CHAR_BUDGET = int(os.getenv("LLM_RULES_CHAR_BUDGET", "8000"))
    LLM_SYSTEM_PROMPT_LIMIT = int(os.getenv("LLM_SYSTEM_PROMPT_LIMIT", "12000"))
    LLM_SCHEMA_COLUMN_LIMIT = int(os.getenv("LLM_SCHEMA_COLUMN_LIMIT", "20"))

    _validated: bool = False

    @staticmethod
    def validate() -> None:
        if Config._validated:
            return
        Config._validated = True
        logger.info("Validating LLM configuration")
        if not Config.GROQ_API_KEY:
            logger.warning("GROQ_API_KEY is not set in environment. Deterministic mappings will run; LLM calls will require key.")
        if not COSMOS_DB_API and not os.getenv("MONGO_URI"):
            logger.warning("No MongoDB connection configured in environment (MONGO_URI / COSMOS_BASE_API / BASE_API_URL)")
        logger.info("Using Groq API provider with model %s (max_concurrency=%d, max_input_tokens=%d, max_output_tokens=%d)",
                    Config.GROQ_MODEL, Config.LLM_MAX_CONCURRENCY, Config.LLM_MAX_INPUT_TOKENS, Config.LLM_MAX_OUTPUT_TOKENS)

