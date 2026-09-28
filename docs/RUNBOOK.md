# Runbook

Panduan operasional: apa yang harus dilakukan ketika sesuatu terlihat salah.

---

## Urutan pemeriksaan standar

Ketika sebuah angka di dashboard terlihat mencurigakan, periksa dari bawah
ke atas:

1. **`mart_pipeline_health`** — apakah konektor jalan? berapa yield-nya?
2. **`ops.v_ingestion_freshness`** — kapan terakhir tiap sumber menghasilkan data?
3. **`stg_data_quality`** — apakah gerbang kualitas lolos?
4. **`mart_engine_quality`** — apakah semua sinyal masih tersedia?
5. **`mart_document_feed`** — baca teks aslinya.

---

## Gejala dan penanganan

### Dashboard kosong

```sql
SELECT count(*) FROM raw.documents;
SELECT count(*) FROM core.document_sentiment;
SELECT count(*) FROM analytics_marts.mart_sentiment_daily;
```

| Yang kosong | Penyebab paling mungkin | Tindakan |
|---|---|---|
| `raw.documents` | semua konektor gagal / cooldown aktif | `make seed` lalu `make score` |
| `core.document_sentiment` | tahap scoring belum jalan | `make score` |
| mart | dbt belum dijalankan | `make dbt` |
| ketiganya terisi tapi Metabase kosong | Metabase belum sync | `make dashboard` |

### Konektor tidak menghasilkan apa pun

```sql
SELECT connector, status, message, inserted_count, started_at
FROM raw.ingestion_runs ORDER BY started_at DESC LIMIT 20;
```

| Status | Arti | Tindakan |
|---|---|---|
| `skipped_no_credentials` | API key belum diisi | isi di `.env`, atau abaikan |
| `skipped_cooldown` | baru jalan belum lama | `--force` bila memang perlu |
| `skipped_circuit_open` | 3 kegagalan beruntun | cek upstream; reset dengan `DEL brilink:ingest:circuit_open:<nama>` |
| `unavailable` | dependensi Python hilang | rebuild image |
| `failed` | error runtime | baca kolom `message` dan log task |

Reset seluruh state Redis:

```bash
docker compose exec redis redis-cli --scan --pattern 'brilink:ingest:*' | \
  xargs -r docker compose exec -T redis redis-cli DEL
```

### Gerbang kualitas gagal

```sql
SELECT expectation, column_name, observed
FROM ops.data_quality_results
WHERE NOT success ORDER BY checked_at DESC LIMIT 20;
```

| Expectation gagal | Biasanya berarti |
|---|---|
| `ExpectColumnValuesToBeUnique(doc_uid)` | konektor menghasilkan id eksternal yang tidak stabil |
| `ExpectColumnValuesToBeInSet(source_platform)` | platform baru belum didaftarkan di daftar yang dikenal |
| `ExpectColumnValuesToBeBetween(published_at)` | bug parsing tanggal / timezone di konektor |
| `ExpectColumnProportionOfUniqueValues(content_hash)` | lapisan dedup regresi — banyak isi identik lolos |

Gerbang **sengaja** memblokir downstream. Perbaiki datanya, jangan naikkan
toleransinya, kecuali memang ada justifikasi.

### Semua dokumen tiba-tiba jadi netral

Hampir selalu berarti IndoBERT berhenti dimuat dan sinyal lain tidak cukup:

```sql
SELECT event_date, transformer_coverage, avg_confidence
FROM analytics_marts.mart_engine_quality
ORDER BY event_date DESC LIMIT 14;
```

`transformer_coverage` turun ke 0 ⇒ periksa akses ke Hugging Face, atau
jalankan build dengan `PREFETCH_MODELS=true` agar model ikut ke dalam image.

### Antrean tinjauan membengkak

```sql
SELECT review_reason, count(*) FROM analytics_marts.mart_review_queue
GROUP BY 1 ORDER BY 2 DESC;
```

| Alasan dominan | Interpretasi |
|---|---|
| `insufficient_signals` | banyak teks sangat pendek — normal untuk ulasan satu kata |
| `signal_conflict` | model dan rating bintang tidak sepakat — periksa apakah domain bergeser |
| `low_confidence` | ambang mungkin terlalu tinggi untuk kanal ini |

### Skor terlihat "terlalu negatif"

Periksa komposisi kanal. Ulasan aplikasi memang condong negatif; bila
proporsinya naik, NSS gabungan ikut turun tanpa ada perubahan sentimen nyata.

```sql
SELECT source_platform, sum(document_count) AS n,
       round(avg(net_sentiment_score), 1) AS nss
FROM analytics_marts.mart_sentiment_daily
WHERE event_date >= current_date - 30
GROUP BY 1 ORDER BY n DESC;
```

---

## Prosedur rutin

### Re-score seluruh korpus dengan mesin yang diperbarui

```bash
# Naikkan versi -> tahap scoring otomatis menganggap semuanya belum di-score
SENTIMENT_MODEL_VERSION=brilink-ensemble-v2.1.0 make score
make dbt
```
Tidak ada yang di-truncate; skor lama tetap tersimpan untuk perbandingan.

### Mencatat adjudikasi manusia

```sql
UPDATE core.document_sentiment
SET reviewed_label = 'neutral',
    reviewed_by    = 'nama.analis',
    reviewed_at    = now()
WHERE document_id = 12345;
```
`effective_label` di seluruh mart otomatis mengikuti setelah `dbt run`.

### Menangani kasus sentimen yang salah (bukan lagi lewat leksikon)

Karena polaritas sekarang murni dari IndoBERT, tidak ada lagi kamus kata
untuk ditambal manual. Kalau menemukan pola yang konsisten salah:

1. Tambahkan kasusnya ke `tests/gold_cases.py` (`text, expected_label, why`)
   supaya jadi regresi otomatis.
2. `make bench` untuk melihat apakah IndoBERT sudah benar atau memang salah.
3. Kalau memang salah dan pola itu sering muncul, opsinya: (a) aktifkan
   `SENTIMENT_ENABLE_LLM_JUDGE=true` (plus `DEEPSEEK_API_KEY` atau
   `ANTHROPIC_API_KEY`) supaya dokumen berkeyakinan rendah dieskalasi ke
   LLM, lalu `make rejudge` sekali untuk antrean yang sudah ada — lihat
   `docs/SENTIMENT_METHODOLOGY.md` §6, atau (b) adjudikasi manual lewat `reviewed_label`
   (lihat contoh SQL di atas) — ini selalu menang atas label model.
4. Naikkan `SENTIMENT_MODEL_VERSION` bila kamu mengganti model/bobot
   ensemble, lalu re-score.

### Menambah sumber baru

1. Buat kelas di `src/brilink/ingestion/`, implementasikan `fetch()`.
2. Daftarkan di `registry.py`.
3. Tambahkan nama platform ke `KNOWN_PLATFORMS` di `quality/validate.py`,
   ke `accepted_values` di `dbt/brilink/models/sources.yml` dan
   `models/staging/schema.yml`, serta ke `CONSTRAINT`/daftar di `sql/001_schema.sql`.
4. Tambahkan nama konektor ke daftar `CONNECTORS` di DAG.
