# Matching Alamat BPOM ke Master SLS (RT-level)

## Deskripsi

Script ini mencocokkan alamat dari data BPOM (`alamat_etl`) ke Master Frame Desa 2025.2 (`nmsls_25_2`) berdasarkan **nomor RT**, dengan **blocking by kelurahan_desa_id**.

### Tabel Sumber

| Tabel | Deskripsi | Kolom Utama |
|-------|-----------|-------------|
| `matcha_datatemp_direktori_bpom_sheet1_saranaizinedar_data11Mei2026_20260518` | Data alamat BPOM | `id_table`, `alamat_etl`, `kelurahan_desa_id_2025_2` |
| `matcha_mfd_2025_2_hirarki_sls` | Master SLS (Satuan Lingkungan Setempat) | `kelurahan_desa_id`, `nmsls_25_2` |

### Filter Input
- `alamat_etl LIKE '% RT %'` — hanya alamat yang mengandung RT
- `is_duplicate = 0` — bukan duplikat

## Cara Kerja

### 1. Ekstraksi RT/RW dari `alamat_etl`

Script mengenali berbagai format penulisan RT/RW:

| Format Contoh | RT | RW |
|--------------|----|----|
| `RT 04, RW 27` | 4 | 27 |
| `RT 005 RW 011` | 5 | 11 |
| `RT 01/RW 04` | 1 | 4 |
| `RT 009 / RW 003` | 9 | 3 |
| `RT 02 /07` | 2 | 7 |
| `RT 04/15` | 4 | 15 |
| `Rt 005 Rw 002` | 5 | 2 |
- `RT 01/RW 04` → `RT=1, RW=4`
- `Rt 005 Rw 002` → `RT=5, RW=2`
- `RT II RW IV` → `RT=2, RW=4` (Angka Romawi didukung hingga XX)

### Area/Dusun Fallback
Script dirancang untuk menangani variasi alamat **tanpa nomor RT/RW**. Jika regex gagal menemukan indikator RT/RW di input (atau jika Master SLS di kelurahan tersebut tidak mencantumkan nomor RT sama sekali), proses akan melakukan **Fuzzy Matching terhadap nama Dusun** melawan seluruh entri SLS di kelurahan terkait.

**Aturan penting:**
- **RT selalu angka pertama**, RW selalu angka kedua
- `RT 001 / 003` = RT 1, RW 3 (**bukan** sebaliknya)
- Leading zeros dihilangkan (`005` → `5`)

### 2. Ekstraksi RT dari `nmsls_25_2`

Format SLS lebih konsisten:
```
RT {nomor} {NAMA AREA}
```

Contoh:
- `RT 1 DUSUN DAMAI MAKMUR` → RT=1, Area="DUSUN DAMAI MAKMUR"
- `RT 03 DUSUN MELATI` → RT=3, Area="DUSUN MELATI"

### 3. Blocking & Matching

```
┌──────────────────────────────────────────────────────────────────┐
│ Untuk setiap row input (alamat_etl):                             │
│                                                                  │
│ 1. Block by kelurahan_desa_id_2025_2                             │
│    ↕ match dengan kelurahan_desa_id di SLS                       │
│                                                                  │
│ 2. Filter SLS candidates yang RT number == RT input              │
│                                                                  │
│ 3. Jika ada RW di alamat DAN di SLS:                             │
│    → Narrow by RT + RW                                           │
│                                                                  │
│ 4. Jika 0 candidates  → UNMATCH                                 │
│    Jika 1 candidate   → MATCH (auto)                             │
│    Jika semua candidate nmsls-nya sama (duplikat) → MATCH        │
│    Jika >1 unique candidates:                                    │
│       → Fuzzy match nama dusun/kampung dari alamat               │
│       ↳ Score >= 55 & gap >= 10 dari runner-up → MATCH           │
│       ↳ Selainnya → POSSIBLE                                     │
└──────────────────────────────────────────────────────────────────┘
```


### 4. Fuzzy Match Nama Dusun

Ketika ada **multiple SLS dengan RT yang sama** dalam satu kelurahan (misal RT 1 ada di 3 dusun berbeda), script akan:
1. Mengekstrak nama area dari `alamat_etl` (dusun/kampung/lingkungan/dukuh)
2. Membandingkan dengan nama area di setiap SLS candidate menggunakan `token_set_ratio`
3. Jika best score ≥ 60 → **match**, jika < 60 → **possible**

## Cara Menjalankan

### Prerequisites
```bash
pip install pandas pyodbc python-dotenv rapidfuzz
```

### File `.env`
```env
DB_SERVER=10.0.45.9
DB_NAME=sbrdb
DB_USER=sbruser
DB_PASSWORD=xxxxx
```

### Jalankan
```bash
Script `match_alamat_sls.py` menjalankan alur berikut:

1. **Memuat Data Input (BPOM)**
   - Script akan memuat seluruh alamat dari tabel ETL BPOM yang tidak ditandai sebagai duplikat (`is_duplicate = 0`).
2. **Memuat Data Master (SLS)**
   - Mengambil data SLS untuk kelurahan yang ada pada data input.
3. **Ekstraksi RT/RW**
   - Mendeteksi format angka biasa maupun **angka romawi** (misal: RT II = RT 2).
   - Ekstraksi juga dilakukan pada nama SLS di tabel master (`nmsls_25_2`).
4. **Matching & Blocking**
   - **Blocking** dilakukan pada level kelurahan (hanya membandingkan alamat dengan SLS pada kelurahan yang sama).
   - **Filter 1 (Exact RT+RW Match):** Memilih kandidat SLS dengan nomor RT (dan RW jika ada) yang sama persis.
   - **Filter 2 (Dusun Fallback):** Jika alamat tidak memiliki nomor RT atau RT tidak ditemukan di Master SLS, script akan mem-bypass filter RT dan membandingkan nama Dusun secara fuzzy dengan seluruh kandidat SLS di kelurahan tersebut.
   - **Filter 3 (Fuzzy Match Name):** Jika terdapat banyak kandidat dengan RT yang sama (atau saat fallback), dilakukan perbandingan teks berbasis fuzzy (`token_set_ratio`) pada nama dusun/lingkungan (threshold 55).
5. **Output**
   - Hasil akan disimpan langsung ke tabel baru di database dengan nama tabel sama seperti tabel input ditambah akhiran `_hasil_match_sls`.
```

## Output

### File: `hasil_matching_alamat_sls.csv`

| Kolom | Deskripsi |
|-------|-----------|
| `id_table` | ID dari tabel BPOM |
| `alamat_etl` | Alamat asli dari BPOM |
| `kelurahan_desa_id` | Kelurahan ID (blocking key) |
| `rt_extracted` | Nomor RT yang di-extract dari alamat |
| `rw_extracted` | Nomor RW yang di-extract dari alamat |
| `nmsls_25_2` | Nama SLS yang match |
| `sls_rt` | RT dari SLS yang match |
| `sls_area` | Nama area dari SLS yang match |
| `match_status` | Status matching (lihat tabel di bawah) |
| `match_score` | Score fuzzy matching (0-100) |
| `candidates_count` | Jumlah SLS candidates dengan RT yang sama |
| `all_candidates` | Semua candidates (jika multiple) |

### Status Matching

| Status | Deskripsi |
|--------|-----------|
| `match` | Match — RT exact + area name confirmed |
| `possible_multiple_rt` | RT match tapi ada banyak candidates, tidak bisa tentukan area |
| `possible_low_score` | RT match tapi fuzzy score nama dusun terlalu rendah |
| `unmatch_no_rt` | Gagal extract RT dari alamat |
| `unmatch_no_kelurahan` | Tidak ada kelurahan_desa_id |
| `unmatch_no_sls` | Tidak ada SLS data untuk kelurahan tersebut |
| `unmatch_rt_not_found` | RT number tidak ditemukan di SLS kelurahan tersebut |

## Log

Log tersimpan di `matching_alamat_sls.log` dan juga ditampilkan di console.
