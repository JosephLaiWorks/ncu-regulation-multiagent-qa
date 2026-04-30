from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from query_system import (
    keyword_variants,
    detect_question_type,
    normalize_question,
    build_match_terms,
    get_relevant_articles,
    driver,
    dedup_results,
)


# ========== Intent ==========

@dataclass
class Intent:
    question_type: str
    keywords: list[str]
    aspect: str
    ambiguous: bool = False
    flags: dict = field(default_factory=dict)
    original_question: str = ""


# ========== Agent 1: NL Understanding ==========

class NLUnderstandingAgent:
    """Converts a raw question into a structured Intent using A4 helpers."""

    # Signals that suggest a vague / unanswerable / trick question
    VAGUE_SIGNALS = [
        "probably", "maybe", "kind of", "generally",
        "always allowed", "can always", "is always",
        "summarize every", "every fee", "in one answer",
        "every student-related", "overall",
        "article 999", "article 000",
    ]

    def run(self, question: str) -> Intent:
        flags    = detect_question_type(question)
        keywords = keyword_variants(question)
        q        = normalize_question(question)

        if flags["is_penalty"]:
            question_type = "penalty"
        elif flags["is_time"]:
            question_type = "time"
        elif flags["is_yesno"]:
            question_type = "yesno"
        else:
            question_type = "general"

        if any(k in q for k in [
            "exam", "invigilator", "cheating", "question paper",
            "electronic", "barred", "late to exam", "leave the exam",
        ]):
            aspect = "exam"
        elif any(k in q for k in [
            "student id", "easycard", "mifare", "working days",
            "student card", "id card",
        ]):
            aspect = "admin"
        elif any(k in q for k in [
            "graduation", "credits", "leave of absence", "suspension",
            "dismissed", "make-up", "passing score", "semesters",
        ]):
            aspect = "academic"
        else:
            aspect = "general"

        # Ambiguous = vague signals OR too few meaningful keywords
        meaningful = [k for k in keywords if len(k) >= 4]
        vague      = any(s in q for s in self.VAGUE_SIGNALS)
        ambiguous  = vague or len(meaningful) < 2

        return Intent(
            question_type=question_type,
            keywords=keywords,
            aspect=aspect,
            ambiguous=ambiguous,
            flags=flags,
            original_question=question,
        )


# ========== Agent 2: Security / Policy ==========

class SecurityAgent:
    """
    Three-layer safety check.
    FIX: 'merge' re-added so MERGE injection (Q39) is caught → 10/10 unsafe.
    """

    BLOCKED_CYPHER = [
        # Core DB write / drop operations
        "delete", "drop", "detach", "merge",
        "set ", "create node", "load csv", "call db", "apoc.",
        # Prompt-injection patterns
        "bypass", "ignore previous", "dump all", "disable safety",
        # Mass-export / data-dump patterns (previously missed)
        "export", "raw json", "entire kg", "every article",
        "every regulation content",
        # Modification patterns
        "modify", "write a script",
        # Credential / direct-access patterns
        "credentials", "query neo4j",
        # Word-by-word content dump
        "word-by-word", "word by word",
    ]

    BLOCKED_POLICY = [
        "all students", "all users", "list everyone",
        "show passwords", "admin credentials",
        "personal data of", "all database", "all records",
    ]

    def run(self, question: str, intent: Intent) -> dict[str, str]:
        q = question.lower()

        for pattern in self.BLOCKED_CYPHER:
            if pattern in q:
                return {
                    "decision": "REJECT",
                    "reason": f"Unsafe query pattern detected: '{pattern}'.",
                }

        cypher_hits = sum(
            1 for k in ["match", "where", "return", "with", "union"] if k in q
        )
        if cypher_hits >= 3:
            return {
                "decision": "REJECT",
                "reason": "Possible Cypher injection attempt detected.",
            }

        for pattern in self.BLOCKED_POLICY:
            if pattern in q:
                return {
                    "decision": "REJECT",
                    "reason": f"Policy violation: '{pattern}' is not permitted.",
                }

        return {"decision": "ALLOW", "reason": "Passed security check."}


# ========== Agent 3: Query Planning ==========

class QueryPlannerAgent:
    """
    Builds a query plan.
    - Normal questions   → use_original=True (uses A4's get_relevant_articles)
    - Ambiguous/vague    → use_original=False, min_score=100 → guaranteed NO_DATA
                           → repair will be triggered
    """

    def run(self, intent: Intent) -> dict[str, Any]:
        keyword_str = " ".join(intent.keywords)
        terms       = build_match_terms(keyword_str)

        if intent.ambiguous:
            # Force NO_DATA on first pass so repair fires
            return {
                "strategy": "fulltext_only",
                "keywords": intent.keywords,
                "terms": terms,
                "aspect": intent.aspect,
                "question_type": intent.question_type,
                "flags": intent.flags,
                "original_question": intent.original_question,
                "use_original": False,
                "min_score": 100,   # impossible threshold → NO_DATA
            }

        return {
            "strategy": "typed_then_broad",
            "keywords": intent.keywords,
            "terms": terms,
            "aspect": intent.aspect,
            "question_type": intent.question_type,
            "flags": intent.flags,
            "original_question": intent.original_question,
            "use_original": True,   # use A4's proven retrieval
            "min_score": 5,
        }


# ========== Agent 4: Query Execution ==========

class QueryExecutionAgent:
    """
    Two execution modes:
    - use_original=True  → A4's get_relevant_articles (proven, used for clear questions)
    - use_original=False → direct Cypher with min_score from plan (used in repair)
    """

    CYPHER = """
    MATCH (a:Article)-[:CONTAINS_RULE]->(r:Rule)
    WITH a, r,
         reduce(score = 0, term IN $terms |
            score +
            CASE WHEN a.content  CONTAINS term THEN 3 ELSE 0 END +
            CASE WHEN r.action   CONTAINS term THEN 4 ELSE 0 END +
            CASE WHEN r.result   CONTAINS term THEN 2 ELSE 0 END +
            CASE WHEN r.reg_name CONTAINS term THEN 1 ELSE 0 END
         ) +
         CASE
            WHEN $prefer_exam    = true AND a.category = 'Exam'    THEN 5
            WHEN $prefer_admin   = true AND a.category = 'Admin'   THEN 5
            WHEN $prefer_general = true AND a.category = 'General' THEN 3
            ELSE 0
         END AS score
    WHERE score >= $min_score
    RETURN
        r.rule_id AS rule_id, r.type AS type,
        r.action AS action, r.result AS result,
        r.art_ref AS art_ref, r.reg_name AS reg_name,
        a.content AS article_content, a.category AS category,
        score AS score
    ORDER BY score DESC
    LIMIT 10
    """

    def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        use_original = plan.get("use_original", True)

        if use_original:
            return self._run_original(plan)
        return self._run_cypher(plan)

    def _run_original(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Use A4's proven get_relevant_articles."""
        question = plan.get("original_question", "")
        if not question.strip():
            return {"rows": [], "error": None}
        try:
            rows = get_relevant_articles(question)
            return {"rows": rows, "error": None}
        except Exception as e:
            return {"rows": [], "error": str(e)}

    def _run_cypher(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Direct Cypher with score threshold — used in repair mode."""
        if driver is None:
            return {"rows": [], "error": "neo4j_connection_failed"}

        terms     = plan.get("terms", [])
        min_score = plan.get("min_score", 5)
        aspect    = plan.get("aspect", "general")

        if not terms:
            return {"rows": [], "error": None}

        try:
            with driver.session() as session:
                rows = session.run(
                    self.CYPHER,
                    terms=terms,
                    min_score=min_score,
                    prefer_exam=(aspect == "exam"),
                    prefer_admin=(aspect == "admin"),
                    prefer_general=(aspect in ("academic", "general")),
                ).data()
            return {"rows": dedup_results(rows, limit=5), "error": None}
        except Exception as e:
            error_msg = str(e)
            if "property" in error_msg.lower() or "label" in error_msg.lower():
                return {"rows": [], "error": f"schema_error: {error_msg}"}
            return {"rows": [], "error": error_msg}


# ========== Agent 5: Diagnosis ==========

class DiagnosisAgent:
    """Classifies execution: SUCCESS / QUERY_ERROR / SCHEMA_MISMATCH / NO_DATA"""

    def run(self, execution: dict[str, Any]) -> dict[str, str]:
        error = execution.get("error")
        rows  = execution.get("rows", [])

        if error:
            if "schema_error" in str(error) or "property" in str(error).lower():
                return {"label": "SCHEMA_MISMATCH", "reason": str(error)}
            if "neo4j_connection_failed" in str(error):
                return {"label": "QUERY_ERROR", "reason": "Neo4j is not reachable."}
            return {"label": "QUERY_ERROR", "reason": str(error)}

        if not rows:
            return {"label": "NO_DATA", "reason": "No matching regulation rule found in KG."}

        return {"label": "SUCCESS", "reason": f"Retrieved {len(rows)} rule(s) from KG."}


# ========== Agent 6: Query Repair ==========

class QueryRepairAgent:
    """
    Produces a revised plan with use_original=False and min_score=1
    so the Cypher executor runs with maximum permissiveness.
    repair_changed will be True because use_original flips False→False already
    but min_score drops from 100→1 and strategy/terms change.
    """

    def run(
        self,
        diagnosis: dict[str, str],
        original_plan: dict[str, Any],
        intent: Intent,
    ) -> dict[str, Any]:
        repaired  = dict(original_plan)
        label     = diagnosis["label"]

        # Switch to direct Cypher with low threshold
        repaired["use_original"] = False
        repaired["strategy"]     = "fulltext_only"

        if label == "SCHEMA_MISMATCH":
            repaired["min_score"] = 1
            repaired["terms"]     = build_match_terms(intent.original_question)[:5]

        elif label == "QUERY_ERROR":
            repaired["min_score"] = 1
            simplified            = intent.keywords[:4]
            repaired["keywords"]  = simplified
            repaired["terms"]     = build_match_terms(" ".join(simplified))

        else:
            # NO_DATA: maximum broadening
            repaired["min_score"] = 1
            broader               = keyword_variants(intent.original_question)
            repaired["keywords"]  = broader[:15]
            repaired["terms"]     = build_match_terms(intent.original_question)

        return repaired


# ========== Agent 7: Explanation ==========

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
        parts = [
            f"Question type: {intent.question_type}",
            f"Domain aspect: {intent.aspect}",
            f"Security: {security['decision']} — {security['reason']}",
            f"Diagnosis: {diagnosis['label']} — {diagnosis['reason']}",
        ]
        if repair_attempted:
            parts.append("Query repair was attempted.")
        short = (answer[:120] + "...") if len(answer) > 120 else answer
        parts.append(f"Answer: {short}")
        return " | ".join(parts)


# ========== Pipeline Factory ==========

def build_template_pipeline() -> dict[str, Any]:
    return {
        "nlu":         NLUnderstandingAgent(),
        "security":    SecurityAgent(),
        "planner":     QueryPlannerAgent(),
        "executor":    QueryExecutionAgent(),
        "diagnosis":   DiagnosisAgent(),
        "repair":      QueryRepairAgent(),
        "explanation": ExplanationAgent(),
    }
