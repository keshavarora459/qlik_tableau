"""Pipeline Object Lifecycle Inventory and Loss Matrix."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ObjectStage(str, Enum):
    EXTRACTED = "EXTRACTED"
    NORMALIZED = "NORMALIZED"
    MAPPED = "MAPPED"
    IR_CREATED = "IR_CREATED"
    VALIDATED = "VALIDATED"
    GENERATED = "GENERATED"


@dataclass
class TrackedObject:
    object_id: str
    object_type: str  # "table", "column", "measure", "relationship", "visual", "filter", "variable"
    name: str
    stage_statuses: Dict[str, bool] = field(default_factory=dict)
    final_status: str = "success"
    loss_reason: Optional[str] = None
    lineage: Dict[str, Any] = field(default_factory=dict)


class PipelineInventory:
    """Tracks every source artifact through every stage to prevent silent object loss."""

    def __init__(self):
        self._objects: Dict[str, TrackedObject] = {}

    def track(
        self,
        object_id: str,
        object_type: str,
        name: str,
        stage: ObjectStage = ObjectStage.EXTRACTED,
        passed: bool = True,
        loss_reason: Optional[str] = None,
        lineage: Optional[Dict[str, Any]] = None,
    ) -> None:
        key = f"{object_type}:{object_id}"
        if key not in self._objects:
            self._objects[key] = TrackedObject(
                object_id=object_id,
                object_type=object_type,
                name=name,
                lineage=lineage or {},
            )
        obj = self._objects[key]
        obj.stage_statuses[stage.value] = passed
        if not passed:
            obj.final_status = "failed"
            obj.loss_reason = loss_reason or f"Failed at stage {stage.value}"

    def get_summary(self) -> Dict[str, Any]:
        counts_by_type: Dict[str, Dict[str, int]] = {}
        lost_objects: List[Dict[str, Any]] = []

        for key, obj in self._objects.items():
            t = obj.object_type
            if t not in counts_by_type:
                counts_by_type[t] = {
                    "extracted": 0,
                    "mapped": 0,
                    "validated": 0,
                    "lost": 0,
                }
            counts_by_type[t]["extracted"] += 1
            if obj.stage_statuses.get(ObjectStage.MAPPED.value, True):
                counts_by_type[t]["mapped"] += 1
            if obj.stage_statuses.get(ObjectStage.VALIDATED.value, True):
                counts_by_type[t]["validated"] += 1
            if obj.final_status == "failed" or not obj.stage_statuses.get(ObjectStage.VALIDATED.value, True):
                counts_by_type[t]["lost"] += 1
                lost_objects.append({
                    "object_id": obj.object_id,
                    "object_type": obj.object_type,
                    "name": obj.name,
                    "loss_reason": obj.loss_reason,
                })

        total_extracted = sum(c["extracted"] for c in counts_by_type.values())
        total_lost = sum(c["lost"] for c in counts_by_type.values())

        return {
            "total_extracted": total_extracted,
            "total_lost": total_lost,
            "fidelity_ratio": round((total_extracted - total_lost) / max(total_extracted, 1), 4),
            "counts_by_type": counts_by_type,
            "lost_objects": lost_objects,
        }
