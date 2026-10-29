"""Module CEP / IFS (données ouvertes ECMWF) : découpe par paramètres, niveaux, zone et échéances.

Le run (00Z du jour, sinon 12Z de la veille) est choisi lors du scan et renvoyé au navigateur
(run_id) ; l'extraction utilise exactement ce run.

Fonctionnement :
  - l'extraction tourne en tâche de fond (un job par demande), le navigateur interroge /api/job/<id> ;
  - un fichier GRIB par échéance ; un ZIP si plusieurs échéances ;
  - run du jour 00Z si disponible, sinon 12Z de la veille (bascule automatique).
    Les échéances demandées restent relatives à 00Z du jour : en secours, +12 h est ajouté
    à la requête, et les métadonnées GRIB (date, heure, step) restent celles du vrai run,
    donc la date de validité lue par le logiciel est correcte.

Démarrage conseillé sur Render (un seul worker pour que JOBS soit partagé) :
  gunicorn app:app --workers 1 --threads 8 --timeout 120
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pygrib
from ecmwf.opendata import Client
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(BASE_DIR, 'work')
os.makedirs(WORK_DIR, exist_ok=True)

MAX_STEPS = 41           # 0 à 240 h par pas de 6 h
MAX_STEP_HOURS = 360
MAX_RUNNING_JOBS = 2
JOB_TTL = 3600           # secondes avant suppression d'un job et de ses fichiers
SCAN_TTL = 6 * 3600

JOBS = {}
SCAN_CACHE = {}
LOCK = threading.Lock()
JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')


# --------------------------------------------------------------------------- run

def candidate_runs():
    """(date du run, décalage en heures par rapport à 00Z du jour), par ordre de préférence."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [(today, 0), (today - timedelta(hours=12), 12)]


def run_request(run_dt, step, levtype, **extra):
    return dict(date=int(run_dt.strftime('%Y%m%d')), time=f"{run_dt.hour:02d}", step=step,
                stream="oper", type="fc", levtype=levtype, **extra)


def probe_run(client, run_dt, offset, last_step):
    """Vérifie qu'une échéance existe sur ce run (télécharge un seul champ, 2t). Lève une erreur sinon."""
    probe = os.path.join(WORK_DIR, f"probe_{uuid.uuid4().hex}.grib2")
    try:
        client.retrieve(target=probe, **run_request(run_dt, last_step + offset, "sfc", param="2t"))
        if not os.path.exists(probe) or os.path.getsize(probe) == 0:
            raise RuntimeError("fichier vide")
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def choose_run(client, last_step=0):
    """Appelé par le scan : 00Z du jour si disponible, sinon 12Z de la veille."""
    last_error = None
    for run_dt, offset in candidate_runs():
        try:
            probe_run(client, run_dt, offset, last_step)
            return run_dt, offset
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Aucun run disponible (00Z du jour ni 12Z de la veille). Dernière erreur : {last_error}")


def run_from_id(run_id):
    """Retrouve (run_dt, offset) à partir de l'identifiant AAAAMMJJHH renvoyé par le scan."""
    for run_dt, offset in candidate_runs():
        if run_dt.strftime('%Y%m%d%H') == str(run_id):
            return run_dt, offset
    return None


def run_label(run_dt, offset):
    label = f"{run_dt.hour:02d}Z du {run_dt:%d/%m/%Y}"
    return label + (" (run de secours)" if offset else "")


# --------------------------------------------------------------------------- scan

def scan_from_index(run_dt, levtype):
    """Liste paramètres/niveaux à partir du fichier .index (quelques ko) sans télécharger le GRIB."""
    d, h = run_dt.strftime('%Y%m%d'), f"{run_dt.hour:02d}"
    url = f"https://data.ecmwf.int/forecasts/{d}/{h}z/ifs/0p25/oper/{d}{h}0000-0h-oper-fc.index"
    with urllib.request.urlopen(url, timeout=20) as resp:
        rows = [json.loads(line) for line in resp.read().decode().splitlines() if line.strip()]
    params, levels = set(), set()
    for row in rows:
        if row.get("levtype") != levtype:
            continue
        if row.get("param"):
            params.add(row["param"])
        if levtype == "pl" and str(row.get("levelist", "")).isdigit():
            levels.add(int(row["levelist"]))
    if not params:
        raise ValueError("index vide")
    return sorted(params), sorted(levels)


def scan_from_download(client, run_dt, offset, levtype):
    """Plan B : télécharge une échéance complète (lent) et lit son contenu."""
    path = os.path.join(WORK_DIR, f"scan_{uuid.uuid4().hex}.grib2")
    params, levels = set(), set()
    try:
        client.retrieve(target=path, **run_request(run_dt, offset, levtype))
        fin = pygrib.open(path)
        try:
            for grb in fin:
                short = getattr(grb, 'shortName', '')
                if short:
                    params.add(short)
                if levtype == "pl":
                    levels.add(int(getattr(grb, 'level', 0)))
        finally:
            fin.close()
    finally:
        if os.path.exists(path):
            os.remove(path)
    return sorted(params), sorted(levels)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/scan', methods=['POST'])
def scan():
    levtype = (request.json or {}).get("levtype", "sfc")
    if levtype not in ("sfc", "pl"):
        return jsonify({"error": "Mode inconnu."}), 400
    try:
        client = Client(source="ecmwf")
        run_dt, offset = choose_run(client, 0)
        key = (run_dt.isoformat(), levtype)
        cached = SCAN_CACHE.get(key)
        if cached and time.time() - cached["t"] < SCAN_TTL:
            return jsonify(cached["data"])
        try:
            params, levels = scan_from_index(run_dt, levtype)
        except Exception:
            params, levels = scan_from_download(client, run_dt, offset, levtype)
        data = {"success": True, "params": params, "levels": levels, "run": run_label(run_dt, offset),
                "run_id": run_dt.strftime('%Y%m%d%H')}
        SCAN_CACHE[key] = {"t": time.time(), "data": data}
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# --------------------------------------------------------------------------- découpe

def crop_to_file(src, dst, bounds, params, levels):
    """Écrit dans dst les messages de src filtrés et découpés. Retourne le nombre de messages."""
    lat_n = max(float(bounds["n"]), float(bounds["s"]))
    lat_s = min(float(bounds["n"]), float(bounds["s"]))
    lon_w, lon_e = float(bounds["w"]), float(bounds["e"])
    written = 0
    fin = pygrib.open(src)
    try:
        with open(dst, "wb") as fout:
            for grb in fin:
                try:
                    if params and getattr(grb, 'shortName', '') not in params:
                        continue
                    if levels and int(getattr(grb, 'level', 0)) not in levels:
                        continue
                    data, lats, lons = grb.data(lat1=lat_s, lat2=lat_n, lon1=lon_w, lon2=lon_e)
                    if data.ndim != 2 or data.size == 0:
                        continue
                    if lats[0, 0] < lats[-1, 0]:          # nord -> sud pour les lecteurs comme qtVlm
                        data, lats, lons = np.flipud(data), np.flipud(lats), np.flipud(lons)
                    nj, ni = data.shape
                    clone = pygrib.fromstring(grb.tostring())
                    clone.values = data
                    clone['Ni'] = int(ni)
                    clone['Nj'] = int(nj)
                    clone['latitudeOfFirstGridPointInDegrees'] = float(lats[0, 0])
                    clone['latitudeOfLastGridPointInDegrees'] = float(lats[-1, 0])
                    clone['longitudeOfFirstGridPointInDegrees'] = float(lons[0, 0] % 360)
                    clone['longitudeOfLastGridPointInDegrees'] = float(lons[0, -1] % 360)
                    fout.write(clone.tostring())
                    written += 1
                except Exception:
                    continue
    finally:
        fin.close()
    return written


# --------------------------------------------------------------------------- jobs

def cleanup_old_jobs():
    now = time.time()
    for name in os.listdir(WORK_DIR):
        path = os.path.join(WORK_DIR, name)
        try:
            if now - os.path.getmtime(path) > JOB_TTL:
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass
    with LOCK:
        for job_id in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL]:
            del JOBS[job_id]


def update_job(job_id, **fields):
    with LOCK:
        JOBS[job_id].update(fields)


def run_job(job_id, params, levels, steps, bounds, levtype, run_id):
    job_dir = os.path.join(WORK_DIR, job_id)
    try:
        client = Client(source="ecmwf")
        found = run_from_id(run_id)
        if found is None:
            raise RuntimeError("Le run choisi au scan n'est plus valable. Relancez le scan.")
        run_dt, offset = found
        update_job(job_id, message="Vérification des échéances sur le run choisi…")
        try:
            probe_run(client, run_dt, offset, max(steps))
        except Exception as exc:
            raise RuntimeError(f"L'échéance +{max(steps)} h n'est pas encore publiée sur le run {run_label(run_dt, offset)}. "
                               f"Réduisez les échéances ou réessayez plus tard. ({exc})")
        label = run_label(run_dt, offset)
        today_00 = run_dt + timedelta(hours=offset)
        outputs = []

        for i, step in enumerate(steps):
            update_job(job_id, message=f"Échéance +{step} h ({i + 1}/{len(steps)}) — run {label}")
            raw = os.path.join(job_dir, f"raw_{step}.grib2")
            extra = {"param": params}
            if levtype == "pl":
                extra["levelist"] = levels
            try:
                client.retrieve(target=raw, **run_request(run_dt, step + offset, levtype, **extra))
            except Exception as exc:
                raise RuntimeError(f"Échéance +{step} h indisponible : {exc}")

            valid = today_00 + timedelta(hours=step)
            out = os.path.join(job_dir, f"ecmwf_{levtype}_{valid:%Y%m%d_%H}Z.grib2")
            count = crop_to_file(raw, out, bounds, params, levels if levtype == "pl" else [])
            os.remove(raw)
            if count == 0:
                os.remove(out)
                raise RuntimeError(f"Échéance +{step} h : aucun champ dans la zone ou la sélection.")
            outputs.append(out)
            update_job(job_id, progress=int((i + 1) / len(steps) * 95))

        if len(outputs) == 1:
            final = outputs[0]
        else:
            final = os.path.join(job_dir, f"ecmwf_{levtype}_{run_dt:%Y%m%d_%H}Z.zip")
            with zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in outputs:
                    zf.write(path, os.path.basename(path))
            for path in outputs:
                os.remove(path)

        update_job(job_id, status="done", progress=100, message="Terminé", run=label,
                   file=os.path.basename(final), files=len(outputs),
                   size_mb=round(os.path.getsize(final) / 1e6, 2))
    except Exception as exc:
        update_job(job_id, status="error", error=str(exc))


@app.route('/api/extract', methods=['POST'])
def extract():
    d = request.json or {}
    try:
        levtype = d.get("levtype", "sfc")
        params = [str(p) for p in d.get("params", [])]
        levels = sorted({int(x) for x in d.get("levels", [])})
        steps = sorted({int(x) for x in d.get("steps", [])})
        b = {k: float(d["bounds"][k]) for k in ("n", "s", "e", "w")}
        run_id = str(d["run"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Requête incomplète ou invalide."}), 400

    if levtype not in ("sfc", "pl"):
        return jsonify({"error": "Mode inconnu."}), 400
    if run_from_id(run_id) is None:
        return jsonify({"error": "Le run du scan a expiré. Relancez le scan."}), 400
    if not params:
        return jsonify({"error": "Sélectionnez au moins un paramètre."}), 400
    if levtype == "pl" and not levels:
        return jsonify({"error": "Sélectionnez au moins un niveau."}), 400
    if not steps or len(steps) > MAX_STEPS or any(s < 0 or s > MAX_STEP_HOURS for s in steps):
        return jsonify({"error": f"Choisissez entre 1 et {MAX_STEPS} échéances."}), 400
    if not (-90 <= b["s"] < b["n"] <= 90 and -180 <= b["w"] < b["e"] <= 180):
        return jsonify({"error": "Zone invalide (Nord > Sud et Est > Ouest, entre -180° et 180°)."}), 400

    cleanup_old_jobs()
    with LOCK:
        if sum(1 for j in JOBS.values() if j["status"] == "running") >= MAX_RUNNING_JOBS:
            return jsonify({"error": "Serveur occupé, réessayez dans une minute."}), 429
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {"status": "running", "progress": 0, "message": "Démarrage…",
                        "created": time.time(), "file": None, "error": None}
    os.makedirs(os.path.join(WORK_DIR, job_id), exist_ok=True)
    threading.Thread(target=run_job, daemon=True,
                     args=(job_id, params, levels, steps, b, levtype, run_id)).start()
    return jsonify({"job_id": job_id})


@app.route('/api/job/<job_id>')
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        return jsonify({k: v for k, v in job.items() if k != "created"} if job else {"status": "unknown"})


@app.route('/api/download/<job_id>/<filename>')
def download(job_id, filename):
    if not JOB_ID_RE.match(job_id):
        abort(404)
    return send_from_directory(os.path.join(WORK_DIR, job_id), filename, as_attachment=True)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
