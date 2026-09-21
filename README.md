# CRt Calculation Tool MVP

Web tool untuk menghitung **CRt Same Area** dan **CRt Area Total** per cohort 1Y sampai 8Y.

## Fitur

- Upload master UIO/VIN dalam XLSX, XLS, XLSB, atau CSV
- Upload satu atau beberapa achievement file
- Mapping kolom secara fleksibel
- Deduplicate master berdasarkan VIN
- Menggabungkan kemunculan VIN dari seluruh achievement files
- Menghitung Same Area dan Area Total per 1Y sampai 8Y
- Menghitung overall CRt secara weighted
- Export summary dan detail VIN ke Excel

## Cara menjalankan di Windows

1. Install Python 3.11 atau 3.12.
2. Extract ZIP.
3. Buka Command Prompt atau PowerShell di folder project.
4. Jalankan:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

5. Buka `http://127.0.0.1:8000` di Chrome atau Edge.

## Aturan input

### Master UIO

Minimal memiliki:

- VIN
- Sales Year atau Sales Date
- Sales Area

### Achievement

Minimal memiliki:

- VIN
- Service Area

Untuk beberapa achievement file, nama kolom sebaiknya konsisten. Jika struktur berbeda, backend menggabungkannya secara diagonal, tetapi mapping yang dipilih harus tersedia pada baris/file yang ingin dihitung.

## Definisi flag

- `AREA_TOTAL_FLAG = 1` jika VIN ditemukan minimal satu kali pada achievement mana pun.
- `SAME_AREA_FLAG = 1` jika VIN ditemukan minimal satu kali dengan Service Area yang sama dengan Sales Area.
- `AGE = CRt Year - Sales Year`.
- Hanya AGE 1 sampai 8 yang masuk denominator.

## Catatan MVP

- File diproses sementara di server dan dihapus setelah kalkulasi.
- Result file disimpan sementara untuk di-download.
- Untuk production, tambahkan login Entra ID, job queue, object storage, audit log, dan automatic cleanup.
