"""portfolio.db 自動備份 — P4 強化項目。

在 run.py 每次同步成功後呼叫 auto_backup(),或手動執行:
  python tools/backup.py

規則:
  * 備份到 backups/portfolio-YYYYMMDD-HHMMSS.db
  * 用 SQLite 線上備份 API(sqlite3 backup),寫入中也安全
  * 同一天只留最新一份;總數超過 KEEP 份時刪最舊(預設 14 份)
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB = ROOT / "portfolio.db"
BACKUP_DIR = ROOT / "backups"
KEEP = 14


def auto_backup(db_path: Path = DB, backup_dir: Path = BACKUP_DIR,
                keep: int = KEEP) -> Path | None:
    if not db_path.exists():
        print("[backup] 找不到 portfolio.db,略過備份")
        return None
    backup_dir.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    dest = backup_dir / f"portfolio-{datetime.now():%Y%m%d-%H%M%S}.db"

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)          # 線上備份:寫入中也一致
    src.close()
    dst.close()

    # 同一天只留最新一份
    same_day = sorted(backup_dir.glob(f"portfolio-{today}-*.db"))
    for old in same_day[:-1]:
        old.unlink(missing_ok=True)

    # 總數上限
    allb = sorted(backup_dir.glob("portfolio-*.db"))
    for old in allb[:-keep]:
        old.unlink(missing_ok=True)

    print(f"[backup] 已備份 → {dest.name}(保留最近 {keep} 份)")
    return dest


if __name__ == "__main__":
    auto_backup()
    sys.exit(0)
