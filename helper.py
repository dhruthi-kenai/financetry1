import os
import requests
import mysql.connector
import pandas as pd
from dotenv import load_dotenv
from msal import PublicClientApplication
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.schema.document import Document
from sentence_transformers import SentenceTransformer
import streamlit as st
 
# Load environment variables
 
 
# 🔐 SharePoint Configuration
CLIENT_ID = st.secrets["CLIENT_ID"]
TENANT_ID = st.secrets["TENANT_ID"]
SHAREPOINT_HOST = st.secrets["SHAREPOINT_HOST"]
SITE_NAME = st.secrets["SITE_NAME"]
DOC_LIB_PATH = st.secrets["DOC_LIB_PATH"]
EMBEDDINGS_MODEL = "sentence-transformers/all-mpnet-base-v2"
embeddings = HuggingFaceEmbeddings(model_name=EMBEDDINGS_MODEL)
 
# ⚙️ Authenticate with Microsoft Graph
def authenticate_microsoft():
    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": st.secrets["CLIENT_SECRET"],
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default"
    }
 
    response = requests.post(token_url, data=payload)
    response.raise_for_status()
 
    return response.json()["access_token"]
 
 
# 📥 Fetch SharePoint Documents
def fetch_txt_files_from_sharepoint():
    token = authenticate_microsoft()
    headers = {"Authorization": f"Bearer {token}"}
 
    site_resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{SHAREPOINT_HOST}:/sites/{SITE_NAME}", headers=headers)
    site_resp.raise_for_status()
    site_id = site_resp.json()["id"]
 
    drives_resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives", headers=headers)
    drives_resp.raise_for_status()
    drive_id = next((d["id"] for d in drives_resp.json()["value"] if d["name"] == "Documents"), None)
 
    encoded_path = DOC_LIB_PATH.replace(" ", "%20")
    files_resp = requests.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives/{drive_id}/root:/{encoded_path}:/children",
        headers=headers)
    files_resp.raise_for_status()
 
    docs = []
    for item in files_resp.json().get("value", []):
        if item["name"].endswith(".txt"):
            text_resp = requests.get(item["@microsoft.graph.downloadUrl"])
            text_resp.raise_for_status()
            docs.append(Document(page_content=text_resp.text, metadata={"source": item["name"]}))
    return docs
 
# 🔍 Get context using SharePoint + FAISS
def get_context_from_docs(user_query):
    if not os.path.exists("./vector_index"):
        print("Index not found. Fetching documents from SharePoint...")
        documents = fetch_txt_files_from_sharepoint()
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
        chunks = text_splitter.split_documents(documents)
        vectorstore = FAISS.from_documents(chunks, embeddings)
        vectorstore.save_local("./vector_index")
    else:
        vectorstore = FAISS.load_local("./vector_index", embeddings, allow_dangerous_deserialization=True)
 
    results = vectorstore.similarity_search(user_query, k=1)
    return results[0].page_content if results else "No relevant document found."
 
# 💬 Mistral LLM
def call_llm(prompt):
    mistral_api_key = st.secrets["MISTRAL_API_KEY"]
    headers = {
        "Authorization": f"Bearer {mistral_api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "mistral-medium",
        "messages": [
            {"role": "system", "content": "You are a helpful finance assistant."},
            {"role": "user", "content": prompt}
        ]
    }
    response = requests.post("https://api.mistral.ai/v1/chat/completions", headers=headers, json=payload)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]
 
# 🧠 SQL Runner
def run_sql_query(query):
    try:
        conn = mysql.connector.connect(
            host=st.secrets["DB_HOST"],
            database=st.secrets["DB_NAME"],
            user=st.secrets["DB_USER"],
            password=st.secrets["DB_PASSWORD"],
            port=int(st.secrets.get("DB_PORT", 3306))
        )
        cursor = conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        cursor.close()
        conn.close()
        return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame()
    except Exception as e:
        return f"SQL Error: {str(e)}"
 
# 🧭 Route Query
def route_query(user_query):
    router_prompt = f"""You are a finance data assistant. Convert the following SQL result into a clear, human-readable summary.
Avoid using markdown headers like ### or **bold**.
 
Use only the following MySQL tables and their columns. Always refer to them exactly as named. Never guess table or column names.
 
---
Table: ap_invoices (Accounts Payable)
Columns:
- invoice_id (INT)
- vendor_id (INT)
- invoice_date (DATE)
- amount (DECIMAL)
- payment_status (VARCHAR: 'Paid' or 'Unpaid')
- due_date (DATE)
 
---
Table: ar_invoices (Accounts Receivable)
Columns:
- invoice_id (INT)
- customer_id (INT)
- invoice_date (DATE)
- amount (DECIMAL)
- payment_received (BOOLEAN)
- due_date (DATE)
 
---
Table: vendors
Columns:
- vendor_id (INT)
- vendor_name (VARCHAR)
- contact_email (VARCHAR)
- city (VARCHAR)
 
---
Table: customers
Columns:
- customer_id (INT)
- customer_name (VARCHAR)
- contact_email (VARCHAR)
- city (VARCHAR)
 
---
Table: payments
Columns:
- payment_id (INT)
- invoice_id (INT)
- payment_date (DATE)
- amount (DECIMAL)
- payment_method (VARCHAR)
- direction (ENUM: 'AP' or 'AR')
 
---
Table: general_ledger
Columns:
- entry_id (INT)
- entry_date (DATE)
- account_code (VARCHAR)
- debit (DECIMAL)
- credit (DECIMAL)
- description (TEXT)
 
---
Instructions:
- If the query is about structured finance data (e.g. invoices, payments, balances), return only a SQL query without explanation.
- If the query is about policy, process, accounting rules, or how-to (like 'how to reverse a journal entry'), respond only with: DOCUMENT
- Always use the correct table and column names.
- Never invent a table or column.
-If the user asks for all invoices or a summary of invoices, return a SQL query that combines both `ap_invoices` and `ar_invoices` using a `UNION ALL`. Make sure the column names match exactly, and add a column called `invoice_type` with values 'AP' or 'AR'.
 
 For consistency:
- In the final *output* (not the SQL), treat `vendor_id` and `customer_id` as a common `entity_id`.
- In the final *output*, treat `payment_status` and `payment_received` both as `payment_status` with values 'Paid'/'Unpaid'.
- But do not rename these fields in the actual SQL query — use the original column names.
 
 
 
Query: {user_query}
Answer:
"""
 
    decision = call_llm(router_prompt).strip()
 
    if "SELECT" in decision.upper():
        cleaned_query = decision.replace("```sql", "").replace("```", "").strip()
        result_df = run_sql_query(cleaned_query)
        if isinstance(result_df, str):
            return result_df
        elif result_df.empty:
            return "No data found."
 
        table_text = result_df.to_markdown(index=False)
        summary_prompt = f"""You are a finance assistant. Convert the following SQL result into a clear, human-readable summary.
 
SQL Output:
{table_text}
 
Answer:"""
        return call_llm(summary_prompt)
 
    elif decision.upper().startswith("DOCUMENT"):
        context = get_context_from_docs(user_query)
        doc_prompt = f"Answer this user query based on the context below.\n\nContext:\n{context}\n\nQuestion: {user_query}"
        return call_llm(doc_prompt)
 
    else:
        return f"{decision}"