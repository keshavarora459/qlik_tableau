import asyncio
import logging
from typing import Dict, Any, Optional
from config import Config

logger = logging.getLogger(__name__)

# Fallback human-friendly explanations for every major mapping stage
STAGE_FALLBACKS = {
    "start": {
        "action": "Starting Application Migration",
        "summary": "We began analyzing your analytics application to prepare all data models, formulas, and visual reports for Microsoft Fabric."
    },
    "connections": {
        "action": "Configuring Data Source Connections",
        "summary": "We identified and configured your underlying database connections so your new Power BI reports can refresh data seamlessly."
    },
    "tables": {
        "action": "Structuring Data Tables and Schemas",
        "summary": "We organized your business data tables and columns into a clean, optimized structure ready for modern cloud reporting."
    },
    "columns": {
        "action": "Standardizing Data Types and Formats",
        "summary": "We verified and standardized all column types—ensuring dates, currency values, and quantities calculate accurately."
    },
    "variables": {
        "action": "Unpacking Dynamic Variables and Formulas",
        "summary": "We resolved reusable formula variables and dynamic logic so every calculation is fully transparent and accurate."
    },
    "measures": {
        "action": "Translating Business Formulas to DAX",
        "summary": "We translated your business calculations, totals, and KPI metrics into standard Power BI DAX formulas."
    },
    "relationships": {
        "action": "Connecting Related Data Tables",
        "summary": "We established relationships and cross-filtering between tables so your dashboard charts can interact harmoniously."
    },
    "visuals": {
        "action": "Rebuilding Dashboard Charts and Visuals",
        "summary": "We transformed your original charts, graphs, summary cards, and multi-tab layouts into modern interactive report pages."
    },
    "dimensions_filters": {
        "action": "Configuring Slicers and Interactive Filters",
        "summary": "We mapped search dropdowns, date sliders, and breakdown categories so business users can effortlessly explore and slice data."
    },
    "complete": {
        "action": "Finalizing and Validating Semantic Model",
        "summary": "We completed quality checks on your semantic model and saved the ready-to-deploy report package into MongoDB."
    }
}

class ActionHumanizer:
    """Uses LLM to convert technical milestone events into friendly, non-technical, human-readable explanations."""
    
    def __init__(self):
        self._client = None
        self._initialized = False

    def _get_client(self):
        if not self._initialized:
            try:
                from src.converters.llm_client import GroqLLMClient
                if Config.GROQ_API_KEY:
                    self._client = GroqLLMClient()
            except Exception as e:
                logger.debug(f"LLM client initialization skipped for ActionHumanizer: {e}")
            self._initialized = True
        return self._client

    async def humanize(self, stage_key: str, technical_action: str, technical_details: str, app_name: Optional[str] = None) -> Dict[str, str]:
        """Generate friendly non-technical title and description for an agent action."""
        fallback = STAGE_FALLBACKS.get(stage_key, {
            "action": technical_action or "Processing Migration Stage",
            "summary": technical_details or "Making progress on converting your analytics dashboard to Microsoft Fabric."
        })

        client = self._get_client()
        if not client or not Config.GROQ_API_KEY:
            if app_name and stage_key == "start":
                return {
                    "action": f"Starting Migration for {app_name}",
                    "summary": f"We began analyzing {app_name} to prepare all data models, formulas, and visual reports for Microsoft Fabric."
                }
            return fallback

        system_prompt = (
            "You are an empathetic, clear, and professional AI narrator explaining an automated analytics migration "
            "to a non-technical business user, manager, or executive.\n"
            "Given a technical development milestone, generate a friendly, non-technical description in plain English.\n"
            "Rules:\n"
            "1. Output valid JSON with two keys: 'action' (a clear headline of 4-8 words) and 'summary' (1-2 sentences explaining what was accomplished and why it matters).\n"
            "2. Never use intimidating jargon (avoid terms like 'AST', 'regex', 'JSON schema', 'Pydantic', 'endpoint', 'TMDL syntax', 'serialization').\n"
            "3. Focus on business value (e.g., accuracy, speed, interactivity, seamless reporting)."
        )

        user_prompt = (
            f"Stage: {stage_key}\n"
            f"App Name: {app_name or 'Analytics Dashboard'}\n"
            f"Technical Action: {technical_action}\n"
            f"Technical Details: {technical_details}\n\n"
            "Produce the JSON object:"
        )

        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "summary": {"type": "string"}
            },
            "required": ["action", "summary"]
        }

        try:
            response = await asyncio.wait_for(
                client.generate_structured_response(system_prompt, user_prompt, schema),
                timeout=4.0
            )
            if isinstance(response, dict) and response.get("action") and response.get("summary"):
                return {
                    "action": str(response["action"]).strip(),
                    "summary": str(response["summary"]).strip()
                }
        except Exception as e:
            logger.debug(f"Action humanization fallback used for stage '{stage_key}': {e}")

        return fallback

# Global singleton instance
action_humanizer = ActionHumanizer()
