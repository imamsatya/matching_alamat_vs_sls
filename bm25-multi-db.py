"""
BM25 Matching Algorithm (MULTIPROCESSING)
=========================================
Algoritma ini menggunakan BM25 (Best Matching 25) untuk kandidat selection.
Cocok untuk menangani perbedaan panjang alamat (Master detail vs ETL singkat).
Menggunakan Character N-Grams (3,3) untuk ketahanan terhadap typo.
"""

import pandas as pd
import pyodbc
import re
import time
import json
import os
import sys
import logging
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed
from rapidfuzz import fuzz as r_fuzz
from rapidfuzz.distance import JaroWinkler
from sklearn.feature_extraction.text import TfidfVectorizer
from scipy.sparse import csr_matrix
from dotenv import load_dotenv

load_dotenv()

# %% Configuration
CONFIG = {
    "CONNECTION_STRING": (
        'DRIVER={ODBC Driver 18 for SQL Server};'
        f'SERVER={os.getenv("DB_SERVER")};DATABASE={os.getenv("DB_NAME")};'
        f'UID={os.getenv("DB_USER")};PWD={os.getenv("DB_PASSWORD")};'
        'TrustServerCertificate=yes;'
    ),
    "TABLE_NAME": "matcha_datatemp_ngibar_part_2_20260625",

    "COL_IDSBR": "idsbr_algoritma",
    "COL_PERUSAHAAN_ID": "perusahaan_id_algoritma",
    "COL_POSSIBLE_MATCH": "possible_match_algoritma",

    "EXTRA_WHERE_CLAUSE": "AND flag_ignore = 0 and is_duplicate = 0 and is_ignored = 0",
    "TEST_KAB_ID": None,

    "BM25_K1": 1.5,
    "BM25_B": 0.75,
    "NGRAM_RANGE": (3, 3),
    "BM25_THRESHOLD": 10.0,

    "BULK_SIZE": 500,
    "LOG_FILE": "matching_bm25_multi.log",
    "CHECKPOINT_FILE": "matching_bm25_checkpoint.json",
    
    "MAX_WORKERS": min(os.cpu_count() or 4, 12),
}

# %% Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(processName)s] %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# %% BM25 Core Class
class BM25Transformer:
    def __init__(self, ngram_range=(3, 3), k1=1.5, b=0.75):
        self.vectorizer = TfidfVectorizer(analyzer='char_wb', ngram_range=ngram_range, use_idf=False)
        self.k1 = k1
        self.b = b

    def fit(self, corpus):
        tf_matrix = self.vectorizer.fit_transform(corpus).tocsr()
        n_docs = tf_matrix.shape[0]
        
        doc_len = np.array(tf_matrix.sum(axis=1)).flatten()
        avgdl = doc_len.mean() if doc_len.size > 0 else 1.0
        
        df = np.diff(tf_matrix.tocsc().indptr)
        idf = np.log((n_docs - df + 0.5) / (df + 0.5) + 1)
        
        data = tf_matrix.data
        indices = tf_matrix.indices
        indptr = tf_matrix.indptr
        
        rows = np.repeat(np.arange(n_docs), np.diff(indptr))
        cols = indices
        
        dl_norm = self.k1 * (1 - self.b + self.b * (doc_len[rows] / avgdl))
        new_data = (idf[cols] * (data * (self.k1 + 1)) / (data + dl_norm)).astype(np.float32)
        
        self.scored_master_matrix = csr_matrix((new_data, (rows, cols)), shape=tf_matrix.shape)
        return self

    def transform(self, query_corpus):
        return (self.vectorizer.transform(query_corpus) > 0).astype(np.float32)

# %% Helper Functions
# Prioritas status_perusahaan_id: kode 1 paling tinggi, lalu 2,3,5,8, sisanya paling rendah
PREFERRED_STATUS = {1: 0, 2: 1, 3: 2, 5: 3, 8: 4}
def status_priority(status_id):
    return PREFERRED_STATUS.get(status_id, 99)

BADAN_HUKUM_PATTERN = re.compile(
    r'\b(pt|cv|ud|tb|fa|pd|po|firma|co|ltd|tbk|inc|corp)\b|[.,]\s*(pt|cv|ud)\s*$',
    re.IGNORECASE
)

def has_badan_hukum(name_raw: str) -> bool:
    return bool(BADAN_HUKUM_PATTERN.search(name_raw))
def normalize(name):
    if not name or str(name).strip() in ('', 'None', 'nan'): return ''
    name = str(name).lower()
    name = re.sub(r'[^a-z0-9 ]', '', name)
    name = re.sub(r'\b(pt|cv|ud|firma|co|ltd|tbk|persero|perseroda)\b', '', name)
    return re.sub(r'\s+', ' ', name).strip()

def collapse_spaced_name(name: str) -> str:
    tokens = name.split()
    if not tokens:
        return name
    single_char = sum(1 for t in tokens if len(t) == 1)
    if single_char > len(tokens) / 2:
        return re.sub(r'\s+', '', name)
    return name

def normalize_address(addr: str) -> str:
    if not addr or str(addr).strip() in ('', 'None', 'nan'): return ''
    addr = str(addr).lower().strip()
    addr = re.sub(r'[.,\-/]', ' ', addr)
    addr = re.sub(r'\b(jl|jl\.|jalan)\b', 'jalan', addr)
    addr = re.sub(r'\b(no|nomor)\b', 'nomor', addr)
    addr = re.sub(r'\bxii\b', '12', addr)
    addr = re.sub(r'\bxi\b', '11', addr)
    addr = re.sub(r'\bviii\b', '8', addr)
    addr = re.sub(r'\bvii\b', '7', addr)
    addr = re.sub(r'\bvi\b', '6', addr)
    addr = re.sub(r'\bix\b', '9', addr)
    addr = re.sub(r'\biv\b', '4', addr)
    addr = re.sub(r'\biii\b', '3', addr)
    addr = re.sub(r'\bii\b', '2', addr)
    addr = re.sub(r'\bv\b', '5', addr)
    return re.sub(r'\s+', ' ', addr)

def remove_dynamic_phrases(addr: str, dynamic_patterns: list) -> str:
    if not dynamic_patterns:
        return addr
    for pattern in dynamic_patterns:
        addr = pattern.sub(" ", addr)
    return re.sub(r'\s+', ' ', addr).strip()

IGNORE_WORDS = {
    "rt", "rw", "provinsi", "kabupaten", "kota", "no", "nomor",
    "desa", "kelurahan", "kec", "kecamatan", "prov", "kab",
    "adm", "daerah", "khusus", "ibukota", "dki", "indonesia", "jalan",
}

def tokenize_address(addr: str) -> list:
    return [t for t in addr.split() if t not in IGNORE_WORDS]

def compare_tokens(name1, name2):
    tokens1, tokens2 = name1.split(), name2.split()
    max_len = max(len(tokens1), len(tokens2))
    if max_len == 0: return 0, 0
    res_jaro = 0
    for i in range(max_len):
        t1 = tokens1[i] if i < len(tokens1) else None
        t2 = tokens2[i] if i < len(tokens2) else None
        if t1 and t2 and JaroWinkler.similarity(t1, t2) >= 0.8: res_jaro += 1
    return 0, round(res_jaro / max_len, 2)

def address_similarity(addr1, addr2, dynamic_patterns=None):
    addr1 = remove_dynamic_phrases(normalize_address(addr1), dynamic_patterns)
    addr2 = remove_dynamic_phrases(normalize_address(addr2), dynamic_patterns)
    t1 = tokenize_address(addr1)
    t2 = tokenize_address(addr2)
    if not t1 or not t2: return 0.0
    return r_fuzz.token_set_ratio(" ".join(t1), " ".join(t2))

# %% Processing Functions
def process_batch_bm25(rows, bm25, input_matrix, master_lookup, config, dynamic_patterns=None):
    all_results = []
    
    scored_matrix_T = bm25.scored_master_matrix.T
    threshold = config.get("BM25_THRESHOLD", 15.0)
    
    for i, row in enumerate(rows):
        id_table = row['id_table']
        nama_norm = normalize(row['nama_usaha'])
        
        result = {"id_table": id_table, "status": "unmatch", "idsbr": None, "perusahaan_id": None}
        
        row_matrix = input_matrix[i]
        row_sims = row_matrix.dot(scored_matrix_T)
        
        if row_sims.nnz == 0:
            all_results.append(result)
            continue
            
        indices, data = row_sims.indices, row_sims.data
        mask = data >= threshold
        if not np.any(mask):
            all_results.append(result)
            continue
            
        f_data, f_idx = data[mask], indices[mask]
        top_k = min(150, len(f_data))
        
        if len(f_data) > top_k:
            idx_part = np.argpartition(-f_data, top_k - 1)[:top_k]
            sorted_k = idx_part[np.argsort(-f_data[idx_part])]
        else:
            sorted_k = np.argsort(-f_data)
        
        candidates = []
        for k in sorted_k:
            m_id, m_kode, m_nama_raw, m_nama_norm, m_alamat, m_status = master_lookup[f_idx[k]]
            sim = r_fuzz.ratio(nama_norm, m_nama_norm)
            candidates.append((sim, m_id, m_kode, m_nama_raw, m_nama_norm, m_alamat, m_status))
            
        candidates.sort(key=lambda x: x[0], reverse=True)
        top_candidates = candidates
        
        match_list, possible_list = [], []
        for sim, m_id, m_kode, m_nama_raw, m_nama_norm, m_alamat, m_status in top_candidates:
            comp = compare_tokens(nama_norm, m_nama_norm)
            
            is_single_token = len(nama_norm.split()) <= 1 and len(m_nama_norm.split()) <= 1
            input_has_bh = has_badan_hukum(str(row['nama_usaha']))
            master_has_bh = has_badan_hukum(str(m_nama_raw))
            bh_mismatch = input_has_bh != master_has_bh

            res_type = 'POSSIBLE'
            if is_single_token:
                if sim >= 95 and comp[1] == 1:
                    res_type = 'MATCH'
                elif comp[1] <= 0.5 and sim <= 80:
                    res_type = 'UNMATCH'
            elif bh_mismatch:
                if comp[1] == 1 and sim >= 90:
                    res_type = 'MATCH'
                elif comp[1] <= 0.5 and sim <= 80:
                    res_type = 'UNMATCH'
            elif (comp[1] == 1 and sim >= 80) or (sim >= 90 and comp[1] >= 0.75):
                res_type = 'MATCH'
            elif comp[1] <= 0.5 and sim <= 80:
                res_type = 'UNMATCH'
            
            obj = {"idsbr": m_kode, "id": m_id, "alamat": m_alamat, "type": res_type, "status_id": m_status}
            if res_type == 'MATCH': match_list.append(obj)
            elif res_type == 'POSSIBLE': possible_list.append(obj)

        if match_list:
            # Prioritaskan status_perusahaan_id: 1 > 2 > 3 > 5 > 8 > lainnya
            match_list.sort(key=lambda m: status_priority(m.get('status_id')))
            found = False
            for m in match_list:
                if address_similarity(row['alamat'], m['alamat'], dynamic_patterns) >= 70:
                    result.update({"status": "match", "idsbr": m["idsbr"], "perusahaan_id": m["id"]})
                    found = True; break
            if not found: result.update({"status": "possible"})
        elif possible_list:
            result.update({"status": "possible"})
        
        all_results.append(result)
    return all_results

def bulk_update_results(conn, results, table_name, config):
    if not results: return 0, 0
    cursor = conn.cursor()
    cursor.fast_executemany = True
    q = f"UPDATE {table_name} SET {config['COL_IDSBR']} = ?, {config['COL_PERUSAHAAN_ID']} = ?, {config['COL_POSSIBLE_MATCH']} = ? WHERE id_table = ?"
    data = [(r["idsbr"], r["perusahaan_id"], r["status"], r["id_table"]) for r in results]
    try:
        cursor.executemany(q, data); conn.commit(); return len(results), 0
    except Exception:
        conn.rollback(); return 0, len(results)
    finally: cursor.close()

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

# %% Worker Function
def process_kabupaten(kab_id, config):
    worker_start = time.time()
    try:
        conn = pyodbc.connect(config["CONNECTION_STRING"])
    except Exception as e:
        return {"kab_id": kab_id, "status": "error", "message": f"DB connect error: {e}"}

    cursor = conn.cursor()
    table_name = config["TABLE_NAME"]
    col_pm = config["COL_POSSIBLE_MATCH"]

    try:
        # Fetch provinsi_id
        cursor.execute(f"""
            SELECT DISTINCT provinsi_id_etl as prov_id FROM {table_name}
            WHERE kabupaten_kota_id_etl = ? AND provinsi_id_etl is not null
              AND nama_etl IS NOT NULL AND alamat_etl IS NOT NULL
            {config.get("EXTRA_WHERE_CLAUSE", "")}
        """, (kab_id,))
        prov_rows = cursor.fetchall()
        prov_id = prov_rows[0].prov_id if prov_rows else None

        if not prov_id:
            return {"kab_id": kab_id, "status": "skip", "message": "No provinsi_id"}

        # Fetch area names for dynamic patterns
        cursor.execute("""
            SELECT akk.nama as kab_nama, ap.nama as prov_nama
            FROM area_kabupaten_kota akk
            JOIN area_provinsi ap ON akk.provinsi_id = ap.id AND ap.snapshot_id = 4
            WHERE akk.id = ?
        """, (kab_id,))
        area_row = cursor.fetchone()
        dynamic_patterns = []
        if area_row:
            kab_nama_raw = str(area_row.kab_nama).strip()
            prov_nama_raw = str(area_row.prov_nama).strip()
            kab_nama_clean = re.sub(r'^(kota(?: adm(?:inistrasi)?)?|kabupaten|kab)\s+', '', kab_nama_raw, flags=re.IGNORECASE)
            prov_nama_clean = re.sub(r'^(provinsi|prov)\s+', '', prov_nama_raw, flags=re.IGNORECASE)
            kab_nama_clean = collapse_spaced_name(kab_nama_clean)
            prov_nama_clean = collapse_spaced_name(prov_nama_clean)
            pattern_kab = re.compile(rf'\b(?:kota(?: adm(?:inistrasi)?)?|kabupaten|kab)?\s*{re.escape(kab_nama_clean)}\b', re.IGNORECASE)
            pattern_prov = re.compile(rf'\b(?:provinsi|prov)?\s*{re.escape(prov_nama_clean)}\b', re.IGNORECASE)
            dynamic_patterns = [pattern_kab, pattern_prov]

        # Load Master
        log.info(f"[Kab {kab_id}] Loading master data...")
        m_query = """
            SELECT id, nama, alamat, kode, status_perusahaan_id FROM matchapro_business_perusahaan
            WHERE provinsi_id = ? AND kabupaten_kota_id = ?
              AND (status_perusahaan_id not in (9,10) or status_perusahaan_id is null)
        """
        master_df = pd.read_sql(m_query, conn, params=[prov_id, kab_id])
        log.info(f"[Kab {kab_id}] Master: {len(master_df)} rows")
        if master_df.empty:
            return {"kab_id": kab_id, "status": "skip", "message": "No master data"}

        # Build BM25
        idx_start = time.time()
        bm25 = BM25Transformer(ngram_range=config["NGRAM_RANGE"], k1=config["BM25_K1"], b=config["BM25_B"])
        corpus, master_lookup = [], []
        for idx, row in master_df.iterrows():
            n_name = normalize(row['nama'])
            alamat_norm = remove_dynamic_phrases(normalize_address(row['alamat']), dynamic_patterns)
            n_addr = " ".join(tokenize_address(alamat_norm)[:5])
            corpus.append(n_name + " " + n_addr)
            master_lookup.append((row['id'], row['kode'], row['nama'], n_name, row['alamat'], row.get('status_perusahaan_id')))
        bm25.fit(corpus)
        idx_time = time.time() - idx_start
        log.info(f"[Kab {kab_id}] BM25 index built in {idx_time:.1f}s")
        del master_df

        # Load Input
        i_query = f"""
            SELECT id_table, nama_etl as nama_usaha, alamat_etl as alamat
            FROM {table_name}
            WHERE {col_pm} IS NULL AND kabupaten_kota_id_etl = ? AND provinsi_id_etl is not null
              AND nama_etl IS NOT NULL AND alamat_etl IS NOT NULL
            {config.get("EXTRA_WHERE_CLAUSE", "")}
        """
        cursor.execute(i_query, (kab_id,))
        cols = [c[0] for c in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
        if not rows:
            return {"kab_id": kab_id, "status": "skip", "message": "No pending rows"}

        log.info(f"[Kab {kab_id}] Input: {len(rows)} rows")

        # Process
        match_start = time.time()
        input_corpus = []
        for r in rows:
            alamat_norm = remove_dynamic_phrases(normalize_address(r['alamat']), dynamic_patterns)
            input_corpus.append(normalize(r['nama_usaha']) + " " + " ".join(tokenize_address(alamat_norm)[:5]))
        input_matrix = bm25.transform(input_corpus)
        results = process_batch_bm25(rows, bm25, input_matrix, master_lookup, config, dynamic_patterns)
        match_time = time.time() - match_start

        # Count statistics
        match_count = sum(1 for r in results if r["status"] == "match")
        possible_count = sum(1 for r in results if r["status"] == "possible")
        unmatch_count = sum(1 for r in results if r["status"] == "unmatch")
        log.info(f"[Kab {kab_id}] Matching completed in {match_time:.1f}s (match={match_count}, possible={possible_count}, unmatch={unmatch_count})")

        # Update
        kab_success, kab_errors = 0, 0
        for batch in chunked(results, config["BULK_SIZE"]):
            s, e = bulk_update_results(conn, batch, table_name, config)
            kab_success += s
            kab_errors += e

        worker_time = time.time() - worker_start
        return {
            "kab_id": kab_id, "status": "success", "processed": len(rows),
            "match": match_count, "possible": possible_count, "unmatch": unmatch_count,
            "errors": kab_errors,
            "message": f"Updated {kab_success} rows in {worker_time:.1f}s (Index:{idx_time:.1f}s Match:{match_time:.1f}s)"
        }

    except Exception as e:
        return {"kab_id": kab_id, "status": "error", "message": str(e)}
    finally:
        cursor.close()
        conn.close()

# %% Checkpoint Logic
def load_checkpoint(fp):
    if os.path.exists(fp):
        with open(fp, 'r') as f: return set(json.load(f).get("completed", []))
    return set()

def save_checkpoint(fp, completed):
    with open(fp, 'w') as f: json.dump({"completed": sorted(list(completed))}, f)

# %% Main
def main():
    table_name = CONFIG["TABLE_NAME"]
    conn_string = CONFIG["CONNECTION_STRING"]
    checkpoint_file = CONFIG["CHECKPOINT_FILE"]
    max_workers = CONFIG["MAX_WORKERS"]
    test_kab = CONFIG["TEST_KAB_ID"]
    col_pm = CONFIG["COL_POSSIBLE_MATCH"]

    log.info("=" * 70)
    log.info("MATCHING ALGORITHM — BM25 (MULTIPROCESSING)")
    log.info(f"Workers: {max_workers} | K1: {CONFIG['BM25_K1']} | B: {CONFIG['BM25_B']} | Threshold: {CONFIG['BM25_THRESHOLD']}")
    log.info(f"Table: {table_name}")
    log.info(f"Columns: idsbr={CONFIG['COL_IDSBR']}, possible_match={col_pm}")
    log.info(f"Extra Filter: {CONFIG.get('EXTRA_WHERE_CLAUSE', 'None')}")
    log.info("=" * 70)

    try:
        conn = pyodbc.connect(conn_string)
    except Exception as e:
        log.error(f"Cannot connect to DB: {e}")
        return

    cursor = conn.cursor()

    # Add columns
    for col, dtype in [(CONFIG["COL_IDSBR"], "VARCHAR(255)"), (CONFIG["COL_PERUSAHAAN_ID"], "INT"), (col_pm, "VARCHAR(50)")]:
        try:
            cursor.execute(f"ALTER TABLE {table_name} ADD {col} {dtype}"); conn.commit()
            log.info(f"✅ Kolom {col} berhasil ditambahkan!")
        except Exception:
            conn.rollback()

    # Get Kab IDs
    if test_kab:
        all_kabs = [test_kab]
    else:
        cursor.execute(f"""
            SELECT DISTINCT kabupaten_kota_id_etl as kab_id FROM {table_name}
            WHERE {col_pm} IS NULL AND provinsi_id_etl is not null AND kabupaten_kota_id_etl is not null
              AND nama_etl IS NOT NULL AND alamat_etl IS NOT NULL
            {CONFIG.get("EXTRA_WHERE_CLAUSE", "")}
            ORDER BY kab_id
        """)
        all_kabs = [r.kab_id for r in cursor.fetchall()]
    conn.close()

    completed = load_checkpoint(checkpoint_file)
    pending = [k for k in all_kabs if k not in completed]

    log.info(f"Total Kab: {len(all_kabs)} | Completed: {len(completed)} | Pending: {len(pending)}")

    if not pending:
        log.info("✅ Semua kabupaten sudah selesai!")
        return

    global_start = time.time()
    total_processed = 0
    total_match = 0
    total_possible = 0
    total_unmatch = 0
    total_errors = 0
    processed_count = 0

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_kabupaten, k, CONFIG): k for k in pending}
        for future in as_completed(futures):
            k = futures[future]
            processed_count += 1
            try:
                res = future.result()
                if res["status"] in ["success", "skip"]:
                    if res["status"] == "success":
                        total_processed += res.get("processed", 0)
                        total_match += res.get("match", 0)
                        total_possible += res.get("possible", 0)
                        total_unmatch += res.get("unmatch", 0)
                        total_errors += res.get("errors", 0)
                        log.info(f"[Kab {k}] ✅ {res['message']} ({processed_count}/{len(pending)})")
                    else:
                        log.info(f"[Kab {k}] ⏭️ Skipped: {res['message']} ({processed_count}/{len(pending)})")
                    completed.add(k)
                    save_checkpoint(checkpoint_file, completed)
                else:
                    log.error(f"[Kab {k}] ❌ Error: {res['message']}")
            except Exception as e:
                log.error(f"[Kab {k}] ❌ Fatal: {e}")

    global_elapsed = time.time() - global_start
    log.info("=" * 70)
    log.info(f"🎉 BM25 MATCHING ALL DONE!")
    log.info(f"Total rows processed: {total_processed}")
    log.info(f"Results: match={total_match}, possible={total_possible}, unmatch={total_unmatch}")
    log.info(f"Errors: {total_errors}")
    log.info(f"Total time: {global_elapsed / 60:.1f} minutes")
    log.info("=" * 70)

if __name__ == "__main__":
    main()
