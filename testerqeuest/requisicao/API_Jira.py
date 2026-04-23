import csv
import json
import os
import re
import time
import requests
import pandas as pd
from requests.auth import HTTPBasicAuth
import keyring

# =========================================================
# CONFIG
# =========================================================
SERVICE = "jira_cnpem"

EMAIL = keyring.get_password(SERVICE, "email")
BASE = keyring.get_password(SERVICE, "base")
API_TOKEN = keyring.get_password(SERVICE, EMAIL or "")

if not EMAIL or not BASE or not API_TOKEN:
    raise RuntimeError(
        "Credenciais do Jira não encontradas no keyring.\n"
        "Esperado no keyring:\n"
        f"- service={SERVICE}, username=email  -> value=<seu email>\n"
        f"- service={SERVICE}, username=base   -> value=https://cnpem.atlassian.net\n"
        f"- service={SERVICE}, username=<email> -> value=<api_token>\n"
    )

JQL = "project = IINFRA ORDER BY Rank ASC"
PAGE_SIZE = 200

OUT_FIELDS = "iinfra_field_dictionary.csv"
OUT_ISSUES = "iinfra_organizado.csv"
OUT_ATTACH = "iinfra_attachments.csv"
OUT_PARQUET = "iinfra_organizado.parquet"

DOWNLOAD_ATTACHMENTS = False
ATTACHMENTS_DIR = "attachments_download"

# Campos (nomes como aparecem na UI) que você quer garantir no export
WANTED_UI_FIELDS = [
    "Team",
    "Unidade Requisitante",
    "Código de Projeto",
    "Orçamento (R$)",
    "Expense (R$)",
    "Balance (R$)",
    "Orçamento revisado (R$)",
    "Orçamento liberado no ano (R$)",
    "% Execução",
    "Criteria - Indicators",
    "Indicador TA",
    "Work type id",
    "Observações",
    "Start date",
    "Epic Link",
    "Flagged",
    "Story Points",
    "servicedesk-approvals",
]

# Ordem final das colunas do CSV de issues
FINAL_COL_ORDER = [
    "key", "url",
    "project", "issuetype", "status", "priority",
    "Team", "Unidade Requisitante", "Código de Projeto",
    "Start date", "duedate",
    "Orçamento (R$)", "Orçamento_num",
    "Expense (R$)", "Expense_num",
    "Balance (R$)", "Balance_num",
    "Orçamento revisado (R$)", "Orçamento_revisado_num",
    "Orçamento liberado no ano (R$)", "Orçamento_liberado_ano_num",
    "% Execução", "Execução_percent_num",
    "Indicador TA", "Criteria - Indicators",
    "Work type id",
    "assignee", "reporter",
    "created", "updated",
    "summary", "description_text",
    "Observações",
    "attachment_count",
]

# Schema fixo para anexos
ATTACH_COLS = [
    "issueKey",
    "attachmentId",
    "filename",
    "mimeType",
    "size",
    "created",
    "author",
    "contentUrl",
    "thumbnailUrl",
]

auth = HTTPBasicAuth(EMAIL, API_TOKEN)
headers = {"Accept": "application/json"}

# =========================================================
# HELPERS: HTTP
# =========================================================
def req_json(method, url, *, params=None, max_retries=6):
    for attempt in range(max_retries):
        r = requests.request(method, url, headers=headers, auth=auth, params=params)

        if r.status_code in (429, 500, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", "0") or "0")
            if wait <= 0:
                wait = min(2 ** attempt, 30)
            time.sleep(wait)
            continue

        if not r.ok:
            print("\n=== HTTP ERROR ===")
            print("METHOD:", method)
            print("URL:", r.url)
            print("STATUS:", r.status_code)
            print("BODY (2000 chars):", r.text[:2000])
            print("=== END ERROR ===\n")
            r.raise_for_status()

        return r.json()

    r.raise_for_status()


def jira_get(path, params=None):
    return req_json("GET", f"{BASE}{path}", params=params)

# =========================================================
# HELPERS: NORMALIZAÇÃO
# =========================================================
def _sanitize_cell(v):
    """Remove CR/LF e normaliza nulos -> string vazia.
    Isso evita linha quebrada e deslocamento de colunas no CSV.
    """
    if v is None:
        return ""
    # pandas NA / nan
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass

    s = str(v)
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def to_scalar(v):
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        for k in ("displayName", "name", "value", "key"):
            if k in v and v[k] is not None:
                return v[k]
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        items = [to_scalar(x) for x in v]
        items = [str(x) for x in items if x is not None and str(x).strip() != ""]
        return ", ".join(items) if items else None
    return str(v)


def parse_ptbr_number(s):
    """Converte '1.234.567,89' / '150.000,00' / '57%' -> float"""
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    s = s.replace("R$", "").replace("%", "").strip()
    s = re.sub(r"[^0-9\-,\.]", "", s)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def adf_to_text(node):
    """Extrai texto simples de ADF (description)."""
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(adf_to_text(x) for x in node)
    if isinstance(node, dict):
        t = node.get("type")
        if t == "text":
            return node.get("text", "")
        if t == "hardBreak":
            return "\n"
        return adf_to_text(node.get("content", []))
    return str(node)


def description_to_text(desc):
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    if isinstance(desc, dict):
        return adf_to_text(desc)
    return str(desc)

# =========================================================
# CSV WRITER (ROBUSTO)
# =========================================================
def write_csv_dictrows(path, rows, fieldnames):
    """Escreve CSV determinístico:
    - sempre mesmas colunas e mesma ordem
    - QUOTE_ALL
    - lineterminator '\n'
    - newline='' (evita linhas em branco no Windows)
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            delimiter=";",
            quotechar='"',
            quoting=csv.QUOTE_ALL,
            lineterminator="\n",
            escapechar="\\",
            doublequote=True,
            extrasaction="ignore",
        )
        w.writeheader()
        for r in rows:
            out = {k: _sanitize_cell(r.get(k)) for k in fieldnames}
            w.writerow(out)

# =========================================================
# JIRA: FIELDS + SEARCH
# =========================================================
def fetch_fields_catalog():
    """GET /rest/api/3/field/search paginado"""
    all_fields = []
    start_at = 0
    max_results = 100
    while True:
        data = jira_get("/rest/api/3/field/search", params={"startAt": start_at, "maxResults": max_results})
        values = data.get("values", [])
        all_fields.extend(values)
        start_at += len(values)
        if start_at >= data.get("total", 0) or len(values) == 0:
            break
    return all_fields


def search_all_issues(jql, fields_csv, expand="names,schema", page_size=100):
    """GET /rest/api/3/search/jql com nextPageToken"""
    issues = []
    next_token = None

    while True:
        params = {
            "jql": jql,
            "maxResults": page_size,
            "fields": fields_csv,
            "expand": expand,
        }
        if next_token:
            params["nextPageToken"] = next_token

        data = jira_get("/rest/api/3/search/jql", params=params)
        batch = data.get("issues", [])
        issues.extend(batch)

        is_last = data.get("isLast", True)
        next_token = data.get("nextPageToken")

        print(f"Baixando... acumulado={len(issues)} isLast={is_last}")
        if is_last or not next_token or len(batch) == 0:
            break

    return issues

# =========================================================
# (Opcional) Download de anexos físicos
# =========================================================
def download_attachments(attach_rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    for row in attach_rows:
        issue_key = row.get("issueKey")
        url = row.get("contentUrl")
        filename = row.get("filename")
        if not issue_key or not url or not filename:
            continue

        issue_dir = os.path.join(out_dir, issue_key)
        os.makedirs(issue_dir, exist_ok=True)
        path = os.path.join(issue_dir, filename)

        r = requests.get(url, auth=auth, stream=True, headers={"Accept": "*/*"})
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    print("Download concluído em:", out_dir)

# =========================================================
# MAIN
# =========================================================
def main():
    print("BASE:", BASE)
    print("EMAIL:", EMAIL)
    print("JQL:", JQL)

    # 1) Catálogo de campos (dicionário)
    fields_catalog = fetch_fields_catalog()
    dict_rows = [{
        "fieldId": f.get("id"),
        "fieldName": f.get("name"),
        "custom": f.get("custom"),
        "schema": json.dumps(f.get("schema"), ensure_ascii=False),
    } for f in fields_catalog]

    write_csv_dictrows(
        OUT_FIELDS,
        dict_rows,
        fieldnames=["fieldId", "fieldName", "custom", "schema"],
    )
    print("OK:", OUT_FIELDS)

    name_to_id = {f.get("name"): f.get("id") for f in fields_catalog if f.get("name") and f.get("id")}

    # 2) Fields a buscar (system + custom)
    system_fields = [
        "summary", "description", "status", "issuetype", "priority",
        "assignee", "reporter", "created", "updated", "duedate", "project",
        "attachment",
    ]
    custom_field_ids = [name_to_id.get(nm) for nm in WANTED_UI_FIELDS if name_to_id.get(nm)]
    fields_list = system_fields + custom_field_ids

    # remove duplicados preservando ordem
    seen = set()
    fields_list = [f for f in fields_list if not (f in seen or seen.add(f))]
    fields_csv = ",".join(fields_list)

    # 3) Busca issues
    issues = search_all_issues(JQL, fields_csv, expand="names,schema", page_size=PAGE_SIZE)

    # 4) Monta linhas de issues + anexos
    issue_rows = []
    attach_rows = []

    for issue in issues:
        f = issue.get("fields", {})
        key = issue.get("key")

        # issue base
        row = {
            "key": key,
            "url": f"{BASE}/browse/{key}" if key else None,
            "project": to_scalar(f.get("project")),
            "issuetype": to_scalar(f.get("issuetype")),
            "status": to_scalar(f.get("status")),
            "priority": to_scalar(f.get("priority")),
            "assignee": to_scalar(f.get("assignee")),
            "reporter": to_scalar(f.get("reporter")),
            "created": f.get("created"),
            "updated": f.get("updated"),
            "duedate": f.get("duedate"),
            "summary": f.get("summary"),
            "description_text": description_to_text(f.get("description")),
        }

        # custom fields por nome da UI
        for ui_name in WANTED_UI_FIELDS:
            fid = name_to_id.get(ui_name)
            row[ui_name] = to_scalar(f.get(fid)) if fid else None

        # numéricos prontos p/ BI
        row["Orçamento_num"] = parse_ptbr_number(row.get("Orçamento (R$)"))
        row["Expense_num"] = parse_ptbr_number(row.get("Expense (R$)"))
        row["Balance_num"] = parse_ptbr_number(row.get("Balance (R$)"))
        row["Orçamento_revisado_num"] = parse_ptbr_number(row.get("Orçamento revisado (R$)"))
        row["Orçamento_liberado_ano_num"] = parse_ptbr_number(row.get("Orçamento liberado no ano (R$)"))
        row["Execução_percent_num"] = parse_ptbr_number(row.get("% Execução"))

        # anexos
        attachments = f.get("attachment") or []
        row["attachment_count"] = len(attachments)

        for a in attachments:
            attach_rows.append({
                "issueKey": key,
                "attachmentId": a.get("id"),
                "filename": a.get("filename"),
                "mimeType": a.get("mimeType"),
                # size: manter como valor simples (vai como texto no CSV; você tipa no Power BI se quiser)
                "size": a.get("size"),
                "created": a.get("created"),
                "author": (a.get("author") or {}).get("displayName"),
                "contentUrl": a.get("content"),
                "thumbnailUrl": a.get("thumbnail"),
            })

        issue_rows.append(row)

    # 5) Ordena colunas das issues (as que faltarem vão pro final)
    df_issues = pd.DataFrame(issue_rows)
    issue_cols = [c for c in FINAL_COL_ORDER if c in df_issues.columns] + [c for c in df_issues.columns if c not in FINAL_COL_ORDER]
    df_issues = df_issues[issue_cols]

    # Converte de volta para rows (dicts) pra escrever com DictWriter robusto
    issue_rows_out = df_issues.to_dict(orient="records")

    # 6) Export issues (robusto)
    write_csv_dictrows(OUT_ISSUES, issue_rows_out, fieldnames=issue_cols)
    print("OK:", OUT_ISSUES)

    # 7) Export anexos (robusto + schema fixo)
    # garante que mesmo sem anexos o CSV sai com header correto
    attach_rows_out = []
    for r in attach_rows:
        rr = {c: r.get(c) for c in ATTACH_COLS}
        attach_rows_out.append(rr)

    write_csv_dictrows(OUT_ATTACH, attach_rows_out, fieldnames=ATTACH_COLS)
    print("OK:", OUT_ATTACH)

    # 8) Parquet (issues) opcional
    try:
        df_issues.to_parquet(OUT_PARQUET, index=False)
        print("OK:", OUT_PARQUET)
    except Exception as e:
        print("Parquet falhou (instale pyarrow). Erro:", e)

    # 9) Download físico (opcional)
    if DOWNLOAD_ATTACHMENTS and len(attach_rows_out) > 0:
        download_attachments(attach_rows_out, ATTACHMENTS_DIR)

    print("Board:", "https://cnpem.atlassian.net/jira/software/c/projects/IINFRA/boards/587")


if __name__ == "__main__":
    main()