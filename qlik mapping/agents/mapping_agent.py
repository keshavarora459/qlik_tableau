import logging
from typing import Dict, List, Union, Any, Optional
try:
    from autogen import ConversableAgent
except ImportError:
    class ConversableAgent:
        def __init__(self, *args, **kwargs):
            pass
from logging_config import AgentLoggerAdapter
from src.converters.llm_client import GroqLLMClient
from services.dax_converter import DAXConverter
from services.input_normalizer import normalize_measures
from config import Config

logger = logging.getLogger(__name__)

class MappingAgent(ConversableAgent):
    """Agent responsible for extracting and converting Qlik measures to Power BI DAX formats."""
    def __init__(self):
        logger.info("Initializing MappingAgent")
        if Config.USE_GROQ:
            config_list = [{
                "model": Config.GROQ_MODEL,
                "api_key": Config.GROQ_API_KEY or "dummy_key_for_offline",
                "api_type": "openai"
            }]
        else:
            config_list = [{
                "model": Config.AZURE_OPENAI_DEPLOYMENT,
                "api_key": Config.AZURE_OPENAI_API_KEY,
                "base_url": Config.AZURE_OPENAI_ENDPOINT,
                "api_version": Config.AZURE_OPENAI_API_VERSION,
                "api_type": "azure"
            }]

        super().__init__(
            name="MappingAgent",
            system_message="You are an expert in converting Qlik Sense data to Power BI formats.",
            llm_config={
                "config_list": config_list,
                "temperature": 0,
                "max_tokens": 2000
            }
        )
        self.llm_client = GroqLLMClient()
        self.dax_converter = DAXConverter()
        self.logger = AgentLoggerAdapter(logging.getLogger(__name__), {'agent_name': 'MappingAgent'})

    async def extract_renames(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract column rename definitions from raw parsing result."""
        try:
            if not isinstance(data, dict):
                return {'error': "Input data is not a dictionary"}
            column_renames = data.get('column_renames', [])
            if not isinstance(column_renames, list) or not column_renames:
                return {'not_found_msg': "No renames found"}
            return {'table_column_renames': column_renames}
        except Exception as e:
            self.logger.error(f"Error extracting renames: {e}")
            return {'error': str(e)}

    async def extract_measures(self, data: Union[Dict[str, Any], List[Any]], known_tables: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Extract and convert valid measure definitions into Contract 2.0 format."""
        try:
            source = data.get('measures', []) if isinstance(data, dict) else data
            # Qlik keeps the name at qMeasure.qLabel, not at the top level.
            # The previous `"name" in item` guard silently dropped every raw
            # measure — 29 in, 0 out, with no error raised.
            raw_measures = normalize_measures(source)
            if not raw_measures:
                self.logger.warning("No measures could be normalized from the payload")
                return []

            tables_ctx = known_tables or []
            converted = []
            for item in raw_measures:
                try:
                    converted.append(self.dax_converter.convert_measure(item, tables_ctx))
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(f"Measure '{item.get('name')}' failed to convert: {exc}")

            self.logger.info(f"Converted {len(converted)}/{len(raw_measures)} measures")
            return converted
        except Exception as e:
            self.logger.error(f"Error extracting measures: {e}")
            return []
