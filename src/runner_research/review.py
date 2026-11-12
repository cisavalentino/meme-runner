"""Weekly review of the live rules (`rr review --send`, pm2 rr-review).

Answers "are the thresholds throwing runners away?" with the labels the
daily job writes once a token's 72h window closes:
  - per rule: signals of the last 7 days, scored outcomes
  - per rejection reason (r2, e1): runner % and rug % of rejected tokens vs
    the rule's own signals and vs every labeled token
  - focus buckets: e1 creator holding at minute 1, r2 price at 60m >= 5x
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from . import config
from .telegram import e

RUG = "rug"
MIN_N = 20


def _rate(rows, pred) -> str:
    return f"{sum(map(pred, rows)) / len(rows) * 100:.1f}%" if rows else "–"


def _runner(r) -> bool:
    return r["label"] in config.POSITIVE_LABELS


def _rug(r) -> bool:
    return r["label"] == RUG


def _line(title: str, rows: list) -> str:
    n = len(rows)
    flag = "" if n >= MIN_N else " (sedikit)"
    return f"• {e(title)}: n={n}{flag} · runner {_rate(rows, _runner)} · rug {_rate(rows, _rug)}"


def report(conn: sqlite3.Connection, now: datetime) -> str:
    since7 = (now - timedelta(days=7)).isoformat(timespec="seconds")
    since_lab = (now - timedelta(days=21)).isoformat(timespec="seconds")
    lines = [f"📒 <b>REVIEW MINGGUAN</b> · {now:%d %b %Y}", ""]

    lines.append("<b>Sinyal 7 hari</b> (≥2x dari masuk · rata-rata rencana jual)")
    for rule in ("r2", "e1", "w1", "n1"):
        rs = conn.execute("SELECT status, outcome FROM live_signals WHERE rule = ? AND t0 >= ? "
                          "AND status IN ('signal', 'scored')", (rule, since7)).fetchall()
        sc = [json.loads(r["outcome"] or "{}") for r in rs if r["status"] == "scored"]
        sc = [o for o in sc if o.get("max_mult") is not None]
        res = (f" · dinilai {len(sc)}: {sum(o['max_mult'] >= 2 for o in sc) / len(sc) * 100:.0f}% ≥2x · "
               f"rencana {sum(o.get('plan') or 0 for o in sc) / len(sc):.2f}x") if sc else ""
        lines.append(f"• {rule}: {len(rs)} sinyal ({len(rs) / 7:.1f}/hari){res}")

    for rule in ("r2", "e1"):
        rows = conn.execute("SELECT status, reasons, features, label FROM live_signals WHERE rule = ? AND t0 >= ? "
                            "AND label IS NOT NULL", (rule, since_lab)).fetchall()
        if not rows:
            lines += ["", f"<b>{rule}</b>: belum ada token berlabel (label keluar setelah 72 jam)."]
            continue
        sig = [r for r in rows if r["status"] in ("signal", "scored")]
        rej = [r for r in rows if r["status"] == "rejected"]
        lines += ["", f"<b>{rule}: token berlabel 21 hari</b>", _line("semua token", rows), _line("sinyal", sig)]
        by = defaultdict(list)
        for r in rej:
            for why in json.loads(r["reasons"] or "[]"):
                by[why.split(" dalam")[0] if why.startswith("lonjakan") else why].append(r)
        for why, rs in sorted(by.items(), key=lambda kv: -len(kv[1]))[:10]:
            lines.append(_line(f"ditolak: {why}", rs))
        feats = [(r, json.loads(r["features"] or "{}")) for r in rows]
        if rule == "e1":
            lines.append("<i>Creator pegang di menit 1 (token yang sampai cek holder):</i>")
            for lab, lo, hi in (("≤5%", -1, 5), ("5–10%", 5, 10), ("10–20%", 10, 20), (">20%", 20, 101)):
                lines.append(_line(f"creator {lab}", [r for r, f in feats
                                                      if f.get("dev") is not None and lo < f["dev"] <= hi]))
        if rule == "e1":
            lines.append("<i>Fee trader / volume saat sinyal:</i>")
        for lab, lo, hi in (("<0,1%", -1, .001), ("0,1–0,3%", .001, .003), ("0,3–1%", .003, .01),
                            ("1–10%", .01, .1), (">10%", .1, 99)):
            if rule == "e1":
                lines.append(_line(f"fee {lab}", [r for r, f in feats
                                                   if f.get("fee_ratio") is not None and lo <= f["fee_ratio"] < hi]))
        if rule == "r2":
            lines.append("<i>Harga di menit 60 vs graduation:</i>")
            for lab, lo, hi in (("1,5–5x (zona sinyal)", 1.5, 5), ("5–10x", 5, 10), ("≥10x", 10, 1e9)):
                lines.append(_line(f"60m {lab}", [r for r, f in feats
                                                  if f.get("m60") is not None and lo <= f["m60"] < hi]))
    lines += ["", "<b>w1/n1: gelombang retail vs tidak</b> (sinyal dinilai 21 hari, ≥2x dari masuk)"]
    sc = conn.execute("SELECT features, outcome FROM live_signals WHERE rule IN ('w1', 'n1', 'c1') "
                      "AND status = 'scored' AND t0 >= ?", (since_lab,)).fetchall()
    pairs = [(json.loads(r["features"] or "{}"), json.loads(r["outcome"] or "{}")) for r in sc]
    pairs = [(f, o) for f, o in pairs if o.get("max_mult") is not None and f.get("flow_buys")]
    for lab, pred in (("gelombang retail (≥40% lewat wallet biasa)", lambda f: f.get("retail_share", 0) >= 0.4),
                      ("bukan", lambda f: f.get("retail_share", 0) < 0.4)):
        g = [o for f, o in pairs if pred(f)]
        lines.append(f"• {lab}: n={len(g)} · ≥2x {_rate(g, lambda o: o['max_mult'] >= 2)}")
    lines += ["", "Runner = label slow_climb/runner_other. Kelompok ditolak dengan runner% jauh di atas sinyal "
                  "= batas itu membuang runner; tanyakan Claude untuk menyesuaikan."]
    return "\n".join(lines)
