import urllib.request, json, sys
sys.stdout.reconfigure(encoding='utf-8')

token = 'Po123456-'
base = 'http://178.104.154.252:8000'

print("=== CHECKING TELEGRAM NOTIFIER LIVE STATUS ON VPS ===")

def fetch(path, method="GET"):
    try:
        url = f"{base}{path}?token={token}" if '?' not in path else f"{base}{path}&token={token}"
        req = urllib.request.Request(url, method=method)
        res = urllib.request.urlopen(req, timeout=5)
        return json.loads(res.read().decode('utf-8'))
    except Exception as e:
        return {"error": str(e)}

# 1. Notifier Status
print("\n--- 1. NOTIFIER STATUS ---")
st = fetch("/notifier/status")
print("Status:", st)

# 2. Trigger Live Test Alert from VPS
print("\n--- 2. TRIGGERING LIVE TEST ALERT FROM VPS ---")
test_res = fetch("/notifier/test", method="POST")
print("Test Alert Response:", test_res)

# 3. Monitor Status
print("\n--- 3. MONITOR STATUS ---")
m = fetch("/monitor/status")
print("Cycle Age (sec):", m.get("cycle", {}).get("age_sec"))
print("MT5 Connected:", m.get("mt5", {}).get("connected"))
print("Usable Providers:", m.get("llm", {}).get("usable"))
print("LLM Cooldown List:", m.get("llm", {}).get("cooldown"))
