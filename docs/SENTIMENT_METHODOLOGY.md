# Metodologi Sentimen

> Dokumen ini menjelaskan **mengapa** sebuah dokumen diberi label tertentu, dan
> apa saja batas kemampuan mesinnya. Ditulis untuk dua pembaca: analis yang
> memakai dashboard, dan engineer yang akan memodifikasi mesinnya.

---

## 1. Kenapa bukan hitung kata kunci (lexicon)?

Versi awal project ini pernah memakai leksikon (kamus kata berbobot,
misalnya `kecewa` = −0.6, `mantap` = +0.8) yang dikombinasikan dengan aturan
manual untuk negasi, sarkasme, dan klausa kontras. Pendekatan itu **auditable**
(setiap skor bisa ditelusuri ke kata pemicunya) tapi punya dua masalah serius
sebagai artefak portofolio data engineering:

1. **Tidak generalisasi.** Kamus hanya tahu kata yang sudah dimasukkan
   seseorang secara manual. Slang baru, typo, atau frasa yang belum
   terpikirkan tetap lolos tanpa skor.
2. **Butuh kurasi manusia terus-menerus.** Menambah aturan baru (bobot
   negasi, sarkasme, dsb.) adalah pekerjaan linguistik manual yang tidak
   pernah selesai — dan hasilnya tetap rapuh dibanding model yang sudah
   dilatih pada ratusan ribu contoh berlabel.

Karena itu leksikon dihapus dari project ini. Semua penilaian polaritas
sekarang didelegasikan ke **IndoBERT** — model transformer yang sudah
di-*fine-tune* untuk klasifikasi sentimen Bahasa Indonesia
(`mdhugol/indonesia-bert-sentiment-classification`). Konsekuensinya jujur
diakui di bagian 8: model ini adalah kotak hitam yang tidak bisa lagi
menunjuk ke "kata mana" yang memicu label — yang bisa ditunjukkan adalah
*distribusi probabilitas* dan *tingkat keyakinan*-nya.

---

## 2. Arsitektur mesin: ensemble multi-sinyal

Skor akhir tetap rata-rata tertimbang dari sinyal-sinyal yang **tersedia**,
tapi sekarang semua sinyal polaritas turunan teks (dokumen maupun per-aspek)
bersumber dari model yang sama:

```
                 ┌──────────────────────────────────────────┐
  teks  ───────► │ 1. IndoBERT (transformer)      bobot 0.55 │──┐
                 │ 2. Klasifikasi emosi           bobot 0.15 │──┤
                 │ 3. Rating bintang (jika ada)   bobot 0.18 │──┼──► skor
                 │ 4. Konsensus antar-aspek       bobot 0.12 │──┘    akhir
                 └──────────────────────────────────────────┘
                                    │
                     bobot dinormalisasi ulang atas
                     sinyal yang benar-benar tersedia
```

**Kenapa masih ensemble, bukan cuma IndoBERT polos?** Karena tiap sinyal
tetap punya titik buta sendiri:

| Sinyal | Kuat pada | Lemah pada |
|---|---|---|
| IndoBERT | konteks, framing implisit, negasi | domain drift, teks sangat panjang, sarkasme kering |
| Emosi | intensitas afektif di UGC (ulasan, komentar) | berita faktual yang datar secara emosi |
| Rating bintang | mendekati ground truth | hanya ada pada ulasan aplikasi |
| Konsensus aspek | menghargai kekhususan (bagus/buruk *pada hal apa*) | kosong bila tak ada aspek disebut |

Bila IndoBERT gagal dimuat (tanpa internet, air-gapped, `INSTALL_TORCH=false`
saat build), bobotnya **dinormalisasi ulang** ke sinyal yang tersisa —
tapi karena aspek dan emosi juga bergantung pada model, dalam praktiknya
dokumen tanpa rating bintang (mayoritas berita) akan jatuh ke **netral,
keyakinan rendah, masuk antrean tinjauan** — bukan diam-diam salah. Ini
trade-off yang disengaja: lihat bagian 8.

---

## 3. Cara IndoBERT memutuskan positif / negatif / netral

1. **Chunking.** Artikel panjang dipecah jadi potongan ~180 kata dengan
   overlap 40 kata (maks 6 potongan), karena memotong artikel 900 kata jadi
   384 token saja akan membuang sebagian besar informasi.
2. **Klasifikasi per potongan.** Tiap potongan melewati model dan
   menghasilkan distribusi probabilitas `{positive, neutral, negative}`.
3. **Agregasi berbobot.** Potongan yang lebih panjang dan lebih awal
   (lead/lede berita biasanya paling representatif) diberi bobot lebih
   besar saat digabung.
4. **Skor bertanda.** `skor = P(positive) − P(negative)` — bukan sekadar
   argmax — sehingga distribusi 50/50 jatuh tepat di 0 (netral), bukan
   memaksa memilih satu sisi.
5. **Pita netral (`neutral_band`, default 0.12).** Skor di dalam ±0.12
   dilaporkan sebagai `neutral`; di luar itu jadi `positive`/`negative`.

```
skor > +0.12   → positive
skor < −0.12   → negative
selainnya      → neutral
```

Sinyal emosi (`StevenLimcorn/indonesian-roberta-base-emotion-classifier`)
bekerja serupa tapi mengklasifikasi emosi (marah, senang, sedih, dst.), lalu
tiap emosi dipetakan ke valensi (mis. `anger` → −0.85, `senang` → +0.8) untuk
jadi kontribusi terpisah ke ensemble.

---

## 4. Aspect-Based Sentiment Analysis (ABSA)

Label dokumen menjawab "bagus atau buruk?". Pemilik produk butuh
"bagus atau buruk **pada hal apa**?".

Delapan aspek diturunkan dari lanskap keluhan dan pemberitaan nyata:

| Kode | Aspek | Grup |
|---|---|---|
| `biaya_tarif` | Biaya & Tarif | Komersial |
| `jaringan_sistem` | Jaringan & Sistem | Teknis |
| `layanan_agen` | Layanan Agen | Operasional |
| `dana_transaksi` | Dana & Penyelesaian Transaksi | Operasional |
| `keamanan_fraud` | Keamanan & Fraud | Risiko |
| `akses_inklusi` | Akses & Inklusi Keuangan | Dampak Sosial |
| `kemitraan_agen` | Kemitraan & Ekonomi Agen | Komersial |
| `aplikasi_digital` | Aplikasi Digital | Teknis |

**Deteksi vs skor — dua langkah terpisah:**

1. **Deteksi (keyword, bukan model).** Teks dipecah jadi klausa (kalimat
   *dan* klausa dalam kalimat pada konjungsi seperti "tapi/namun"). Kata
   kunci aspek dicocokkan dengan **batas kata** — `aman` tidak ikut terpicu
   di dalam `keamanan`. Ini murni pencarian kata kunci, tidak butuh model,
   jadi berjalan sangat cepat.
2. **Skor (IndoBERT, dibatch).** Klausa yang menyebut aspek tersebut
   dikumpulkan dari **seluruh dokumen dalam satu batch**, lalu dikirim ke
   IndoBERT dalam **satu kali panggilan model** — bukan satu panggilan per
   aspek per dokumen. Ini menjaga biaya komputasi tetap rendah walau setiap
   dokumen bisa menyebut beberapa aspek sekaligus.

Klausa sengaja **tidak** dilebarkan ke kalimat tetangga: kalau dilebarkan,
"Agennya ramah tapi sinyal sering gangguan" akan membuat kedua aspek
tercampur jadi rata-rata yang tidak informatif. Karena itu setiap aspek
dinilai dari klausanya sendiri saja:

```
"Agennya ramah tapi sinyal sering gangguan"
  → layanan_agen    : klausa "Agennya ramah" saja           → positif
  → jaringan_sistem : klausa "sinyal sering gangguan" saja  → negatif
```

---

## 5. Keyakinan (confidence) dan antrean tinjauan manusia

Skor keyakinan dibentuk dari empat besaran yang bisa diukur:

| Komponen | Bobot | Arti |
|---|---|---|
| Agreement | 0.35 | 1 − sebaran antar-sinyal |
| Coverage | 0.20 | berapa sinyal yang benar-benar tersedia |
| Evidence | 0.20 | panjang teks + "ketegasan" distribusi probabilitas IndoBERT (1 − entropi ternormalisasi) |
| Margin | 0.25 | jarak dari pita netral |

Dokumen dirutekan ke **antrean tinjauan manual** bila:

- sinyal-sinyal kuat saling bertentangan tandanya (`signal_conflict`);
- keyakinan di bawah ambang (`low_confidence`);
- skor tepat di tepi pita netral (`borderline_label`);
- hanya satu sinyal yang tersedia (`insufficient_signals`).

Ini disengaja. Mesin yang memaksakan label pada kasus ambigu terlihat lebih
percaya diri, tetapi lebih sering salah. `mart_review_queue` memberi peringkat
antrean berdasarkan ketidakpastian **dan** dampak (engagement, keparahan
aspek), sehingga waktu manusia dipakai di tempat yang paling berharga.

Adjudikasi manusia disimpan di kolom `reviewed_label` dan **selalu menang**
atas label model di seluruh mart (`effective_label`).

---

## 6. Arbiter LLM (opsional)

Bila `SENTIMENT_ENABLE_LLM_JUDGE=true` dan tersedia API key, dokumen
berkeyakinan rendah — dan hanya itu — dieskalasi ke LLM sebagai sinyal
tambahan (bobot 0.35). Tanpa key, mesin berjalan identik; hanya plafon
otomasinya yang turun.

---

## 7. Evaluasi tanpa dataset berlabel manual

Tiga pemeriksaan independen berjalan terus-menerus (`mart_engine_quality`):

1. **Kecocokan dengan rating bintang** — supervisi lemah dari ratusan ulasan
   aplikasi. Ketidaksepakatan berat (bintang 1 diberi label positif) dilacak
   terpisah.
2. **Kecocokan dengan gold set** — korpus sintetis membawa polaritas yang
   diniatkan; ini regresi otomatis. `tests/test_gold_set.py` menjalankan
   IndoBERT sungguhan pada 20 kasus sulit (negasi, sarkasme, kalimat
   bantahan, kondisional) dan **auto-skip** kalau torch/transformers belum
   terpasang, sehingga tidak memperlambat unit test biasa. Lihat job
   `nlp-benchmark` di `.github/workflows/ci.yml`.
3. **Ketersediaan sinyal** — mendeteksi model yang diam-diam berhenti dimuat.

Jalankan `make bench` (atau `python -m tests.gold_report`) untuk melihat
laporan akurasi yang bisa dibaca manusia, lengkap dengan daftar kasus yang
meleset.

---

## 8. Batasan yang jujur

- **Tanpa rating bintang, IndoBERT adalah satu-satunya sumber polaritas.**
  Tidak ada lagi leksikon sebagai jaring pengaman. Kalau model gagal dimuat
  (offline, `INSTALL_TORCH=false`) dan dokumen tidak punya rating, dokumen
  itu akan jatuh ke `neutral` dengan keyakinan rendah dan masuk antrean
  tinjauan — bukan diam-diam diberi skor yang salah. Ini trade-off eksplisit
  antara *auditability* (leksikon) dan *generalisasi* (model pretrained);
  lihat bagian 1.
- **IndoBERT adalah kotak hitam.** Kita tidak bisa lagi menunjuk "kata mana"
  yang memicu skor — yang tersedia adalah probabilitas per kelas dan entropi
  distribusinya (`signals.transformer.probabilities`,
  `signals.transformer.normalized_entropy` di kolom `signals` JSONB).
- **Sarkasme kering tanpa konteks tetap sulit** untuk model manapun,
  termasuk IndoBERT, terutama dalam potongan teks pendek.
- **Aspek bersifat keyword-driven untuk deteksi.** Keluhan yang diungkapkan
  tanpa satu pun kata kunci aspek tidak akan terpetakan, walau skornya
  sendiri sudah pakai model.
- **Berita ≠ opini publik.** Volume pemberitaan mencerminkan agenda redaksi,
  bukan distribusi pendapat masyarakat. Jangan tafsirkan NSS berita sebagai
  survei.
- **Ulasan aplikasi bias ke ekstrem.** Orang menulis ulasan saat sangat senang
  atau sangat kesal. Sudah tercermin dalam distribusi label per kanal.
- **Data sintetis bukan data nyata.** Setiap baris sintetis ditandai
  `is_synthetic` dan bisa difilter di seluruh mart.

Untuk pengambilan keputusan bisnis, angka di dashboard sebaiknya dipakai
sebagai **pengarah perhatian**, lalu diverifikasi lewat kartu *Feed Dokumen*
sampai ke teks aslinya.
