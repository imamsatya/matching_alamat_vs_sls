# Cara Kerja Script: Matching Alamat BPOM vs Master SLS

Script `match_alamat_sls.py` adalah sebuah program berbasis Python yang dirancang untuk mencocokkan data alamat dari database BPOM dengan database referensi Satuan Lingkungan Setempat (SLS) dari BPS, guna menemukan identifier SLS (`idsubsls_25_2`).

Berikut adalah urutan logika dan cara kerja dari script ini secara langkah demi langkah:

---

## 1. Koneksi & Pengambilan Data (Load Data)
Proses dimulai dengan membuka koneksi ke database SQL Server (menggunakan kredensial dari file `.env`).
- **Data Input:** Memuat seluruh alamat dari tabel BPOM di mana field `is_duplicate = 0`.
- **Data Master:** Mengambil daftar unik `kelurahan_desa_id` dari data input, lalu menarik semua data Master SLS dari tabel referensi **hanya untuk kelurahan-kelurahan tersebut** (demi efisiensi memori).

## 2. Pembuatan Lookup Master (Preprocessing SLS)
Seluruh data Master SLS yang ditarik akan diproses terlebih dahulu sebelum pencocokan dimulai:
- Mengekstrak **Nomor RT** dan **Nomor RW** dari nama SLS (misal: "RT 1 RW 2 DUSUN ASRI"). Script mendukung pengenalan angka arab maupun angka romawi.
- Nama Dusun/Lingkungan dipisahkan dari angka RT/RW untuk keperluan _fuzzy matching_.
- Data-data ini disimpan ke dalam sebuah *Dictionary/Lookup Table* yang dikelompokkan (di-blocking) berdasarkan ID Kelurahan. Ini memastikan alamat di suatu kelurahan tidak akan dibandingkan dengan SLS di kelurahan lain.

## 3. Ekstraksi Data Alamat Input
Untuk setiap baris alamat BPOM, script akan memproses teks alamatnya:
- **Ekstraksi RT & RW:** Menggunakan *Regular Expression* (Regex) untuk mencari pola RT dan RW. Terdapat aturan mutlak bahwa angka pertama yang ditemukan selalu berasosiasi dengan RT, dan angka kedua adalah RW. Angka Romawi juga dikonversi secara otomatis.
- **Ekstraksi Area/Dusun:** Script akan mencari kata kunci penanda area seperti `DUSUN`, `KAMPUNG`, `LINGKUNGAN`, `PERUM`, dll, lalu mengambil kata-kata di sebelahnya untuk dijadikan kunci *fuzzy matching* jika diperlukan.

## 4. Inti Proses Matching (Kandidasi & Seleksi)
Setiap alamat BPOM dicocokkan dengan kandidat SLS di kelurahan yang sama. Alur seleksinya adalah:

1. **Exact RT Match (Filter Utama):**
   - Script mencari SLS yang memiliki angka RT yang persis sama dengan RT di alamat input.

2. **Dusun Fallback (Jika RT tidak ada/tidak cocok):**
   - Jika alamat BPOM tidak memiliki angka RT, **ATAU** jika kandidat SLS yang memiliki RT tersebut kosong (kemungkinan Master SLS kelurahan tersebut tidak pakai RT), maka script akan **mengabaikan filter RT**. Seluruh SLS di kelurahan itu dijadikan kandidat.

3. **Exact RW Match (Penyempitan):**
   - Jika RT cocok (dan tidak fallback), script mengecek apakah ada RW. Jika SLS memiliki nomor RW, maka sistem menyeleksi kandidat dengan RT dan RW yang sama.

4. **Auto-Match (Kandidat Tunggal):**
   - Setelah filter di atas, jika hanya tersisa **1 (satu) kandidat tunggal**, maka script langsung mendeklarasikannya sebagai **MATCH (Skor 100)** tanpa perlu _fuzzy matching_.
   - Jika ada beberapa kandidat, tetapi nama utuhnya identik/duplikat di dalam database master, script juga akan menganggapnya sebagai *MATCH*.

5. **Fuzzy Matching Area (Jika Kandidat Ganda / Fallback):**
   - Jika masih terdapat lebih dari satu kandidat (atau jika sistem berjalan di mode *Fallback* ke Dusun), script akan mengevaluasi kemiripan teks menggunakan algoritma `token_set_ratio`.
   - Area yang diekstrak dari alamat BPOM dibandingkan dengan area dari seluruh kandidat SLS.
   - **Syarat Menang:** 
     Kandidat dengan skor tertinggi akan memenangkan _match_ **HANYA JIKA** skornya lebih besar dari `55` (Threshold) **DAN** selisih skornya (gap) dengan juara kedua adalah minimal `10` poin.
   - Jika gap < 10 atau skor tertingginya < 55, script mengklasifikasikannya sebagai `possible_low_score` atau `possible_multiple_no_area` untuk di-review secara manual.

## 5. Output Data
Setelah seluruh alamat BPOM selesai dihitung, script akan:
- Menghapus tabel hasil (jika sudah ada sebelumnya).
- Membuat tabel baru bernama `[NAMA_TABEL_INPUT]_hasil_match_sls`.
- Menyisipkan seluruh data (*batch insert*) ke dalam tabel baru tersebut, lengkap dengan kolom **Primary Key (id)** (Auto-Increment), hasil ekstraksi RT/RW, dan identitas SLS yang di-match (termasuk `idsubsls_25_2`).

Dengan pendekatan ini, script bisa memetakan alamat skala besar dengan akurasi tinggi sambil menghindari *false positive* saat menghadapi data ambigu.
