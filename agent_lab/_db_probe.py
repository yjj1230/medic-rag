import sqlite3

con = sqlite3.connect(r"C:\Users\lyx\Desktop\RAG\medical_rag.db")
cur = con.cursor()
tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()]
print("tables:", tables)
for t in ("patients", "consents", "documents"):
    if t in tables:
        cols = [c[1] for c in cur.execute(f"pragma table_info({t})").fetchall()]
        print(t, "cols:", cols)
        for row in cur.execute(f"select * from {t} limit 5").fetchall():
            print(" ", str(row)[:300])
con.close()
