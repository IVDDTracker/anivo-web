"""Strict schema for machine-used LLM output (DECISIONS.md D-018).

An LLM may only refine classification fields of an ExternalEvent. Its output
MUST validate against this schema; malformed output is rejected and the
rule-based classification stands. Nothing here can reach an executor:
these are data fields, not commands.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EventCategory


class LlmEventClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unexpected fields → reject entirely

    category: EventCategory
    assets: list[str] = Field(default_factory=list, max_length=8)
    sentiment: float = Field(ge=-1.0, le=1.0)
    magnitude: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=300)
    reasoning: str = Field(default="", max_length=500)
