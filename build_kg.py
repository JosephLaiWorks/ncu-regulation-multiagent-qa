"""Minimal KG builder template for Assignment 4.

Keep this contract unchanged:
- Graph: (Regulation)-[:HAS_ARTICLE]->(Article)-[:CONTAINS_RULE]->(Rule)
- Article: number, content, reg_name, category
- Rule: rule_id, type, action, result, art_ref, reg_name
- Fulltext indexes: article_content_idx, rule_idx
- SQLite file: ncu_regulations.db
"""

import os
import re
import sqlite3
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

from llm_loader import load_local_llm


# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
    os.getenv("NEO4J_USER", "neo4j"),
    os.getenv("NEO4J_PASSWORD", "password"),
)


def normalize_text(text: str) -> str:
    """Normalize whitespace and punctuation for more stable parsing."""
    if not text:
        return ""

    text = text.replace("\u3000", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def split_sentences(content: str) -> list[str]:
    """Split Chinese regulation text into sentence-like units."""
    content = normalize_text(content)
    if not content:
        return []

    # Split by Chinese / English punctuation and line breaks.
    parts = re.split(r"[；;。！？!\?\n]+", content)
    sentences = [p.strip(" ：:，,") for p in parts if p.strip(" ：:，,")]
    return sentences


def infer_rule_type(text: str) -> str:
    """Infer a coarse rule type from text."""
    keywords = {
        "eligibility": ["得", "可", "可以", "符合", "申請", "辦理"],
        "prohibition": ["不得", "禁止", "不可", "不得有", "不得為"],
        "obligation": ["應", "應於", "應當", "須", "必須"],
        "penalty": ["罰", "懲處", "處分", "記過", "退學", "撤銷"],
        "exception": ["但", "但書", "例外", "不在此限"],
        "time_limit": ["期限", "逾期", "內", "前", "後"],
    }

    for rule_type, words in keywords.items():
        if any(word in text for word in words):
            return rule_type

    return "general"


def build_action_result(sentence: str) -> tuple[str, str]:
    """
    Build action/result pair from a sentence.
    Very simple heuristic:
    - action: what the subject should / can / must do
    - result: consequence / outcome / allowance / restriction
    """
    s = sentence.strip(" ：:，,")
    if not s:
        return "", ""

    # Priority 1: explicit prohibition
    for marker in ["不得", "禁止", "不可"]:
        if marker in s:
            left, right = s.split(marker, 1)
            left = left.strip(" ，,")
            right = right.strip(" ，,")
            action = f"{left}{marker}{right}".strip()
            result = "prohibited"
            return action, result

    # Priority 2: obligation
    for marker in ["應於", "應當", "應", "須", "必須"]:
        if marker in s:
            left, right = s.split(marker, 1)
            left = left.strip(" ，,")
            right = right.strip(" ，,")
            action = f"{left}{marker}{right}".strip()
            result = "required"
            return action, result

    # Priority 3: permission / eligibility
    for marker in ["得", "可以", "可"]:
        if marker in s:
            left, right = s.split(marker, 1)
            left = left.strip(" ，,")
            right = right.strip(" ，,")
            action = f"{left}{marker}{right}".strip()
            result = "allowed"
            return action, result

    # Priority 4: penalties / consequences
    for marker in ["者，", "者,", "者應", "應予", "處", "懲處", "記過", "撤銷", "退學"]:
        if marker in s:
            action = s
            result = "penalty_or_consequence"
            return action, result

    # Default
    return s, "stated"


def extract_entities(article_number: str, reg_name: str, content: str) -> dict[str, Any]:
    """
    Deterministic extraction:
    return {"rules": [ {type, action, result}, ... ]}
    """
    sentences = split_sentences(content)
    rules: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 4:
            continue

        rule_type = infer_rule_type(sentence)
        action, result = build_action_result(sentence)

        action = action.strip()
        result = result.strip()

        if not action or not result:
            continue

        key = (rule_type, action, result)
        if key in seen:
            continue
        seen.add(key)

        rules.append(
            {
                "type": rule_type,
                "action": action,
                "result": result,
            }
        )

    return {"rules": rules}


def build_fallback_rules(article_number: str, content: str) -> list[dict[str, str]]:
    """
    Fallback:
    If no rule extracted, create one general rule from truncated article content.
    """
    content = normalize_text(content)
    if not content:
        return []

    short_text = content[:180].strip()
    if not short_text:
        return []

    return [
        {
            "type": "general",
            "action": short_text,
            "result": "stated",
        }
    ]


# SQLite tables used:
# - regulations(reg_id, name, category)
# - articles(reg_id, article_number, content)


def build_graph() -> None:
    """Build KG from SQLite into Neo4j using the fixed assignment schema."""
    sql_conn = sqlite3.connect("ncu_regulations.db")
    cursor = sql_conn.cursor()
    driver = GraphDatabase.driver(URI, auth=AUTH)

    # Optional: warm up local LLM (keep for assignment compatibility)
    load_local_llm()

    with driver.session() as session:
        # Fixed strategy: clear existing graph data before rebuilding.
        session.run("MATCH (n) DETACH DELETE n")

        # 1) Read regulations and create Regulation nodes.
        cursor.execute("SELECT reg_id, name, category FROM regulations")
        regulations = cursor.fetchall()
        reg_map: dict[int, tuple[str, str]] = {}

        for reg_id, name, category in regulations:
            reg_map[reg_id] = (name, category)
            session.run(
                "MERGE (r:Regulation {id:$rid}) SET r.name=$name, r.category=$cat",
                rid=reg_id,
                name=name,
                cat=category,
            )

        # 2) Read articles and create Article + HAS_ARTICLE.
        cursor.execute("SELECT reg_id, article_number, content FROM articles")
        articles = cursor.fetchall()

        for reg_id, article_number, content in articles:
            reg_name, reg_category = reg_map.get(reg_id, ("Unknown", "Unknown"))
            session.run(
                """
                MATCH (r:Regulation {id: $rid})
                CREATE (a:Article {
                    number:   $num,
                    content:  $content,
                    reg_name: $reg_name,
                    category: $reg_category
                })
                MERGE (r)-[:HAS_ARTICLE]->(a)
                """,
                rid=reg_id,
                num=article_number,
                content=content,
                reg_name=reg_name,
                reg_category=reg_category,
            )

        # 3) Create full-text index on Article content.
        session.run(
            """
            CREATE FULLTEXT INDEX article_content_idx IF NOT EXISTS
            FOR (a:Article) ON EACH [a.content]
            """
        )

        rule_counter = 0
        dedup_global: set[tuple[str, str, str, str, str]] = set()

        # 4) Iterate through all articles and build Rule nodes + CONTAINS_RULE.
        cursor.execute("SELECT reg_id, article_number, content FROM articles")
        articles_for_rules = cursor.fetchall()

        for reg_id, article_number, content in articles_for_rules:
            reg_name, _ = reg_map.get(reg_id, ("Unknown", "Unknown"))
            content = normalize_text(content)

            extracted = extract_entities(article_number, reg_name, content)
            rules = extracted.get("rules", []) if extracted else []

            if not rules:
                rules = build_fallback_rules(article_number, content)

            local_seen: set[tuple[str, str, str]] = set()

            for rule in rules:
                rule_type = str(rule.get("type", "general")).strip() or "general"
                action = str(rule.get("action", "")).strip()
                result = str(rule.get("result", "")).strip()

                if not action or not result:
                    continue

                # Per-article dedup
                local_key = (rule_type, action, result)
                if local_key in local_seen:
                    continue
                local_seen.add(local_key)

                # Global logical dedup
                global_key = (reg_name, article_number, rule_type, action, result)
                if global_key in dedup_global:
                    continue
                dedup_global.add(global_key)

                rule_counter += 1
                rule_id = f"{reg_id}_{article_number}_{rule_counter}"

                session.run(
                    """
                    MATCH (a:Article {
                        number: $num,
                        reg_name: $reg_name
                    })
                    CREATE (r:Rule {
                        rule_id: $rule_id,
                        type:    $type,
                        action:  $action,
                        result:  $result,
                        art_ref: $art_ref,
                        reg_name:$reg_name
                    })
                    MERGE (a)-[:CONTAINS_RULE]->(r)
                    """,
                    num=article_number,
                    reg_name=reg_name,
                    rule_id=rule_id,
                    type=rule_type,
                    action=action,
                    result=result,
                    art_ref=article_number,
                )

        # 5) Create full-text index on Rule fields.
        session.run(
            """
            CREATE FULLTEXT INDEX rule_idx IF NOT EXISTS
            FOR (r:Rule) ON EACH [r.action, r.result]
            """
        )

        # 6) Coverage audit
        coverage = session.run(
            """
            MATCH (a:Article)
            OPTIONAL MATCH (a)-[:CONTAINS_RULE]->(r:Rule)
            WITH a, count(r) AS rule_count
            RETURN count(a) AS total_articles,
                   sum(CASE WHEN rule_count > 0 THEN 1 ELSE 0 END) AS covered_articles,
                   sum(CASE WHEN rule_count = 0 THEN 1 ELSE 0 END) AS uncovered_articles
            """
        ).single()

        total_articles = int((coverage or {}).get("total_articles", 0) or 0)
        covered_articles = int((coverage or {}).get("covered_articles", 0) or 0)
        uncovered_articles = int((coverage or {}).get("uncovered_articles", 0) or 0)

        print(
            f"[Coverage] covered={covered_articles}/{total_articles}, "
            f"uncovered={uncovered_articles}, total_rules={rule_counter}"
        )

    driver.close()
    sql_conn.close()


if __name__ == "__main__":
    build_graph()