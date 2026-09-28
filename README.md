# BRILink Sentiment Intelligence Platform

Pipeline data engineering end-to-end yang memantau persepsi publik terhadap
**BRILink** — jaringan agen laku pandai BRI dengan 1,18 juta agen di 66.450
desa — dari berita nasional, ulasan aplikasi, dan percakapan media sosial;
menilainya dengan mesin sentimen ensemble multi-sinyal; lalu menyajikannya
sebagai dashboard Metabase interaktif yang di-provision otomatis.

> **Bukan cocok-cocokan kata kunci.** Setiap label dihasilkan oleh model
> IndoBERT (bukan leksikon manual) dikombinasikan dengan tiga sinyal lain —
> lengkap dengan skor keyakinan dan probabilitas per kelas yang bisa
> ditelusuri sampai ke dokumen aslinya.
> Detail lengkap: [`docs/SENTIMENT_METHODOLOGY.md`](docs/SENTIMENT_METHODOLOGY.md)

---

## Mulai dalam 3 perintah

```bash
cp .env.example .env
make up       # Postgres · Redis · Airflow · Metabase
make demo     # ingest → validasi → skoring → dbt → dashboard
```

Tanpa `make` (PowerShell / Git Bash):

```bash
cp .env.example .env
docker compose up -d --build
docker compose --profile tools run --rm --build pipeline-runner
```

Image aplikasi di-build lokal dan tidak pernah dipublikasikan ke registry mana
pun, jadi `--build` (atau `pull_policy: build` yang sudah disetel di
`docker-compose.yml`) diperlukan agar Compose tidak mencoba menariknya dari
Docker Hub.

| Layanan | URL | Kredensial |
|---|---|---|
| Metabase | http://localhost:3000 | `admin@brilink.local` / `BrilinkDemo123!` |
| Airflow | http://localhost:8080 | `admin` / `admin` |

Tanpa API key sama sekali pun dashboard akan terisi: konektor yang butuh
kredensial menonaktifkan diri dengan rapi, dan generator korpus sintetis
deterministik mengisi warehouse agar demo tidak pernah kosong. Baris sintetis
selalu ditandai `is_synthetic` dan bisa difilter di setiap mart.

Butuh build cepat/ringan? `make build-light` melewati torch dan transformers
(~2 GB lebih kecil) untuk uji coba struktur pipeline saja — skoring sentimen
akan sangat terdegradasi (cuma rating bintang) karena tidak ada lagi jalur
leksikon sebagai fallback. Lihat `docs/SENTIMENT_METHODOLOGY.md` bagian 8.

---

## Tumpukan teknologi

| Lapisan | Teknologi | Perannya di sini |
|---|---|---|
| Ingestion | `feedparser`, `google-play-scraper`, `praw`, YouTube Data API, X API v2, `pytrends`, `googlemaps`, BeautifulSoup (Kaskus, Maps), Selenium + Chromium headless (teks penuh artikel berita) | 11 konektor, satu envelope dokumen |
| State & politeness | Redis | cooldown, checkpoint inkremental, circuit breaker |
| Warehouse | PostgreSQL 16 | skema `raw` / `core` / `ops` |
| Kualitas data | Great Expectations 1.x | gerbang yang memblokir, hasilnya dipersistensi |
| NLP | IndoBERT + model emosi | ensemble 4 sinyal + ABSA 8 aspek |
| Transformasi | dbt 1.8 | 5 staging · 2 intermediate · 9 mart · 79 test, tanpa paket eksternal |
| Orkestrasi | Airflow 2.9 | TaskGroup, kegagalan terisolasi, retry eksponensial |
| BI | Metabase | 19 kartu, di-provision lewat API sebagai kode |
| Delivery | Docker Compose, GitHub Actions | 5 job CI termasuk uji warehouse sungguhan |

---

## Sumber data

| Konektor | Platform | Kredensial | Catatan |
|---|---|---|---|
| `rss` | Berita | — | Google News RSS per keyword + 19 feed redaksi langsung |
| `expanded_rss` | Berita | — | Feed tambahan + query lebih luas (laku pandai, inklusi keuangan) |
| `web_scraper` | Berita | — | Scrape artikel penuh dari detik, kompas, cnbcindonesia, tempo, tribunnews, merdeka |
| `playstore` | Ulasan | — | BRImo & BRILink Mobile, membawa rating bintang |
| `appstore` | Ulasan | — | storefront Indonesia |
| `reddit` | Sosial | client id/secret | r/indonesia, r/finansial + komentarnya |
| `youtube` | Sosial | API key | komentar yang menyebut BRILink di channel resmi Bank BRI (@bank_bri) |
| `twitter` | Sosial | bearer token | pencarian recent, bahasa Indonesia |
| `kaskus` | Forum | — | thread & balasan forum Kaskus soal BRILink |
| `google_trends` | Search | — | minat pencarian BRILink per waktu & provinsi |
| `google_maps` | Ulasan | API key (opsional) | ulasan lokasi agen BRILink; fallback scraping tanpa key |
| `seed` | — | — | korpus sintetis deterministik (fallback demo) |

Rating bintang dari app store dipakai ganda: sebagai sinyal di dalam ensemble,
**dan** sebagai supervisi lemah untuk mengukur akurasi mesin tanpa perlu
dataset berlabel manual.

---

## Yang membuat mesin sentimennya berbeda

| Kalimat | Pendekatan naif (hitung kata) | Target label |
|---|---|---|
| "Tidak bagus sama sekali" | positif ❌ | **negatif** |
| "Aplikasinya tidak buruk kok" | negatif ❌ | **positif** |
| "Ramah, tapi biayanya mahal banget" | netral ❌ | **negatif** |
| "Mantap banget, uang saya hilang wkwk" | positif ❌ | **negatif** |
| "BRI membantah tuduhan penipuan" | sangat negatif ❌ | **netral** |
| "Kalau biayanya mahal saya pindah" | negatif ❌ | **netral/positif** |
| "Aplikasinya lemooot bgt, gk bisa transfer" | netral ❌ | **negatif** |

Kasus-kasus ini (negasi, sarkasme, kontras, kalimat bantahan, kondisional) ada
di `tests/gold_cases.py` sebagai gold set, dinilai oleh IndoBERT — bukan
dijamin benar lewat aturan manual seperti pendekatan leksikon lama. Angka
akurasi terkini ada di laporan `make bench`, dan regresinya ditegakkan oleh
job `nlp-benchmark` di CI (lihat `docs/SENTIMENT_METHODOLOGY.md` §7).

```bash
make bench   # laporan akurasi per-kasus (butuh torch/transformers)
```

### Empat sinyal, bobot dinormalisasi ulang

```
IndoBERT                 0.55   konteks & framing implisit, chunking sliding-window
klasifikasi emosi        0.15   intensitas afektif pada UGC
rating bintang           0.18   ground truth lemah pada ulasan aplikasi
konsensus antar-aspek    0.12   menghargai kekhususan, skor juga dari IndoBERT
```

Model yang gagal dimuat **tidak** mematikan pipeline — bobotnya dinormalisasi
ulang ke sinyal yang tersedia, dan `mart_engine_quality` mencatat penurunan
cakupannya. Karena tidak ada lagi leksikon sebagai jaring pengaman, dokumen
tanpa rating bintang yang kehilangan sinyal IndoBERT akan jatuh ke netral
berkeyakinan rendah dan masuk antrean tinjauan — lihat
`docs/SENTIMENT_METHODOLOGY.md` §8.

### ABSA: delapan aspek bisnis

`Biaya & Tarif` · `Jaringan & Sistem` · `Layanan Agen` ·
`Dana & Penyelesaian Transaksi` · `Keamanan & Fraud` ·
`Akses & Inklusi Keuangan` · `Kemitraan & Ekonomi Agen` · `Aplikasi Digital`

```
"Agennya ramah tapi sinyal sering gangguan"
   layanan_agen    → +0.50  positif
   jaringan_sistem → −0.44  negatif
```

Satu dokumen, dua vonis berlawanan — inilah yang membuat angka dashboard bisa
ditindaklanjuti.

### Keyakinan dan human-in-the-loop

Dokumen ambigu **tidak dipaksa** diberi label. Ketika sinyal bertentangan atau
buktinya tipis, dokumen masuk `mart_review_queue` dengan peringkat prioritas
yang menggabungkan ketidakpastian, jangkauan, dan keparahan aspek. Adjudikasi
manusia (`reviewed_label`) selalu menang atas label model di seluruh mart.

---

## Dashboard

19 kartu, dibuat otomatis oleh `python -m brilink.serving.metabase_provision`:

**KPI** — total dokumen · Net Sentiment Score · porsi negatif · ukuran antrean tinjauan
**Tren** — volume & NSS bulanan (batang + garis) · komposisi label per kanal (hijau/abu/merah)
**Aspek** — peringkat NSS per aspek · tren bulanan per aspek · matriks aspek × kanal
**Sumber** — scorecard per sumber: NSS periode vs 90 hari terakhir periode itu
**Aksi** — peringatan dini berbasis z-score · antrean tinjauan manual
**Kepercayaan** — kualitas mesin (kecocokan rating & gold set) · kesehatan pipeline & data quality
**Drill-down** — feed dokumen lengkap dengan teks, label, keyakinan, frasa pemicu, dan aspek
**Data quality (engineering)** — tren hasil gerbang kualitas · expectation paling sering gagal ·
freshness ingestion per konektor · snapshot gate terakhir

Spesifikasinya adalah kode (`src/brilink/serving/dashboard_spec.py`), bisa
direview di pull request, dan provisioning-nya idempoten.

**Rentang waktu:** kartu yang punya filter tanggal tidak membatasi jendela
sendiri. Jika filter **Tanggal mulai / Tanggal akhir** dikosongkan, kartu
menampilkan seluruh riwayat (hasil backfill sejak 2019). Jika diisi, kartu
menampilkan persis periode itu, misalnya `2019-01-01` s/d `2021-12-31`. Hanya
kartu yang memang bersifat "saat ini" yang tetap berjendela tetap dan tidak
punya filter tanggal: peringatan dini (7 hari vs baseline 28 hari) dan
freshness konektor (30 hari).

## Output CSV

Selain dashboard, hasil analisis juga diekspor ke CSV di folder `./output`
(otomatis di akhir DAG dan `make demo`, atau manual):

```bash
make export                          # semua data
make export DAYS=30 PLATFORM=news    # 30 hari terakhir, kanal berita saja
# tanpa make, saat stack berjalan:
docker exec brilink_airflow_scheduler python -m brilink.serving.export_csv --output-dir /opt/airflow/output
```

| File | Isi |
|---|---|
| `sentimen_dokumen.csv` | satu baris per dokumen: tanggal, kanal, sumber, judul, **teks lengkap**, tautan, sentimen, skor, keyakinan, emosi, aspek positif/negatif, flag tinjauan |
| `ringkasan_aspek.csv` | per aspek bisnis: jumlah dokumen, positif/netral/negatif, NSS |
| `ringkasan_sumber.csv` | per kanal & sumber: volume, komposisi label, NSS, rata-rata keyakinan |

File berformat UTF-8 (dengan BOM) agar teks Indonesia tampil benar di Excel.
Jika Excel menggabungkan semua kolom jadi satu, tambahkan `--delimiter ";"`.

## Backfill data historis

Run terjadwal hanya mengambil data baru (checkpoint + 90 hari pertama).
Untuk menarik riwayat panjang sekali jalan, pakai `--since`:

```bash
docker exec brilink_airflow_scheduler python -m brilink.ingestion.run_ingestion \
  --connectors rss,youtube,playstore --since 2019-01-01 --limit 20000 --force
docker exec brilink_airflow_scheduler python -m brilink.nlp.run_sentiment
```

Mode backfill tidak menggeser checkpoint, tidak memasang cooldown, dan tidak
memicu circuit breaker, jadi jadwal 6-jam tetap berjalan seperti biasa.

| Konektor | Jangkauan ke belakang |
|---|---|
| `rss` | Google News dipecah per bulan (`after:`/`before:`); hanya judul + cuplikan |
| `youtube` | seluruh komentar channel resmi yang menyebut BRILink |
| `playstore` | BRILink Mobile sampai 2019; BRImo (~150 ulasan/hari) praktis hanya ~2 minggu per 2.000 ulasan — batasi dengan `INGEST_PLAYSTORE_APPS` |
| konektor lain | tidak mendukung riwayat (RSS outlet, App Store, Kaskus, Trends) |

---

## Model data

dbt project ini **tidak punya dependensi paket eksternal** — dua generic test
yang dibutuhkan (`accepted_range`, `unique_combination_of_columns`) ditulis
lokal di `macros/generic_tests.sql`, sehingga build tidak pernah bergantung
pada `hub.getdbt.com` (penting untuk CI dan deployment air-gapped).

```
raw.documents                    landing zone lintas-sumber, immutable
raw.ingestion_runs               audit log per eksekusi konektor
core.document_sentiment          skor + keyakinan + sinyal + penjelasan (JSONB)
core.document_aspect_sentiment   ABSA, satu baris per (dokumen, aspek)
core.dim_aspect                  katalog aspek
ops.data_quality_results         hasil setiap expectation, per run
      │
      ▼ dbt
staging/       5 view    penamaan, tipe, flag
intermediate/  2 view    int_documents_scored ← satu grain kanonik
marts/         9 tabel   siap dikonsumsi BI
```

Aturan *effective label* dan ambang keyakinan KPI didefinisikan **satu kali**
di layer intermediate, sehingga mustahil ada dua mart yang saling bertentangan.

---

## Rekayasa kualitas

| Lapisan | Cakupan |
|---|---|
| Unit test | normalisasi teks, ABSA, ensemble (via fake transformer), ingestion, state store |
| Gold set | 20 kasus berlabel manual dengan ambang akurasi yang ditegakkan CI |
| Great Expectations | 10 expectation sebagai gerbang keras sebelum scoring |
| dbt test | 79 test: schema test plus 4 singular test yang merekonsiliasi mart dengan sumbernya |
| CI | lint · unit · integrasi warehouse sungguhan (Postgres service) · benchmark gold set · build image |

```bash
make test     # suite unit
make cov      # dengan laporan coverage
make lint     # ruff + black + mypy
make bench    # laporan akurasi gold set
```

---

## Perintah yang tersedia

```bash
make help          # daftar lengkap
make up / down     # jalankan / hentikan stack
make demo          # pipeline sekali jalan, end-to-end
make ingest CONNECTORS=rss,playstore
make seed          # muat korpus sintetis
make score         # skor dokumen yang belum dinilai
make rejudge       # kirim antrean tinjauan ke LLM judge (sekali, setelah judge diaktifkan)
make validate      # jalankan gerbang kualitas data
make dbt           # dbt deps + run + test
make dashboard     # provision ulang dashboard Metabase
make clean         # hentikan stack dan hapus seluruh volume
```

---

## Konfigurasi

Semua diatur lewat environment variable dengan default yang sudah berfungsi
(`src/brilink/settings.py`, contoh di `.env.example`).

| Variabel | Default | Fungsi |
|---|---|---|
| `SENTIMENT_ENGINE_MODE` | `ensemble` | `ensemble` / `transformer_only` |
| `SENTIMENT_NEUTRAL_BAND` | `0.12` | lebar pita netral |
| `SENTIMENT_REVIEW_CONFIDENCE_THRESHOLD` | `0.55` | ambang antrean tinjauan |
| `SENTIMENT_MODEL_VERSION` | `brilink-ensemble-v2.0.0` | naikkan untuk memicu re-score bersih |
| `SENTIMENT_ENABLE_LLM_JUDGE` | `false` | eskalasi dokumen berkeyakinan rendah ke LLM |
| `SENTIMENT_LLM_PROVIDER` | `auto` | `auto` / `deepseek` / `anthropic` (auto = key yang terisi, DeepSeek dulu) |
| `DEEPSEEK_API_KEY` | kosong | key DeepSeek untuk LLM judge |
| `SENTIMENT_LLM_MAX_DOCUMENTS` | `300` | batas dokumen ke LLM per run (pengaman biaya) |
| `INGEST_CONNECTORS` | `all` | daftar konektor yang dijalankan |
| `INGEST_COOLDOWN_MINUTES` | `90` | jeda minimum antar-run per konektor |
| `INGEST_YOUTUBE_CHANNEL_IDS` | `UCRHFE_ooDrkEiRRJbog3EjA` | ID channel YouTube yang komentarnya diambil (default: Bank BRI resmi) |
| `INGEST_PLAYSTORE_APPS` | `brimo,brilink_mobile` | aplikasi Google Play yang ulasannya diambil |
| `INGEST_SEED_FALLBACK` | `true` | isi otomatis bila sumber live kosong |
| `INSTALL_TORCH` (build arg) | `true` | `false` menghasilkan image ~2 GB lebih kecil |

---

## Dokumentasi

| Dokumen | Isi |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | diagram alur, keputusan desain dan alasannya |
| [`docs/SENTIMENT_METHODOLOGY.md`](docs/SENTIMENT_METHODOLOGY.md) | cara kerja mesin sentimen + batasannya |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | gejala → diagnosis → tindakan, dan prosedur rutin |

---

## Batasan

Ditulis eksplisit karena angka tanpa konteks lebih berbahaya daripada tidak ada
angka:

- Volume pemberitaan mencerminkan agenda redaksi, **bukan** distribusi opini
  masyarakat. NSS berita bukan hasil survei.
- Ulasan aplikasi condong ke ekstrem — orang menulis saat sangat senang atau
  sangat kesal.
- Sarkasme hanya terdeteksi bila ada penanda eksplisit.
- Aspek bersifat keyword-driven; keluhan tanpa kata kunci aspek tidak terpetakan.
- Data sintetis ditandai `is_synthetic` dan tidak pernah dicampur diam-diam.

Pakai dashboard ini sebagai **pengarah perhatian**, lalu verifikasi lewat kartu
*Feed Dokumen* sampai ke teks aslinya.

---

## Sumber angka domain

- [BRI catat jumlah BRILink capai 1,18 juta agen di 66.450 desa per Maret — ANTARA News](https://www.antaranews.com/berita/5554160/bri-catat-jumlah-brilink-capai-118-juta-agen-di-66450-desa-per-maret)
- [1,18 Juta BRILink Agen Catat Transaksi Rp420 Triliun — Bloomberg Technoz](https://www.bloombergtechnoz.com/detail-news/108078/1-18-juta-brilink-agen-catat-transaksi-rp420-triliun)
- [Risiko yang Harus Dihadapi Agen BRILink — merdeka.com](https://www.merdeka.com/jateng/resiko-yang-harus-dihadapi-agen-brilink-setiap-kali-transaksi-dari-gangguan-sinyal-internet-hingga-human-error-99918-mvk.html)
- [5 Resiko Jadi Agen BRILink — Fastpay](https://www.fastpay.co.id/blog/resiko-jadi-agen-brilink.html)

---

## Lisensi

MIT. Project portofolio; tidak berafiliasi dengan PT Bank Rakyat Indonesia (Persero) Tbk.
