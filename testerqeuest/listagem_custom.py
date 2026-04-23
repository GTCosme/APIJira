import requests
from requests.auth import HTTPBasicAuth

JIRA_DOMAIN = "https://cnpem.atlassian.net"
EMAIL = "gabriel.torres@cnpem.br"
API_TOKEN = "ATATT3xFfGF00bsJsmYL9rtzwi8ta0v0Bz0LOxauulCelUINGQ52eKb_O2siFiZoexl75tmuEz8F6PEOJTGLYDK1XtSvjC_Aix1jacXXKpy1Bw89EirDgGHL843F9JLX3e-cdPpY3rpHLOH3I5bQBKvCt8foQBdAErPqo25Nne3hhgWaADHfRbk=5ECB886B"

URL = f"{JIRA_DOMAIN}/rest/api/3/field"
HEADERS = {"Accept": "application/json"}

print("🔍 Iniciando validação da requisição...")

# ======================
# TRY → somente a chamada à API
# ======================
try:
    response = requests.get(
        URL,
        headers=HEADERS,
        auth=HTTPBasicAuth(EMAIL, API_TOKEN),
        timeout=30
    )

    print("✅ Requisição enviada")
    print("📡 Status code:", response.status_code)

    response.raise_for_status()
    all_fields = response.json()

except requests.exceptions.RequestException as e:
    print("❌ Erro na requisição")
    print(e)
    exit(1)

#

print(f"\n📊 Total de campos retornados: {len(all_fields)}\n")

print("📋 Campos customizados encontrados:\n")

for f in all_fields:
    if f["custom"]:
        print(f"{f['id']} - {f['name']}")

print("\n✅ Script finalizado com sucesso")
