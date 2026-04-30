from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Intent:
    question_type: str
    keywords: list[str]
    aspect: str
    ambiguous: bool = False


class NLUnderstandingAgent:
    def run(self, question: str) -> Intent:
        """TODO(student): convert question to structured intent."""
        return Intent(question_type="general", keywords=[], aspect="general", ambiguous=False)


class SecurityAgent:
    def run(self, question: str, intent: Intent) -> dict[str, str]:
        """
        Return:
        {
            "decision": "ALLOW" | "REJECT",
            "reason": "..."
        }
        """
        blocked_patterns = [
            "delete",
            "drop",
            "merge",
            "create",
            "set ",
            "bypass",
            "ignore previous",
            "dump all",
        ]
        q = question.lower()
        if any(p in q for p in blocked_patterns):
            return {"decision": "REJECT", "reason": "Unsafe query pattern."}
        return {"decision": "ALLOW", "reason": "Passed security check."}


class QueryPlannerAgent:
    def run(self, intent: Intent) -> dict[str, Any]:
        """TODO(student): build plan that fits A4 schema only."""
        return {
            "strategy": "typed_then_broad",
            "keywords": intent.keywords,
            "aspect": intent.aspect,
        }


class QueryExecutionAgent:
    def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        """TODO(student): execute Neo4j read-only query and return rows/error."""
        return {"rows": [], "error": "not_implemented"}


class DiagnosisAgent:
    def run(self, execution: dict[str, Any]) -> dict[str, str]:
        if execution.get("error"):
            return {"label": "QUERY_ERROR", "reason": str(execution["error"])}
        if not execution.get("rows"):
            return {"label": "NO_DATA", "reason": "No matching rule in KG."}
        return {"label": "SUCCESS", "reason": "Query succeeded."}


class QueryRepairAgent:
    def run(self, diagnosis: dict[str, str], original_plan: dict[str, Any], intent: Intent) -> dict[str, Any]:
        """TODO(student): return revised plan that differs from original."""
        repaired = dict(original_plan)
        repaired["strategy"] = "fulltext_only"
        return repaired


class ExplanationAgent:
    def run(
        self,
        question: str,
        intent: Intent,
        security: dict[str, str],
        diagnosis: dict[str, str],
        answer: str,
        repair_attempted: bool,
    ) -> str:
        return (
            f"Intent={intent.question_type}, Security={security['decision']}, "
            f"Diagnosis={diagnosis['label']}, Repair={repair_attempted}. "
            f"Answer: {answer}"
        )


def build_template_pipeline() -> dict[str, Any]:
    """Factory for student use in query_system_multiagent_template.py."""
    return {
        "nlu": NLUnderstandingAgent(),
        "security": SecurityAgent(),
        "planner": QueryPlannerAgent(),
        "executor": QueryExecutionAgent(),
        "diagnosis": DiagnosisAgent(),
        "repair": QueryRepairAgent(),
        "explanation": ExplanationAgent(),
    }
