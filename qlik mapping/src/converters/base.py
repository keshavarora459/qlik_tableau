import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .llm_client import GroqLLMClient


@dataclass
class ConvertedItem:
    name: str
    source: Dict[str, Any]
    fabric: Dict[str, Any]
    confidence: Dict[str, Any]


class ConversionContext:
    def __init__(
        self,
        app_id: str = "",
        tables: List[Dict[str, Any]] = None,
        grid_columns: int = 24,
        grid_rows: int = 12,
        sheet_grids: Dict[str, Any] = None,
        measures: List[Dict[str, Any]] = None,
        relationships: List[Dict[str, Any]] = None,
    ):
        self.app_id = app_id
        self.tables = tables or []
        self.grid_columns = grid_columns
        self.grid_rows = grid_rows
        self.sheet_grids = sheet_grids or {}
        # Measures and relationships are what let a visual's fields be bound
        # to real model entities in the prompt rather than guessed at
        # downstream by fuzzy string matching.
        self.measures = measures or []
        self.relationships = relationships or []
        self.llm_client = GroqLLMClient()
        self._schema_context: Optional[str] = None

    @property
    def schema_context(self) -> str:
        """The resolved model rendered for prompts, built once per run.

        One visual conversion per chart means this string would otherwise be
        rebuilt dozens of times per app for an identical result.
        """
        if self._schema_context is None:
            from services.schema_context import build_schema_context

            self._schema_context = build_schema_context(
                self.tables, measures=self.measures, relationships=self.relationships
            )
        return self._schema_context


class DummyConfidence:
    def score(self, *args, **kwargs) -> Dict[str, Any]:
        score = kwargs.get("llm_score", 0.95)
        score_100 = int(round(score * 100)) if score <= 1.0 else int(round(score))
        fabric = kwargs.get("fabric") or {}
        checks = [
            {"id": "visual_type_recognized", "status": "pass" if fabric.get("supported", True) else "fail"},
        ]
        unbound = kwargs.get("unbound_fields") or []
        if unbound:
            checks.append({
                "id": "fields_bound_to_model",
                "status": "fail",
                "detail": "unbound: " + ", ".join(str(u) for u in unbound[:5]),
            })
        else:
            checks.append({"id": "fields_bound_to_model", "status": "pass"})
        return {
            "score": score,
            "score_out_of_100": score_100,
            "percentage": f"{score_100}%",
            "band": "high" if score >= 0.85 else ("medium" if score >= 0.60 else "low"),
            "llm_score": score,
            "requires_review": score < 0.85 or not fabric.get("supported", True) or bool(unbound),
            "rationale": kwargs.get("rationale", ""),
        }


class BaseConverter:
    name: str = "base"
    validators = []

    def __init__(self):
        self.confidence = DummyConfidence()

    def item_name(self, item: Any) -> str:
        return "Item"

    def source_block(self, item: Any) -> Dict[str, Any]:
        return item if isinstance(item, dict) else {}

    def build_user_payload(self, item: Any, context: ConversionContext) -> Dict[str, Any]:
        return self.source_block(item)

    def read_result(self, parsed: Any, item: Any, context: ConversionContext) -> Dict[str, Any]:
        if not isinstance(parsed, dict):
            raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
        return parsed

    async def _call_llm(
        self, item: Any, context: ConversionContext, rules: list, system_prompt: str, schema: dict
    ) -> Dict[str, Any]:
        payload = self.build_user_payload(item, context)
        user_prompt = (
            f"Convert the following item based on the rules.\nItem Data:\n"
            f"{json.dumps(payload, indent=2, default=str)}\n\nRules:\n{json.dumps(rules, indent=2)}"
        )
        return await context.llm_client.generate_structured_response(
            system_prompt, user_prompt, schema, stage=getattr(self, "name", "visuals")
        )

    async def _call_llm_prompts(
        self, context: ConversionContext, system_prompt: str, user_prompt: str, schema: dict
    ) -> Dict[str, Any]:
        """Structured call with prompts built elsewhere (services/prompt_builder).

        Kept alongside `_call_llm` so converters that need schema-grounded
        prompts aren't forced through the generic "here is the item, here are
        the rules" envelope.
        """
        return await context.llm_client.generate_structured_response(
            system_prompt, user_prompt, schema, stage=getattr(self, "name", "visuals")
        )

    async def convert_one(self, item: Any, context: ConversionContext) -> ConvertedItem:
        raise NotImplementedError("Subclasses must implement convert_one")
