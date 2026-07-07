# Tanya Jawab (Q&A) Evaluasi Script Matching SLS

Dokumen ini merangkum pertanyaan-pertanyaan yang diajukan selama proses pengembangan script `match_alamat_sls.py` beserta jawaban dan analisisnya.

---

### Q1: Apakah query inputnya harus menggunakan filter `WHERE alamat_etl LIKE '%% RT %%'`? Bagaimana kalau Master SLS-nya tidak mengandung RT/RW, tetapi hanya ada nama Dusun saja?

**Jawaban:**
Awalnya script dibuat dengan filter tersebut karena asumsinya kita hanya fokus pada alamat yang eksplisit menyebutkan RT. Namun, setelah dievaluasi, ternyata **terdapat ratusan ribu Master SLS (sekitar 268.000 entri) yang namanya hanya mencantumkan Dusun/Lingkungan tanpa nomor RT**. 

Oleh karena itu, filter `LIKE '%% RT %%'` **sudah dihapus** sehingga seluruh data non-duplikat akan diproses. 
Sebagai solusinya, kita menambahkan algoritma **Dusun Fallback**:
- Jika alamat input tidak memiliki tulisan "RT", ATAU
- Jika Master SLS di kelurahan tersebut sama sekali tidak menggunakan sistem RT (hanya berupa daftar Dusun),
Maka script akan mem-*bypass* proses penyaringan nomor RT, dan langsung melakukan perbandingan kemiripan teks (*Fuzzy Matching*) antara area/dusun dari input alamat melawan seluruh kandidat SLS di kelurahan tersebut.

---

### Q2: Menurut kamu apa kekurangan script ini? Pada kasus apa dia akan menghasilkan salah tebak (*false match*)?

**Jawaban:**
Walaupun script sudah dibekali sistem pertahanan (seperti *exact match* untuk angka RT dan batasan selisih poin untuk *fuzzy*), tidak ada sistem *matching* teks yang 100% sempurna. Berikut adalah beberapa skenario kelemahan di mana script berpotensi menghasilkan *False Match*:

1. **Kasus Salah Ketik RT (Fallback Terlalu Jauh)**
   - *Skenario:* Input = `"RT 3 Dusun Mawar"`. Di database master kelurahan tersebut hanya ada `"RT 2 Dusun Mawar"`.
   - *Error:* Script tidak menemukan RT 3, lalu masuk ke mode *fallback* (hanya membandingkan nama "Mawar"). Karena namanya identik skor 100, script akan menjodohkannya ke "RT 2". Padahal aslinya itu adalah tempat yang berbeda (RT 3 vs RT 2).

2. **Kelemahan Metode `token_set_ratio` Terlalu Toleran**
   - *Skenario:* Input = `"DUSUN JAYA"`. Di database master ada `"RT 1 DUSUN MEKAR JAYA"`.
   - *Error:* Karena metode `token_set_ratio` mengabaikan kata tambahan, skor akan tetap 100 meskipun kata "Mekar" hilang di input. Jika kebetulan tidak ada kandidat lain di kelurahan itu, script akan menganggapnya *Match*.

3. **RW Diabaikan saat Mode Fallback**
   - *Skenario:* Input = `"KAMPUNG MELAYU RW 5"` (tidak ada tulisan RT-nya).
   - *Error:* Script gagal menemukan RT, langsung masuk *fallback* nama Dusun, membandingkan kata "Melayu". Script sama sekali tidak mengecek nilai RW-nya. Jika yang menang ternyata memiliki RW 2, script akan men-statuskannya sebagai *Match*.

4. **Over-Extraction Area di Kota Besar**
   - *Skenario:* Input = `"Jalan Komplek Perumahan Indah RT 1"`.
   - *Error:* Script membuang awalan "PERUM" dan menyisakan kata `"INDAH"` sebagai target pencarian Dusun. Jika di kelurahan yang sama ternyata kebetulan ada Master bernama `"DUSUN INDAH"`, keduanya akan dijodohkan, padahal konteks Perumahan dan Dusun biasanya menunjuk ke lokasi yang berbeda.

5. **Format RT/RW Terbalik (*False Negative*)**
   - *Skenario:* Pengguna BPOM menginput alamat `"RW 01 / RT 03"`.
   - *Error:* Script kita secara kaku mengasumsikan angka sebelum garis miring pasti adalah RT, dan setelahnya pasti RW. Script akan menganggapnya sebagai RT 1, yang akan berujung pada gagal *match* (Unmatch).
