"""診斷工具 — 一次檢查環境與 .env,找出無法連線的原因。

用法(在 my-portfolio 目錄):
  python doctor.py

把輸出貼回對話,即可判斷問題所在。不會連線券商、不會洩漏完整金鑰。
"""
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent


def mask(s: str) -> str:
    if not s:
        return "(空)"
    return (s[:4] + "…" + s[-2:]) if len(s) > 6 else (s[0] + "***")


def main() -> None:
    line = "=" * 56
    print(line)
    print("  資產整合 P0 — 環境診斷")
    print(line)

    # 1) Python 版本
    v = sys.version_info
    ok = v >= (3, 10)
    print(f"Python 版本        : {v.major}.{v.minor}.{v.micro}  "
          f"{'OK' if ok else '✗ 需 >= 3.10'}")
    print(f"執行檔             : {sys.executable}")

    # 2) 套件
    for mod in ("shioaji", "streamlit", "pandas"):
        try:
            m = __import__(mod)
            print(f"套件 {mod:<11}: 已安裝 {getattr(m, '__version__', '')}")
        except Exception as e:
            print(f"套件 {mod:<11}: ✗ 未安裝({type(e).__name__})")

    # 3) .env
    env_path = PROJECT_DIR / ".env"
    print(f".env 路徑          : {env_path}")
    if not env_path.exists():
        print("  ✗ 找不到 .env")
        if (PROJECT_DIR / ".env.txt").exists():
            print("  ⚠ 偵測到 .env.txt(Windows 自動加副檔名)→ 請改名為 .env")
        print(line)
        return

    raw = env_path.read_bytes()
    enc_used, text = None, ""
    for enc in ("utf-8-sig", "utf-8", "cp950", "latin-1"):
        try:
            text = raw.decode(enc)
            enc_used = enc
            break
        except UnicodeDecodeError:
            continue
    print(f".env 編碼          : {enc_used}"
          + ("  (含 BOM,已處理)" if raw[:3] == b"\xef\xbb\xbf" else ""))

    data = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, val = ln.partition("=")
            data[k.strip()] = val.strip().strip('"').strip("'")

    keys = ("SINOPAC_API_KEY", "SINOPAC_SECRET_KEY", "SINOPAC_CA_PATH",
            "SINOPAC_CA_PASSWD", "SINOPAC_PERSON_ID", "SINOPAC_SIMULATION")
    print("讀到的設定:")
    for k in keys:
        val = data.get(k, "")
        secret = ("KEY" in k) or ("PASSWD" in k)
        show = mask(val) if secret else (val or "(空)")
        flag = ""
        if k in ("SINOPAC_API_KEY", "SINOPAC_SECRET_KEY"):
            if not val:
                flag = "  ✗ 空白"
            elif val.startswith("你的"):
                flag = "  ✗ 仍是範本佔位字"
        print(f"  {k:<20}= {show}{flag}")

    # 4) 憑證檔
    ca = data.get("SINOPAC_CA_PATH", "")
    sim = data.get("SINOPAC_SIMULATION", "0") == "1"
    if ca:
        p = Path(ca)
        if not p.is_absolute():
            p = (PROJECT_DIR / ca).resolve()
        print(f"憑證檔路徑         : {p}")
        print(f"  {'OK 存在' if p.exists() else '✗ 此路徑找不到憑證檔'}")
    print(f"執行模式           : {'模擬(免 CA)' if sim else '正式(需 CA)'}")
    print(line)

    bad = (not data.get("SINOPAC_API_KEY") or
           data.get("SINOPAC_API_KEY", "").startswith("你的"))
    print("結論:" + ("API 金鑰未正確填入,請修正上面標 ✗ 的項目。"
                     if bad else "設定看起來正常,可執行 python run.py。"))


if __name__ == "__main__":
    main()
