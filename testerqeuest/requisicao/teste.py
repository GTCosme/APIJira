import keyring

EMAIL = "gabriel.torres@cnpem.br"
BASE = "https://cnpem.atlassian.net"
API_TOKEN = "ATATT3xFfGF00bsJsmYL9rtzwi8ta0v0Bz0LOxauulCelUINGQ52eKb_O2siFiZoexl75tmuEz8F6PEOJTGLYDK1XtSvjC_Aix1jacXXKpy1Bw89EirDgGHL843F9JLX3e-cdPpY3rpHLOH3I5bQBKvCt8foQBdAErPqo25Nne3hhgWaADHfRbk=5ECB886B"

# Armazenando no keyring
keyring.set_password("jira_cnpem", "email", EMAIL)
keyring.set_password("jira_cnpem", "base", BASE)
keyring.set_password("jira_cnpem", EMAIL, API_TOKEN)

print("✅ Credenciais do Jira armazenadas com sucesso no keyring")