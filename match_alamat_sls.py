"""
Matching Alamat BPOM ke Master SLS (RT-level)
==============================================
Mencocokkan alamat_etl dari tabel BPOM ke nmsls_25_2 dari tabel master SLS
berdasarkan nomor RT + RW, dengan blocking by kelurahan_desa_id.

Output: Tabel baru di database (TABLE_INPUT + '_hasil_match_sls').

Rules:
- RT selalu angka pertama, RW selalu angka kedua
- RT 001/003 = RT 1, RW 3 (bukan sebaliknya)
- RT harus exact match (RT 1 != RT 2)
- Jika ada RW di alamat dan SLS, gunakan RT+RW untuk narrowing
- Jika multiple SLS candidates, fuzzy match nama dusun
- Jika tidak ada RT di alamat atau SLS, fallback ke fuzzy match dusun
"""

import pandas as pd
import numpy as np
import pyodbc
import re
import os
import sys
import time
import logging
from rapidfuzz import fuzz as r_fuzz
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────
CONFIG = {
    "CONNECTION_STRING": (
        'DRIVER={ODBC Driver 18 for SQL Server};'
        f'SERVER={os.getenv("DB_SERVER")};DATABASE={os.getenv("DB_NAME")};'
        f'UID={os.getenv("DB_USER")};PWD={os.getenv("DB_PASSWORD")};'
        'TrustServerCertificate=yes;Connection Timeout=30;'
    ),
    "TABLE_INPUT": "matcha_datatemp_direktori_bpom_sheet1_saranaizinedar_data11Mei2026_20260518",
    "TABLE_SLS": "matcha_mfd_2025_2_hirarki_sls",
    "FUZZY_THRESHOLD": 55,  # minimum score untuk fuzzy match nama dusun
}

# ── Logging ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[
        logging.FileHandler("matching_alamat_sls.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

ROMAN_TO_INT = {
    'i': 1, 'ii': 2, 'iii': 3, 'iv': 4, 'v': 5,
    'vi': 6, 'vii': 7, 'viii': 8, 'ix': 9, 'x': 10,
    'xi': 11, 'xii': 12, 'xiii': 13, 'xiv': 14, 'xv': 15,
    'xvi': 16, 'xvii': 17, 'xviii': 18, 'xix': 19, 'xx': 20
}

def parse_number_or_roman(val: str):
    if not val:
        return None
    val = val.lower().strip()
    if val.isdigit():
        return int(val)
    if val in ROMAN_TO_INT:
        return ROMAN_TO_INT[val]
    return None

# ── RT/RW Extraction from alamat_etl ──────────────────────────────────

def extract_rt_rw(alamat: str) -> tuple:
    """
    Extract RT and RW numbers from alamat_etl.
    
    Handles formats:
      - RT 04, RW 27          → RT=4, RW=27
      - RT 005 RW 011         → RT=5, RW=11
      - RT 01/RW 04           → RT=1, RW=4
      - RT 009 / RW 003       → RT=9, RW=3
      - RT 02 /07             → RT=2, RW=7
      - RT 04/15              → RT=4, RW=15
      - Rt 005 Rw 002         → RT=5, RW=2
      - RT 001/003            → RT=1, RW=3
    
    Rule ketat: RT = angka pertama, RW = angka kedua. Tidak pernah terbalik.
    
    Returns:
        (rt_number: int or None, rw_number: int or None)
    """
    if not alamat or str(alamat).strip() in ('', 'None', 'nan'):
        return None, None
    
    text = str(alamat).strip()
    
    rt_num = None
    rw_num = None
    
    # Pattern 1: Explicit "RT xxx" - capture the number or roman after RT
    rt_match = re.search(r'\bRT\s*\.?\s*([0-9]{1,3}|[IVXLCDMivxlcdm]+)\b', text, re.IGNORECASE)
    if rt_match:
        rt_num = parse_number_or_roman(rt_match.group(1))
    
    # Pattern 2: Explicit "RW xxx" - capture the number or roman after RW
    rw_match = re.search(r'\bRW\s*\.?\s*([0-9]{1,3}|[IVXLCDMivxlcdm]+)\b', text, re.IGNORECASE)
    if rw_match:
        rw_num = parse_number_or_roman(rw_match.group(1))
    
    # Pattern 3: If RT found but RW not found, look for slash pattern after RT number
    # e.g., "RT 02 /07" or "RT 04/15" or "RT 001 / 003"
    if rt_num is not None and rw_num is None:
        slash_match = re.search(
            r'\bRT\s*\.?\s*(?:[0-9]{1,3}|[IVXLCDMivxlcdm]+)\s*[/\\]\s*([0-9]{1,3}|[IVXLCDMivxlcdm]+)\b',
            text, re.IGNORECASE
        )
        if slash_match:
            rw_num = parse_number_or_roman(slash_match.group(1))
    
    return rt_num, rw_num


def extract_area_name(alamat: str) -> str:
    """
    Extract area name (dusun/kampung/lingkungan) from alamat_etl for fuzzy matching.
    """
    if not alamat:
        return ""
    
    text = str(alamat).strip()
    
    # Try to find explicit dusun/kampung/lingkungan names
    patterns = [
        r'\b(?:DSN|DUSUN)\s+([A-Za-z\s]+?)(?:\s*(?:RT|RW|NO|,|\d))',
        r'\b(?:KP|KAMPUNG)\s+([A-Za-z\s]+?)(?:\s*(?:RT|RW|NO|,|\d))',
        r'\b(?:LINGKUNGAN|LINK|LNGK)\s+([A-Za-z\s]+?)(?:\s*(?:RT|RW|NO|,|\d))',
        r'\b(?:DUKUH|DK)\s+([A-Za-z\s]+?)(?:\s*(?:RT|RW|NO|,|\d))',
        r'\b(?:PERUM|PERUMAHAN)\s+([A-Za-z\s]+?)(?:\s*(?:RT|RW|NO|,|\d))',
    ]
    
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip().upper()
    
    return ""


# ── RT/RW Extraction from nmsls_25_2 ──────────────────────────────────

def extract_rt_rw_from_sls(nmsls: str) -> tuple:
    """
    Extract RT number, RW number, and area name from nmsls_25_2.
    
    Formats:
      - "RT 1 DUSUN DAMAI MAKMUR"     → RT=1, RW=None, area="DUSUN DAMAI MAKMUR"
      - "RT 002 RW 001 DUSUN 3"       → RT=2, RW=1,    area="DUSUN 3"
      - "RT 01 RW 01"                 → RT=1, RW=1,    area=""
      - "RT 007 RW 04"                → RT=7, RW=4,    area=""
    
    Returns:
        (rt_number: int or None, rw_number: int or None, area_name: str)
    """
    if not nmsls or str(nmsls).strip() in ('', 'None', 'nan'):
        return None, None, ""
    
    text = str(nmsls).strip()
    
    # Extract RT
    rt_match = re.search(r'\bRT\s+([0-9]{1,3}|[IVXLCDMivxlcdm]+)\b', text, re.IGNORECASE)
    if not rt_match:
        return None, None, text
    
    rt_num = parse_number_or_roman(rt_match.group(1))
    
    # Extract RW
    rw_match = re.search(r'\bRW\s+([0-9]{1,3}|[IVXLCDMivxlcdm]+)\b', text, re.IGNORECASE)
    rw_num = parse_number_or_roman(rw_match.group(1)) if rw_match else None
    
    # Extract area name: everything after the RT/RW numbers
    # Remove RT xxx and RW xxx parts to get the area name
    area = text
    area = re.sub(r'\bRT\s+\d{1,3}', '', area, flags=re.IGNORECASE)
    area = re.sub(r'\bRW\s+\d{1,3}', '', area, flags=re.IGNORECASE)
    area = re.sub(r'\s+', ' ', area).strip()
    
    return rt_num, rw_num, area


# ── Matching Logic ─────────────────────────────────────────────────────

def match_single_row(row, sls_by_kel, config):
    """
    Match a single alamat_etl row against SLS candidates.
    
    Strategy:
    1. Filter by exact RT number
    2. If RW available in both alamat and SLS → narrow by RT+RW
    3. If still multiple → fuzzy match area name
    4. If single → auto match
    """
    id_table = row['id_table']
    alamat = str(row['alamat_etl'] or '')
    kel_id = row['kelurahan_desa_id_2025_2']
    
    result = {
        'id_table': id_table,
        'alamat_etl': alamat,
        'kelurahan_desa_id': kel_id,
        'rt_extracted': None,
        'rw_extracted': None,
        'idsubsls_25_2': None,
        'nmsls_25_2': None,
        'sls_rt': None,
        'sls_rw': None,
        'sls_area': None,
        'match_status': 'unmatch',
        'match_score': None,
        'candidates_count': 0,
        'all_candidates': None,
    }
    
    # Extract RT/RW from alamat
    rt, rw = extract_rt_rw(alamat)
    result['rt_extracted'] = rt
    result['rw_extracted'] = rw
    

    
    if kel_id is None:
        result['match_status'] = 'unmatch_no_kelurahan'
        return result
    
    # Get SLS candidates for this kelurahan
    candidates = sls_by_kel.get(kel_id, [])
    if not candidates:
        result['match_status'] = 'unmatch_no_sls'
        return result
    
    working_set = []
    rt_matches = []
    
    if rt is not None:
        # Filter by exact RT number
        rt_matches = [c for c in candidates if c['rt'] == rt]
        if rt_matches:
            if rw is not None:
                rt_rw_matches = [c for c in rt_matches if c['rw'] == rw]
                working_set = rt_rw_matches if rt_rw_matches else rt_matches
            else:
                working_set = rt_matches
                
    # Fallback if working_set is empty (no RT in alamat, or RT not found in SLS)
    is_fallback = False
    if not working_set:
        working_set = candidates
        is_fallback = True
        
    result['candidates_count'] = len(working_set)
    result['all_candidates'] = '; '.join([c['nmsls'] for c in working_set])
    
    unique_nmsls = set(c['nmsls'] for c in working_set)
    
    # If exactly 1 candidate (and not in fallback mode) -> auto match
    if len(working_set) == 1 and not is_fallback:
        c = working_set[0]
        result['idsubsls_25_2'] = c['idsubsls']
        result['nmsls_25_2'] = c['nmsls']
        result['sls_rt'] = c['rt']
        result['sls_rw'] = c['rw']
        result['sls_area'] = c['area']
        result['match_status'] = 'match'
        result['match_score'] = 100
        return result
    
    # Or if duplicates of the same SLS and not in fallback mode -> auto match
    if len(unique_nmsls) == 1 and not is_fallback:
        c = working_set[0]
        result['idsubsls_25_2'] = c['idsubsls']
        result['nmsls_25_2'] = c['nmsls']
        result['sls_rt'] = c['rt']
        result['sls_rw'] = c['rw']
        result['sls_area'] = c['area']
        result['match_status'] = 'match'
        result['match_score'] = 100
        return result
    
    # Step 4: Multiple candidates → fuzzy match area name
    area_from_alamat = extract_area_name(alamat)
    
    if not area_from_alamat:
        if is_fallback:
            result['match_status'] = 'unmatch_no_rt_no_area'
        else:
            result['match_status'] = 'possible_multiple_no_area'
        result['nmsls_25_2'] = result['all_candidates']
        return result
    
    # Score each candidate (deduplicate by nmsls first)
    seen_nmsls = set()
    unique_candidates = []
    for c in working_set:
        if c['nmsls'] not in seen_nmsls:
            seen_nmsls.add(c['nmsls'])
            unique_candidates.append(c)
    
    scored = []
    for c in unique_candidates:
        if c['area']:
            score = r_fuzz.token_set_ratio(area_from_alamat, c['area'].upper())
        else:
            score = 0
        scored.append((score, c))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_c = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    
    # Match if best score is high enough AND clearly better than runner-up
    if best_score >= config['FUZZY_THRESHOLD'] and (best_score - second_score) >= 10:
        result['idsubsls_25_2'] = best_c['idsubsls']
        result['nmsls_25_2'] = best_c['nmsls']
        result['sls_rt'] = best_c['rt']
        result['sls_rw'] = best_c['rw']
        result['sls_area'] = best_c['area']
        result['match_status'] = 'match'
        result['match_score'] = best_score
    else:
        result['match_status'] = 'possible_low_score'
        result['nmsls_25_2'] = result['all_candidates']
        result['match_score'] = best_score
    
    return result


# ── Main ───────────────────────────────────────────────────────────────

def main():
    start_time = time.time()
    
    log.info("=" * 70)
    log.info("MATCHING ALAMAT BPOM → MASTER SLS (RT+RW LEVEL)")
    log.info("=" * 70)
    
    # Connect
    log.info("Connecting to database...")
    conn = pyodbc.connect(CONFIG["CONNECTION_STRING"])
    
    # ── Load input data ────────────────────────────────────────────────
    log.info("Loading input data...")
    query_input = f"""
        SELECT id_table, alamat_etl, kelurahan_desa_id_2025_2
        FROM {CONFIG['TABLE_INPUT']}
        WHERE is_duplicate = 0
    """
    df_input = pd.read_sql(query_input, conn)
    log.info(f"  Loaded {len(df_input)} input rows")
    
    # ── Load SLS data ──────────────────────────────────────────────────
    kel_ids = df_input['kelurahan_desa_id_2025_2'].dropna().unique().tolist()
    log.info(f"  Need SLS for {len(kel_ids)} distinct kelurahan")
    
    if not kel_ids:
        log.error("No kelurahan IDs found in input data!")
        conn.close()
        return
    
    sls_frames = []
    batch_size = 500
    for i in range(0, len(kel_ids), batch_size):
        batch = kel_ids[i:i+batch_size]
        placeholders = ','.join(['?'] * len(batch))
        query_sls = f"""
            SELECT kelurahan_desa_id, idsubsls_25_2, nmsls_25_2
            FROM {CONFIG['TABLE_SLS']}
            WHERE kelurahan_desa_id IN ({placeholders})
        """
        df_batch = pd.read_sql(query_sls, conn, params=batch)
        sls_frames.append(df_batch)
    
    df_sls = pd.concat(sls_frames, ignore_index=True) if sls_frames else pd.DataFrame()
    log.info(f"  Loaded {len(df_sls)} SLS rows")
    # Keep connection open for writing results later
    
    # ── Build SLS lookup ───────────────────────────────────────────────
    log.info("Building SLS lookup by kelurahan...")
    sls_by_kel = {}
    for _, row in df_sls.iterrows():
        kel_id = row['kelurahan_desa_id']
        nmsls = str(row['nmsls_25_2'] or '').strip()
        idsubsls = row['idsubsls_25_2']
        sls_rt, sls_rw, sls_area = extract_rt_rw_from_sls(nmsls)
        if sls_rt is not None:
            if kel_id not in sls_by_kel:
                sls_by_kel[kel_id] = []
            sls_by_kel[kel_id].append({
                'rt': sls_rt,
                'rw': sls_rw,
                'area': sls_area,
                'nmsls': nmsls,
                'idsubsls': idsubsls,
            })
    
    log.info(f"  SLS lookup built: {len(sls_by_kel)} kelurahan with RT-based SLS")
    
    # ── Match ──────────────────────────────────────────────────────────
    log.info("Starting matching...")
    results = []
    for idx, row in df_input.iterrows():
        result = match_single_row(row, sls_by_kel, CONFIG)
        results.append(result)
        if (idx + 1) % 100 == 0:
            log.info(f"  Processed {idx + 1}/{len(df_input)} rows")
    
    log.info(f"  Processed all {len(df_input)} rows")
    
    # ── Statistics ─────────────────────────────────────────────────────
    df_results = pd.DataFrame(results)
    
    status_counts = df_results['match_status'].value_counts()
    log.info("\n" + "=" * 50)
    log.info("MATCHING RESULTS:")
    for status, count in status_counts.items():
        log.info(f"  {status}: {count}")
    log.info(f"  TOTAL: {len(df_results)}")
    log.info("=" * 50)
    
    # ── Save to DB table ───────────────────────────────────────────────
    output_table = CONFIG['TABLE_INPUT'] + '_hasil_match_sls'
    log.info(f"Saving results to table: {output_table}")
    
    output_cols = [
        'id_table', 'alamat_etl', 'kelurahan_desa_id',
        'rt_extracted', 'rw_extracted',
        'idsubsls_25_2', 'nmsls_25_2', 'sls_rt', 'sls_rw', 'sls_area',
        'match_status', 'match_score',
        'candidates_count', 'all_candidates'
    ]
    df_output = df_results[output_cols].copy()
    
    cursor = conn.cursor()
    
    # Drop table if exists, then create
    try:
        cursor.execute(f"DROP TABLE IF EXISTS {output_table}")
        conn.commit()
    except Exception:
        conn.rollback()
    
    create_sql = f"""
        CREATE TABLE {output_table} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            id_table INT,
            alamat_etl NVARCHAR(MAX),
            kelurahan_desa_id INT,
            rt_extracted INT,
            rw_extracted INT,
            idsubsls_25_2 NVARCHAR(MAX),
            nmsls_25_2 NVARCHAR(MAX),
            sls_rt INT,
            sls_rw INT,
            sls_area NVARCHAR(MAX),
            match_status VARCHAR(50),
            match_score FLOAT,
            candidates_count INT,
            all_candidates NVARCHAR(MAX)
        )
    """
    cursor.execute(create_sql)
    conn.commit()
    log.info(f"  Table {output_table} created")
    
    # Insert data in batches
    insert_sql = f"""
        INSERT INTO {output_table}
        (id_table, alamat_etl, kelurahan_desa_id, rt_extracted, rw_extracted,
         idsubsls_25_2, nmsls_25_2, sls_rt, sls_rw, sls_area,
         match_status, match_score, candidates_count, all_candidates)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    
    cursor.fast_executemany = False  # Safer with NVARCHAR(MAX) and None values
    insert_batch_size = 100
    
    rows_inserted = 0
    for _, row in df_output.iterrows():
        vals = []
        for col in output_cols:
            v = row[col]
            if v is None or (isinstance(v, float) and np.isnan(v)):
                vals.append(None)
            elif isinstance(v, (np.integer,)):
                vals.append(int(v))
            elif isinstance(v, (np.floating,)):
                vals.append(float(v))
            else:
                vals.append(v)
        cursor.execute(insert_sql, vals)
        rows_inserted += 1
        if rows_inserted % insert_batch_size == 0:
            conn.commit()
    conn.commit()
    
    log.info(f"  Inserted {rows_inserted} rows into {output_table}")
    
    cursor.close()
    conn.close()
    log.info(f"\n✅ Results saved to table: {output_table}")
    
    elapsed = time.time() - start_time
    log.info(f"Total time: {elapsed:.1f}s")
    log.info("=" * 70)


if __name__ == "__main__":
    main()
