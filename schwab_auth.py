"""Schwab 一次性 OAuth 授權 — 產生 schwab_token.json。

執行:python schwab_auth.py

流程(全程約 1 分鐘):
  1. 本工具印出授權網址 → 你用瀏覽器開啟、登入嘉信、同意授權。
  2. 嘉信會把瀏覽器導向你的 Callback URL(https://127.0.0.1:8182/...)。
     該頁面會顯示「無法連上」是正常的 — 我們只需要網址列裡的 code。
  3. 把瀏覽器網址列的【完整網址】複製貼回本工具,即完成。

之後 7 天內,run.py 會用 refresh token 自動續期,不需再做這步。
refresh token 過期(約 7 天)時,重跑本工具一次即可。
"""
import base64
import json
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"


def load_env() -> dict:
    """沿用專案 run.py 的 .env 讀取(若可用),否則用最簡讀取。"""
    try:
        from run import load_env as _load   # 專案既有的強健讀取
        return _load()
    except Exception:
        env = {}
        p = ROOT / ".env"
        if p.exists():
            for raw in p.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        return env


def main() -> None:
    import requests

    env = load_env()
    app_key = env.get("SCHWAB_APP_KEY", "")
    app_secret = env.get("SCHWAB_APP_SECRET", "")
    callback = env.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    token_path = Path(env.get("SCHWAB_TOKEN_PATH", "schwab_token.json"))

    if not app_key or not app_secret:
        print("請先在 .env 填入 SCHWAB_APP_KEY / SCHWAB_APP_SECRET")
        sys.exit(1)

    url = (f"{AUTH_URL}?client_id={urllib.parse.quote(app_key)}"
           f"&redirect_uri={urllib.parse.quote(callback)}")
    print("\n=== Schwab OAuth 一次性授權 ===\n")
    print("步驟 1:用瀏覽器開啟以下網址,登入嘉信並同意授權:\n")
    print("  " + url + "\n")
    print("步驟 2:授權後瀏覽器會跳轉(頁面顯示無法連上是正常的),")
    print("        把網址列的【完整網址】複製貼到下面。\n")

    redirected = input("貼上跳轉後的完整網址:").strip()
    qs = urllib.parse.urlparse(redirected).query
    code = urllib.parse.parse_qs(qs).get("code", [""])[0]
    if not code:
        print("\n❌ 網址中找不到 code 參數,請確認貼的是跳轉後的完整網址。")
        sys.exit(1)

    basic = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code",
              "code": code, "redirect_uri": callback},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"\n❌ 換發 token 失敗(HTTP {resp.status_code}):{resp.text[:300]}")
        print("常見原因:code 已用過/逾時(重跑本工具)、Callback URL 與 App 設定不一致。")
        sys.exit(1)

    data = resp.json()
    now = time.time()
    token = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "access_expires_at": now + data.get("expires_in", 1800),
        "refresh_issued_at": now,           # 7 天壽命起算點
    }
    token_path.write_text(json.dumps(token, indent=2), encoding="utf-8")
    print(f"\n✅ 完成!token 已存到 {token_path}")
    print("   之後直接 python run.py 即可;約 7 天後需重跑本工具一次。")
    print("   (請確認 schwab_token.json 已列入 .gitignore)")


if __name__ == "__main__":
    main()
