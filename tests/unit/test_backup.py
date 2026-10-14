import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location("backup_db", Path(__file__).resolve().parents[2] / "scripts" / "backup_db.py")
backup_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_db)


def test_backup_is_consistent_and_retention_prunes(tmp_path):
    src = tmp_path / "live.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()
    dest = tmp_path / "b"
    dest.mkdir()
    for n in ("20200101T000000Z", "20200102T000000Z"):
        (dest / f"flightdesk-{n}.db").write_bytes(b"old")
    out = backup_db.backup(src, dest, keep=2)
    assert sqlite3.connect(out).execute("SELECT x FROM t").fetchone() == (42,)
    assert sorted(p.name for p in dest.glob("*.db"))[0] == "flightdesk-20200102T000000Z.db"
    assert len(list(dest.glob("*.db"))) == 2
