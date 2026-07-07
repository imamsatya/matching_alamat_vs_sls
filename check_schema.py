import pyodbc
import os
from dotenv import load_dotenv

load_dotenv()
conn_string = (
    'DRIVER={ODBC Driver 18 for SQL Server};'
    f'SERVER={os.getenv("DB_SERVER")};DATABASE={os.getenv("DB_NAME")};'
    f'UID={os.getenv("DB_USER")};PWD={os.getenv("DB_PASSWORD")};'
    'TrustServerCertificate=yes;Connection Timeout=30;'
)
conn = pyodbc.connect(conn_string)
cursor = conn.cursor()

t1 = "matcha_datatemp_direktori_bpom_sheet1_saranaizinedar_data11Mei2026_20260518"
t2 = "matcha_mfd_2025_2_hirarki_sls"

# How many SLS entries do NOT have RT in their name?
cursor.execute(f"SELECT COUNT(*) FROM {t2} WHERE nmsls_25_2 NOT LIKE '%RT%'")
print(f"SLS without RT: {cursor.fetchone()[0]}")

cursor.execute(f"SELECT COUNT(*) FROM {t2} WHERE nmsls_25_2 LIKE '%RT%'")
print(f"SLS with RT: {cursor.fetchone()[0]}")

# Sample SLS without RT
cursor.execute(f"SELECT TOP 20 kelurahan_desa_id, nmsls_25_2 FROM {t2} WHERE nmsls_25_2 NOT LIKE '%RT%'")
print("\nSample SLS without RT:")
for r in cursor.fetchall():
    print(f"  kel={r[0]}, nmsls={r[1]}")

# How many input rows have kelurahan where ALL SLS are dusun-only (no RT)?
cursor.execute(f"""
    SELECT COUNT(*) FROM {t1}
    WHERE is_duplicate = 0
      AND kelurahan_desa_id_2025_2 IS NOT NULL
      AND kelurahan_desa_id_2025_2 NOT IN (
          SELECT DISTINCT kelurahan_desa_id FROM {t2} WHERE nmsls_25_2 LIKE '%RT%'
      )
      AND kelurahan_desa_id_2025_2 IN (
          SELECT DISTINCT kelurahan_desa_id FROM {t2}
      )
""")
print(f"\nInput rows where kelurahan only has dusun SLS (no RT): {cursor.fetchone()[0]}")

# Sample of those
cursor.execute(f"""
    SELECT TOP 10 t1.id_table, t1.alamat_etl, t1.kelurahan_desa_id_2025_2
    FROM {t1} t1
    WHERE t1.is_duplicate = 0
      AND t1.kelurahan_desa_id_2025_2 IS NOT NULL
      AND t1.kelurahan_desa_id_2025_2 NOT IN (
          SELECT DISTINCT kelurahan_desa_id FROM {t2} WHERE nmsls_25_2 LIKE '%RT%'
      )
      AND t1.kelurahan_desa_id_2025_2 IN (
          SELECT DISTINCT kelurahan_desa_id FROM {t2}
      )
""")
print("\nSample input rows in dusun-only kelurahan:")
for r in cursor.fetchall():
    print(f"  id={r[0]}, kel={r[2]}, alamat={r[1][:80]}...")

conn.close()
