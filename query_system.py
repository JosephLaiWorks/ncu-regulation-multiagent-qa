"""Minimal KG query template for Assignment 4.

Keep these APIs unchanged for auto-test:
- generate_text(messages, max_new_tokens=220)
- get_relevant_articles(question)
- generate_answer(question, rule_results)

Keep Rule fields aligned with build_kg output:
rule_id, type, action, result, art_ref, reg_name
"""

import os
import re
from typing import Any

from neo4j import GraphDatabase
from dotenv import load_dotenv

from llm_loader import load_local_llm, get_tokenizer, get_raw_pipeline


# ========== 0) Initialization ==========
load_dotenv()

URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
AUTH = (
    os.getenv("NEO4J_USER", "neo4j"),
    os.getenv("NEO4J_PASSWORD", "password"),
)

# Avoid local proxy settings interfering with model/Neo4j access.
for key in ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    if key in os.environ:
        del os.environ[key]

try:
    driver = GraphDatabase.driver(URI, auth=AUTH)
    driver.verify_connectivity()
except Exception as e:
    print(f"⚠️ Neo4j connection warning: {e}")
    driver = None


# ========== 1) Helpers ==========
STOPWORDS = {
    "what", "is", "the", "a", "an", "of", "for", "to", "in", "on", "at", "by",
    "when", "where", "who", "whom", "which", "how", "many", "much", "long",
    "can", "may", "should", "must", "do", "does", "did", "are", "am", "be",
    "if", "and", "or", "with", "without", "from", "into", "about", "under",
    "student", "students", "university", "schooling", "my"
}

PHRASE_MAP = {
    "student id": ["學生證"],
    "student card": ["學生證"],
    "id card": ["學生證"],

    "forget student id": ["學生證", "未帶"],
    "forgot student id": ["學生證", "未帶"],
    "forgetting student id": ["學生證", "未帶"],
    "forget student card": ["學生證", "未帶"],
    "forgot student card": ["學生證", "未帶"],
    "forgetting student card": ["學生證", "未帶"],

    "leave of absence": ["休學"],
    "suspension of schooling": ["休學"],
    "maximum duration": ["最長", "期限"],
    "minimum duration": ["最短", "期限"],

    "penalty": ["處分", "懲處", "記過", "扣分", "零分"],
    "punishment": ["處分", "懲處", "記過"],
    "consequence": ["處分", "結果"],

    "late": ["遲到", "20 minutes"],
    "barred from the exam": ["20 minutes", "not be permitted to enter"],
    "leave the exam room": ["leave", "40 minutes", "not permitted to leave"],
    "question paper": ["exam papers", "not permitted to take"],
    "electronic devices": ["electronic receivers", "mobile phones"],
    "communication capabilities": ["mobile phones", "electronic receivers"],

    "cheating": ["copy", "pass notes", "zero grade", "作弊"],
    "copying": ["copy", "zero grade"],
    "passing notes": ["pass notes", "zero grade"],

    "working days": ["three workdays", "工作天"],
    "easycard": ["EasyCard", "悠遊卡"],
    "mifare": ["Mifare"],

    "credits": ["credits", "學分"],
    "graduation": ["graduation", "畢業"],
    "physical education": ["PE", "physical education"],
    "military training": ["military training"],
    "passing score": ["passing score", "60", "70"],
    "bachelor": ["bachelor", "undergraduate"],
    "undergraduate": ["undergraduate"],
    "graduate": ["graduate", "Master", "PhD"],
    "dismissed": ["withdraw from school", "expelled"],
    "make-up exam": ["make-up exam", "補考"],
}


def normalize_question(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize_question(question: str) -> list[str]:
    q = normalize_question(question)
    tokens = []
    for tok in q.split():
        if len(tok) <= 1:
            continue
        if tok in STOPWORDS:
            continue
        tokens.append(tok)
    return tokens


def keyword_variants(question: str) -> list[str]:
    q = normalize_question(question)
    terms: list[str] = []
    seen = set()

    def add_term(term: str) -> None:
        term = term.strip()
        if not term:
            return
        if term not in seen:
            seen.add(term)
            terms.append(term)

    add_term(question.strip())

    for phrase, mapped_terms in PHRASE_MAP.items():
        if phrase in q:
            add_term(phrase)
            for t in mapped_terms:
                add_term(t)

    for tok in tokenize_question(question):
        add_term(tok)

    token_map = {
        "penalty": ["處分", "懲處", "記過", "扣分", "零分"],
        "punishment": ["處分", "懲處"],
        "duration": ["期限", "最長", "最短"],
        "maximum": ["最長"],
        "minimum": ["最短"],
        "leave": ["leave", "休學", "請假"],
        "absence": ["absence", "缺課", "曠課"],
        "suspension": ["休學"],
        "id": ["學生證"],
        "card": ["證"],
        "exam": ["exam", "考試"],
        "credits": ["credits", "學分"],
        "credit": ["credit", "學分"],
        "graduation": ["graduation", "畢業"],
        "tuition": ["學費", "學雜費"],
        "scholarship": ["獎學金"],
        "easycard": ["EasyCard", "悠遊卡"],
        "mifare": ["Mifare"],
        "working": ["workdays"],
        "days": ["days", "workdays"],
        "late": ["late", "遲到", "20 minutes"],
        "leave": ["leave", "40 minutes"],
        "cheating": ["copy", "pass notes", "zero grade", "作弊"],
        "copying": ["copy"],
        "notes": ["pass notes"],
        "electronic": ["electronic receivers", "mobile phones"],
        "devices": ["devices", "mobile phones"],
        "question": ["question paper", "exam papers"],
        "paper": ["question paper", "exam papers"],
        "undergraduate": ["undergraduate"],
        "graduate": ["graduate"],
        "bachelor": ["bachelor"],
        "master": ["Master"],
        "phd": ["PhD"],
        "dismissed": ["withdraw from school", "expelled"],
        "make": ["make-up exam"],
    }

    for tok in tokenize_question(question):
        for mapped in token_map.get(tok, []):
            add_term(mapped)

    return terms[:20]


def detect_question_type(question: str) -> dict[str, bool]:
    q = normalize_question(question)
    return {
        "is_penalty": any(k in q for k in ["penalty", "punishment", "consequence", "demerit"]),
        "is_time": any(k in q for k in ["how many", "how long", "minutes", "working days", "duration", "maximum", "minimum"]),
        "is_yesno": q.startswith(("can ", "is ", "are ", "do ", "does ", "will ")),
    }


def dedup_results(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    seen = set()
    merged = []

    for row in sorted(rows, key=lambda x: x.get("score", 0), reverse=True):
        key = (
            row.get("rule_id", ""),
            row.get("art_ref", ""),
            row.get("reg_name", ""),
            row.get("action", ""),
            row.get("result", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
        if len(merged) >= limit:
            break

    return merged


def build_match_terms(question: str) -> list[str]:
    terms = keyword_variants(question)

    useful = []
    seen = set()

    for term in terms:
        term = term.strip()
        if not term:
            continue
        if term in seen:
            continue
        seen.add(term)

        if re.search(r"[\u4e00-\u9fff]", term) or len(term) >= 4:
            useful.append(term)

    return useful[:12]


# ========== 2) Public API ==========
def generate_text(messages: list[dict[str, str]], max_new_tokens: int = 220) -> str:
    tok = get_tokenizer()
    pipe = get_raw_pipeline()
    if tok is None or pipe is None:
        load_local_llm()
        tok = get_tokenizer()
        pipe = get_raw_pipeline()

    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    output = pipe(prompt, max_new_tokens=max_new_tokens)[0]["generated_text"]

    if output.startswith(prompt):
        output = output[len(prompt):]

    return output.strip()


def extract_entities(question: str) -> dict[str, Any]:
    return {
        "question_type": detect_question_type(question),
        "subject_terms": keyword_variants(question),
        "aspect": "general",
    }


def build_typed_cypher(entities: dict[str, Any]) -> tuple[str, str]:
    cypher_typed = ""
    cypher_broad = ""
    return cypher_typed, cypher_broad


def get_relevant_articles(question: str) -> list[dict[str, Any]]:
    if driver is None:
        return []

    q = normalize_question(question)
    flags = detect_question_type(question)
    terms = build_match_terms(question)
    if not terms:
        return []

    cypher = """
    MATCH (a:Article)-[:CONTAINS_RULE]->(r:Rule)
    WITH a, r,
         reduce(score = 0, term IN $terms |
            score +
            CASE WHEN a.content CONTAINS term THEN 3 ELSE 0 END +
            CASE WHEN r.action CONTAINS term THEN 4 ELSE 0 END +
            CASE WHEN r.result CONTAINS term THEN 2 ELSE 0 END +
            CASE WHEN r.reg_name CONTAINS term THEN 1 ELSE 0 END
         ) +
         CASE
            WHEN $prefer_exam = true AND a.category = 'Exam' THEN 8
            WHEN $prefer_admin = true AND a.category = 'Admin' THEN 8
            WHEN $prefer_general = true AND a.category = 'General' THEN 6
            ELSE 0
         END +
         CASE
            WHEN $is_time = true AND (
                a.content CONTAINS '20 minutes' OR
                a.content CONTAINS '40 minutes' OR
                a.content CONTAINS 'three workdays' OR
                a.content CONTAINS 'workdays' OR
                a.content CONTAINS '學期' OR
                a.content CONTAINS '年' OR
                r.action CONTAINS '20 minutes' OR
                r.action CONTAINS '40 minutes' OR
                r.action CONTAINS 'three workdays' OR
                r.action CONTAINS 'workdays' OR
                r.action CONTAINS '學期' OR
                r.action CONTAINS '年'
            ) THEN 6
            ELSE 0
         END +
         CASE
            WHEN $is_penalty = true AND (
                a.content CONTAINS 'zero grade' OR
                a.content CONTAINS 'five points deducted' OR
                a.content CONTAINS 'withdraw from school' OR
                r.action CONTAINS 'zero grade' OR
                r.action CONTAINS 'five points deducted' OR
                r.action CONTAINS 'withdraw from school'
            ) THEN 6
            ELSE 0
         END AS score
    WHERE score > 0
    RETURN
        r.rule_id AS rule_id,
        r.type AS type,
        r.action AS action,
        r.result AS result,
        r.art_ref AS art_ref,
        r.reg_name AS reg_name,
        a.content AS article_content,
        a.category AS category,
        score AS score
    ORDER BY score DESC
    LIMIT 12
    """

    prefer_exam = any(x in q for x in [
        "exam", "invigilator", "question paper", "electronic", "cheating",
        "copying", "passing notes", "late", "barred"
    ])
    prefer_admin = any(x in q for x in [
        "student id", "easycard", "mifare", "working days"
    ])
    prefer_general = any(x in q for x in [
        "graduation", "credits", "study duration", "leave of absence",
        "passing score", "undergraduate", "graduate", "dismissed", "make-up exam"
    ])

    try:
        with driver.session() as session:
            rows = session.run(
                cypher,
                terms=terms,
                prefer_exam=prefer_exam,
                prefer_admin=prefer_admin,
                prefer_general=prefer_general,
                is_time=flags["is_time"],
                is_penalty=flags["is_penalty"],
            ).data()
    except Exception as e:
        print(f"⚠️ Retrieval warning: {e}")
        return []

    return dedup_results(rows, limit=5)


def generate_answer(question: str, rule_results: list[dict[str, Any]]) -> str:
    if not rule_results:
        return "Insufficient rule evidence to answer this question."

    top_rules = rule_results[:5]

    evidence_lines = []
    for idx, r in enumerate(top_rules, start=1):
        evidence_lines.append(
            f"[{idx}] Regulation: {r.get('reg_name', '')}\n"
            f"    Article: {r.get('art_ref', '')}\n"
            f"    Rule type: {r.get('type', '')}\n"
            f"    Action: {r.get('action', '')}\n"
            f"    Result: {r.get('result', '')}\n"
        )

    evidence_text = "\n".join(evidence_lines)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a university regulation assistant. "
                "Answer in English using ONLY the evidence provided. "
                "If the evidence is insufficient, say exactly: "
                "'Insufficient rule evidence to answer this question.' "
                "If possible, mention the regulation name and article number."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Evidence:\n{evidence_text}\n"
                "Please provide a concise grounded answer in English."
            ),
        },
    ]

    try:
        answer = generate_text(messages, max_new_tokens=180).strip()
        if not answer:
            return "Insufficient rule evidence to answer this question."

        answer = answer.replace("<|assistant|>", "").replace("<|user|>", "").strip()
        return answer
    except Exception as e:
        return f"Error: {str(e)}"


def main() -> None:
    if driver is None:
        return

    load_local_llm()

    print("=" * 50)
    print("🎓 NCU Regulation Assistant (Template)")
    print("=" * 50)
    print("💡 Try: 'What is the penalty for forgetting student ID?'")
    print("👉 Type 'exit' to quit.\n")

    while True:
        try:
            user_q = input("\nUser: ").strip()
            if not user_q:
                continue
            if user_q.lower() in {"exit", "quit"}:
                print("👋 Bye!")
                break

            results = get_relevant_articles(user_q)
            answer = generate_answer(user_q, results)
            print(f"Bot: {answer}")

        except KeyboardInterrupt:
            print("\n👋 Bye!")
            break
        except NotImplementedError as e:
            print(f"⚠️ {e}")
            break
        except Exception as e:
            print(f"❌ Error: {e}")

    driver.close()


if __name__ == "__main__":
    main()