# Topik "research": database dev & wallet

Status: **didiskusikan, belum dibangun** (12 Nov 2026). Lanjutkan saat user bilang "research".

## Sumber ide (saran dari luar, ditempel user)

Mulai dari token yang terbang, cari creator/deployer, trace funding dan wallet terkait.
Jangan anggap dev "trusted" hanya karena satu token pernah terbang; cek semua launch lain.

1. Cek runner sekarang dan lama: dev pernah launch apa, supply disebar ke mana, pola jual.
   Catat juga yang gagal/rug supaya historinya lengkap.
2. Cari dari early buyers dan top profit: wallet yang berulang kali masuk awal.
   Pisahkan label trader dan dev; pembeli awal belum tentu tim.
3. Trace koneksi antar wallet: transfer langsung, sumber funding, pola berulang.
4. Jangan simpan alamat saja: simpan catatan kenapa dilacak (token apa, pola jual,
   koneksi, mana yang masih dugaan).
5. Pasang alert, lacak aktivitas berikutnya untuk validasi. Jangan ikut beli tiap notif.
Cicil 3–5 token per hari. 20 wallet yang dipahami > ratusan alamat tanpa alasan.

## Penjelasan yang sudah disepakati

**Trace funding**: wallet baru butuh SOL; ikuti asal SOL itu mundur 1–3 lompatan,
berhenti di exchange (hot wallet = bukan penghubung). Juga ke samping (ke mana supply
dibagi) dan ke bawah (ke mana hasil jual dikumpulkan). Jebakan: exchange, layanan
swap instan, payout bot, dusting → setiap link diberi status *terbukti* / *dugaan*.

**Data kita (11 Nov)**: 105k graduate, 79k creator unik, 73.6k creator hanya 1 token
→ dev serius memakai wallet baru tiap launch, jadi alamat creator saja tidak cukup.
Creator dengan ≥4 graduate: runner 0,6% vs 1,6% creator sekali pakai ("pabrik token").

**Dev = entitas**, bukan satu wallet: deployer + pendana + wallet bundle + wallet exit.
Disimpan: semua launch (termasuk gagal/rug), pola pegang supply, pola jual, koneksi,
catatan. Tidak ada label "trusted"; yang ada rekam jejak (n launch, % runner, % rug).
Pembeli awal = trader kecuali ada bukti koneksi (funding sama, terima token dari dev,
beli di slot bundle).

## Tahap pembangunan (usulan)

- **0 (sudah ada)**: tokens + creator, labels, candles, probe holder/cluster e1.
- **A**: tabel `wallets`, `entities`, `links` (jenis, tx bukti, terbukti/dugaan), `notes`.
- **B**: `/dossier <mint>` di Telegram: deployer, funding 2–3 hop, launch lain (juga yang
  tidak graduate, dari riwayat wallet), deployer saudara (pendana sama), distribusi supply,
  pola jual dev, ke mana hasil jual, 30–50 pembeli awal (tim vs independen).
  Claude menyusun; user menilai lewat `/note` dan `/track`.
- **C**: trader berulang: wallet yang masuk awal di ≥3 runner, cek funding dulu.
- **D**: alert dengan konteks (pendana dev X mendanai wallet baru, deployer X membuat token,
  trader Y masuk awal); e1/r2 mengecek apakah deployer token baru terhubung ke entitas.
  Hasil alert dicatat 72 jam untuk validasi.

Biaya kira-kira: dossier 300–1.500 kredit Helius (paling mahal: riwayat pembeli awal);
5/hari ≈ 100–200 ribu/bulan. Pantau 20 wallet tiap 2 mnt ≈ 430 ribu/bulan (webhook Helius lebih murah).

## Pertanyaan terbuka untuk user

1. Token pertama untuk dossier (usulan: mihao, scamsem, lalu 3 runner berlabel).
2. Menilai lewat Telegram saja, atau perlu halaman web dengan graf koneksi?
3. Paket Helius: gratis atau berbayar?
