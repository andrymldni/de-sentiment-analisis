# Arsitektur

## Diagram alur

```
┌──────────────────────────────────────────────────────────────────────────┐
│ SUMBER                                                                   │
│  Google News RSS · 10 feed redaksi langsung (rss connector saja)         │
│  Google Play (BRImo, BRILink Mobile) · App Store                         │
│  Reddit · YouTube comments · X/Twitter                                   │
│  [fallback] generator korpus sintetis deterministik                      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │  BaseConnector.fetch() → Document
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ ORCHESTRATOR (lintas-konektor, ditulis sekali)                           │
│  Redis: cooldown · checkpoint inkremental · circuit breaker              │
│  Dedup: doc_uid (eksak) + content_hash + SimHash (near-duplicate)        │
│  Filter relevansi · pencatatan run → raw.ingestion_runs                  │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
                    ┌───────────────────────┐
                    │ raw.documents         │  landing zone, immutable
                    └───────────┬───────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ GERBANG KUALITAS DATA (Great Expectations)                               │
│  10 expectation · GAGAL ⇒ exit non-zero ⇒ seluruh downstream di-skip     │
│  Hasil disimpan ke ops.data_quality_results (bukan cuma log)             │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ MESIN SENTIMEN ENSEMBLE                                                  │
│  leksikon komposisional + IndoBERT + emosi + rating + konsensus aspek    │
│  → core.document_sentiment          (skor, keyakinan, sinyal, penjelasan)│
│  → core.document_aspect_sentiment   (ABSA, 8 aspek)                      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ dbt: staging → intermediate → marts                                      │
│  5 staging · 2 intermediate · 9 mart · 79 test (schema + singular)      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ METABASE — dashboard 19 kartu, di-provision otomatis lewat API           │
└──────────────────────────────────────────────────────────────────────────┘

               Seluruhnya diorkestrasi Airflow (LocalExecutor)
```

## Keputusan desain dan alasannya

### Satu envelope `Document` untuk semua sumber
Berita, ulasan aplikasi, komentar YouTube dan tweet punya bentuk yang sangat
berbeda. Semuanya direduksi ke satu bentuk kanonik **di tepi sistem**, sehingga
gerbang kualitas, mesin NLP, dbt, dan Metabase hanya perlu memahami satu skema.
Menambah sumber baru = satu kelas yang mengimplementasikan `fetch()`.

### Concern lintas-konektor ditulis sekali
Cooldown, checkpoint, circuit breaker, dedup, filter relevansi dan pencatatan
run ada di orchestrator — bukan di tiap konektor. Konektor hanya mengurus
"bagaimana bicara dengan upstream".

### Redis untuk state, bukan sekadar cache
- **Cooldown** — TTL key mencegah konektor menghantam upstream berulang kali,
  dan tetap berlaku setelah container restart.
- **Checkpoint** — high-water mark per konektor membuat setiap run inkremental.
  Sengaja dimundurkan 6 jam karena penerbit sering membetulkan timestamp.
- **Circuit breaker** — setelah 3 kegagalan beruntun, konektor dilewati selama
  satu jam alih-alih terus menabrak host yang mati.

Semua mekanisme punya fallback in-memory, sehingga unit test dan eksekusi lokal
tidak butuh Redis.

### Deduplikasi tiga lapis
1. `doc_uid` — hash deterministik (platform + sumber + id eksternal) sebagai
   kunci idempotensi; re-run tidak menduplikasi apa pun.
2. `content_hash` — teks identik dari URL berbeda.
3. **SimHash + jarak Hamming** — artikel sindikasi yang hanya beda satu
   kalimat. Tanpa ini, satu rilis pers Antara yang dimuat ulang 12 media akan
   terlihat seperti 12 sinyal terpisah dan mendistorsi tren.

### Gerbang kualitas yang benar-benar menutup pintu
`validate.py` melempar exception, task Airflow exit non-zero, dan semua task
setelahnya di-skip. Peringatan yang tidak dibaca siapa pun bukan data quality.
Setiap hasil expectation ditulis ke `ops.data_quality_results`, sehingga
dashboard bisa menampilkan kualitas dari waktu ke waktu.

### Idempotensi berbasis versi model
Tahap scoring memilih dokumen yang **belum punya skor untuk versi model saat
ini**. Menaikkan `SENTIMENT_MODEL_VERSION` memicu re-score bersih tanpa
truncate apa pun, dan run yang crash cukup dijalankan ulang.

### Layering dbt yang ketat
- **staging** — satu view per tabel sumber. Hanya penamaan, tipe, dan flag.
- **intermediate** — satu grain kanonik (`int_documents_scored`). Aturan
  *effective label* dan ambang keyakinan KPI didefinisikan **sekali** di sini,
  jadi tidak mungkin ada dua mart yang saling bertentangan.
- **marts** — tabel siap-BI, satu per pertanyaan bisnis.

Project dbt ini sengaja **hermetic**: dua generic test yang dipakai
(`accepted_range`, `unique_combination_of_columns`) diimplementasikan di
`macros/generic_tests.sql` alih-alih ditarik dari `dbt_utils`, sehingga tidak
ada langkah build yang bergantung pada registry eksternal.

### Kegagalan yang terisolasi di Airflow
Setiap konektor adalah task terpisah dengan `trigger_rule=ALL_DONE`. Satu
upstream mati tidak menjatuhkan yang lain; task `consolidate_ingestion` yang
menegakkan syarat sebenarnya ("minimal satu sumber menghasilkan data").

### dbt diisolasi di virtualenv sendiri
`dbt-core` dan Airflow sama-sama mem-pin `click`, `jinja2`, `protobuf`, dan
`packaging`, dan irisan rentangnya berubah tiap rilis. Menginstal dbt
berdampingan dengan Airflow adalah cara paling umum sebuah image Airflow rusak.
Karena DAG memang memanggil `dbt` sebagai subprocess, memindahkannya ke
`/opt/dbt-venv` dengan shim di PATH menghilangkan seluruh kelas kegagalan itu
tanpa mengubah apa pun yang lain.

### Dashboard sebagai kode
Spesifikasi dashboard hidup di `dashboard_spec.py` dan di-provision lewat
Metabase API secara idempoten. Bisa direview di pull request, reproducible di
mesin baru, dan versinya sejalan dengan model yang jadi sumbernya.

## Struktur repositori

```
.
├── src/brilink/
│   ├── settings.py             konfigurasi bertipe (pydantic-settings)
│   ├── logging_config.py       logging terstruktur (teks lokal / JSON di cloud)
│   ├── db.py                   akses DB: bulk upsert, jsonb, healthcheck
│   ├── utils/
│   │   ├── text.py             normalisasi, slang, SimHash, chunking
│   │   ├── ratelimit.py        cooldown, checkpoint, circuit breaker
│   │   └── retry.py            backoff eksponensial + jitter
│   ├── ingestion/
│   │   ├── base.py             kontrak Document + BaseConnector
│   │   ├── registry.py         katalog konektor
│   │   ├── orchestrator.py     concern lintas-konektor
│   │   ├── rss_connector.py    Google News + 10 feed redaksi (satu-satunya sumber berita)
│   │   ├── store_connectors.py Google Play + App Store
│   │   ├── social_connectors.py Reddit, YouTube, X
│   │   ├── seed_connector.py   korpus sintetis deterministik
│   │   └── run_ingestion.py    CLI
│   ├── nlp/
│   │   ├── aspects.py          ABSA (8 aspek): deteksi keyword + skor via IndoBERT
│   │   ├── transformer_model.py IndoBERT + model emosi
│   │   ├── llm_judge.py        arbiter opsional
│   │   ├── ensemble.py         penggabungan sinyal + keyakinan
│   │   └── run_sentiment.py    CLI
│   ├── quality/validate.py     gerbang Great Expectations
│   └── serving/
│       ├── dashboard_spec.py   dashboard sebagai data
│       └── metabase_provision.py
├── dbt/brilink/                5 staging · 2 intermediate · 9 mart, hermetic
├── dags/                       pipeline utama + maintenance mingguan
├── sql/                        DDL warehouse + view operasional
├── tests/                      unit test (fake transformer) + gold set (IndoBERT asli)
├── docker/                     Dockerfile berlapis
└── docs/
```
