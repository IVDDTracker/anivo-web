"""LLM output validation: strict schema, malformed → rejected, never a command."""

from __future__ import annotations

from app.data.llm_enrichment import parse_classification
from app.models.enums import EventCategory


def test_valid_output_parses():
    out = parse_classification(
        'Here is the JSON: {"category": "HACK", "assets": ["BTCUSDT"], "sentiment": -0.8,'
        ' "magnitude": 0.9, "summary": "Exchange hot wallet drained."}')
    assert out is not None and out.category == EventCategory.HACK


def test_malformed_json_rejected():
    assert parse_classification("BUY BITCOIN NOW!!!") is None
    assert parse_classification('{"category": "HACK", "sentiment": ') is None


def test_out_of_range_rejected():
    assert parse_classification(
        '{"category": "HACK", "assets": [], "sentiment": -5, "magnitude": 0.5,'
        ' "summary": "x"}') is None


def test_unknown_category_rejected():
    assert parse_classification(
        '{"category": "EXECUTE_TRADE", "assets": [], "sentiment": 0, "magnitude": 0.5,'
        ' "summary": "x"}') is None


def test_extra_fields_rejected():
    """An LLM cannot smuggle execution-like fields through the schema."""
    assert parse_classification(
        '{"category": "HACK", "assets": [], "sentiment": 0, "magnitude": 0.5,'
        ' "summary": "x", "action": "BUY", "quantity": 5}') is None


def test_schema_has_no_execution_fields():
    from app.models.llm import LlmEventClassification

    fields = set(LlmEventClassification.model_fields)
    assert fields == {"category", "assets", "sentiment", "magnitude", "summary", "reasoning"}
