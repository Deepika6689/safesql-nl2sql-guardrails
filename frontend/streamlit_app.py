"""SafeSQL Streamlit frontend. Talks only to the FastAPI backend (never to the database)."""

import os

import httpx
import pandas as pd
import streamlit as st

API = os.environ.get("SAFESQL_API_URL", "http://localhost:8000")

st.set_page_config(page_title="SafeSQL", page_icon="🛡️", layout="wide")
st.title("🛡️ SafeSQL")
st.caption("Ask questions in plain English. SQL is generated, then validated before anything runs.")
st.info(
    "Generated SQL is untrusted. Before execution it is parsed, checked against a table/column "
    "allowlist, restricted to a single SELECT, row-limited, and run by a read-only database account."
)


@st.cache_data(ttl=60, show_spinner=False)
def load_examples() -> list[str]:
    try:
        return httpx.get(f"{API}/examples", timeout=5).json()["examples"]
    except Exception:
        return []


def ask(question: str) -> None:
    try:
        r = httpx.post(f"{API}/query", json={"question": question}, timeout=120)
        st.session_state["result"] = (r.status_code, r.json())
    except httpx.HTTPError:
        st.session_state["result"] = (0, None)


if "question" not in st.session_state:
    st.session_state["question"] = ""

with st.sidebar:
    st.subheader("Example questions")
    for ex in load_examples():
        if st.button(ex, use_container_width=True):
            st.session_state["question"] = ex
            ask(ex)
    st.caption(f"Backend: {API}")

question = st.text_input("Your question", key="question", max_chars=500)
if st.button("Run", type="primary", disabled=len(question.strip()) < 3):
    with st.spinner("Generating, validating and running..."):
        ask(question.strip())

if "result" in st.session_state:
    status_code, body = st.session_state["result"]
    if body is None:
        st.error("Cannot reach the SafeSQL backend. Is it running (uvicorn app.main:app)?")
    elif "status" not in body:
        st.error(f"Request failed (HTTP {status_code}).")
    else:
        mode = body.get("generation_mode")
        if mode:
            label = (
                f"Local LLM ({body.get('model')})"
                if mode == "llm"
                else "Demo fallback (deterministic templates, not an LLM)"
            )
            st.caption(f"Generation mode: **{label}**")
        for note in body.get("notes", []):
            st.warning(note)
        if body["status"] != "ok":
            icon = st.warning if body["status"] == "rejected" else st.error
            icon(
                f"{'Blocked by validator' if body['status'] == 'rejected' else 'Error'}: {body['error']['message']}"
            )
        if body.get("generated_sql"):
            st.subheader("Generated SQL")
            st.code(body["generated_sql"], language="sql")
        if body["status"] == "ok":
            if body["executed_sql"] != body["generated_sql"]:
                with st.expander("SQL actually executed (validated, normalised, row-limited)"):
                    st.code(body["executed_sql"], language="sql")
            st.subheader("Explanation")
            st.write(body["explanation"])
            st.subheader("Results")
            st.dataframe(pd.DataFrame(body["rows"], columns=body["columns"]), use_container_width=True)
            st.caption(
                f"{body['row_count']} row(s) · {body['execution_time_ms']} ms"
                + (" · truncated" if body["truncated"] else "")
            )
