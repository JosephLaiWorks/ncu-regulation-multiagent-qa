from __future__ import annotations

from typing import Any

from agents.a5_template import build_template_pipeline
from query_system import generate_answer   # A4's proven answer generator


PIPELINE = build_template_pipeline()


def answer_question(question: str) -> dict[str, Any]:
    """
    Multi-agent QA entry point.

    Output contract for auto_test_a5.py:
    {
        "answer":           str,
        "safety_decision":  "ALLOW" | "REJECT",
        "diagnosis":        "SUCCESS" | "QUERY_ERROR" | "SCHEMA_MISMATCH" | "NO_DATA",
        "repair_attempted": bool,
        "repair_changed":   bool,
        "explanation":      str,
    }
    """
    nlu               = PIPELINE["nlu"]
    security_agent    = PIPELINE["security"]
    planner           = PIPELINE["planner"]
    executor          = PIPELINE["executor"]
    diagnosis_agent   = PIPELINE["diagnosis"]
    repair_agent      = PIPELINE["repair"]
    explanation_agent = PIPELINE["explanation"]

    # ── Agent 1: NL Understanding ──────────────────────────────────────────
    intent = nlu.run(question)

    # ── Agent 2: Security / Policy ─────────────────────────────────────────
    security = security_agent.run(question, intent)

    if security["decision"] == "REJECT":
        diagnosis = {"label": "QUERY_ERROR", "reason": "Blocked by security policy."}
        answer    = "This request has been rejected by the security policy."
        explanation = explanation_agent.run(
            question, intent, security, diagnosis, answer, False
        )
        return {
            "answer": answer,
            "safety_decision": "REJECT",
            "diagnosis": diagnosis["label"],
            "repair_attempted": False,
            "repair_changed": False,
            "explanation": explanation,
        }

    # ── Agent 3: Query Planning ────────────────────────────────────────────
    plan = planner.run(intent)

    # ── Agent 4: Query Execution ───────────────────────────────────────────
    execution = executor.run(plan)

    # ── Agent 5: Diagnosis ─────────────────────────────────────────────────
    diagnosis = diagnosis_agent.run(execution)

    # ── Agent 6: Repair — at most one round ───────────────────────────────
    repair_attempted = False
    repair_changed   = False

    if diagnosis["label"] in {"QUERY_ERROR", "SCHEMA_MISMATCH", "NO_DATA"}:
        repair_attempted = True
        repaired_plan    = repair_agent.run(diagnosis, plan, intent)
        repair_changed   = repaired_plan != plan

        repaired_execution = executor.run(repaired_plan)
        repaired_diagnosis = diagnosis_agent.run(repaired_execution)

        # Accept repair if it improves the outcome
        if repaired_diagnosis["label"] == "SUCCESS" or (
            diagnosis["label"] == "QUERY_ERROR"
            and repaired_diagnosis["label"] == "NO_DATA"
        ):
            execution = repaired_execution
            diagnosis = repaired_diagnosis

    # ── Answer Generation (A4's generate_answer) ──────────────────────────
    if diagnosis["label"] == "SUCCESS":
        answer = generate_answer(question, execution["rows"])
    elif diagnosis["label"] == "NO_DATA":
        answer = "No matching regulation evidence was found in the Knowledge Graph."
    else:
        answer = "The query could not be resolved even after a repair attempt."

    # ── Agent 7: Explanation ───────────────────────────────────────────────
    explanation = explanation_agent.run(
        question, intent, security, diagnosis, answer, repair_attempted
    )

    return {
        "answer": answer,
        "safety_decision": "ALLOW",
        "diagnosis": diagnosis["label"],
        "repair_attempted": repair_attempted,
        "repair_changed": repair_changed,
        "explanation": explanation,
    }


def run_multiagent_qa(question: str) -> dict[str, Any]:
    return answer_question(question)


def run_qa(question: str) -> dict[str, Any]:
    return answer_question(question)


if __name__ == "__main__":
    print("=" * 55)
    print("NCU Regulation Multi-Agent QA System (Assignment 5)")
    print("=" * 55)
    while True:
        try:
            q = input("Question: ").strip()
            if not q or q.lower() in {"exit", "quit"}:
                break
            result = answer_question(q)
            print(f"  Answer     : {result['answer']}")
            print(f"  Safety     : {result['safety_decision']}")
            print(f"  Diagnosis  : {result['diagnosis']}")
            print(f"  Repair     : attempted={result['repair_attempted']}, changed={result['repair_changed']}")
            print(f"  Explanation: {result['explanation']}\n")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
