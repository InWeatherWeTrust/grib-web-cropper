import sys
import os
import json
import shutil
import urllib.request
import ssl
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file
import pygrib

base_dir = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(base_dir, 'templates')

app = Flask(__name__, template_folder=template_dir)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

UPLOAD_FOLDER = os.path.join(base_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PROGRESS = 0

def check_url_availability(url):
    """Vérifie par une requête HEAD si le run est disponible sur l'IP fixe de l'ECMWF"""
    ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('Host', 'data.ecmwf.int')
        req.add_header('User-Agent', 'Mozilla/5.0')
        with urllib.request.urlopen(req, context=ctx, timeout=3):
            return True
    except Exception:
        return False

def get_arpege_routing_strategy(step_val, levtype="sfc"):
    """Aiguilleur temporel intelligent : bascule de run automatique et gestion de la date"""
    ecmwf_static_ip = "136.156.129.245"
    suffix = "pl" if levtype == "pl" else "fc"
    step_str = f"{step_val:03d}"
    
    # 1. Stratégie A : Run 00Z d'aujourd'hui
    today_str = datetime.utcnow().strftime("%Y%m%d")
    url_today = f"https://{ecmwf_static_ip}/forecasts/{today_str}/00z/arpege/0p25/oper/{today_str}0000-{step_str}h-oper-{suffix}.grib2"
    if check_url_availability(url_today):
        print(f"🎯 [STRATÉGIE A] Utilisation du run 00Z d'aujourd'hui ({today_str})")
        return url_today
        
    # 2. Stratégie B : Repli automatique sur 12Z d'hier avec décalage temporel de +12h
    yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y%m%d")
    adjusted_step = step_val + 12
    adjusted_str = f"{adjusted_step:03d}"
    print(f"⚠️ [STRATÉGIE B] Run du jour absent. Bascule automatique sur 12Z d'hier ({yesterday_str})")
    return f"https://{ecmwf_static_ip}/forecasts/{yesterday_str}/12z/arpege/0p25/oper/{yesterday_str}1200-{adjusted_str}h-oper-{suffix}.grib2"

@app.route('/')
@app.route('/arpege')
def index():
    return render_template('arpege.html')

@app.route('/api/progress', methods=['GET'])
def get_progress():
    global PROGRESS
    return jsonify({"progress": PROGRESS})

@app.route('/api/scan_arpege', methods=['POST'])
def scan_arpege():
    """Génère la liste des paramètres réels basée sur votre scan du fichier binaire"""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    if levtype == "pl":
        # Paramètres d'altitude validés par votre inspection (Paquet IP1)
        params = [
            "U component of wind (u)",
            "V component of wind (v)",
            "Geopotential (z)",
            "Temperature (t)",
            "Relative humidity (r)"
        ]
        levels = ["1000 hPa", "850 hPa", "700 hPa", "500 hPa", "400 hPa", "300 hPa", "250 hPa"]
    else:
        # Paramètres de surface validés (Paquet SP1)
        params = [
            "10m U-component of wind (10u)",
            "10m V-component of wind (10v)",
            "Mean sea level pressure (msl)",
            "2m temperature (2t)",
            "Total precipitation (tp)"
        ]
        levels = ["0 (surface)"]
        
    return jsonify({"success": True, "params": params, "levels": levels})

@app.route('/api/fetch_and_crop', methods=['POST'])
def fetch_and_crop():
    global PROGRESS
    PROGRESS = 5
    
    data = request.json
    bounds = data.get("bounds")
    levtype = data.get("levtype", "sfc")
    selected_params_raw = data.get("params", [])
    selected_levels_raw = data.get("levels", [])
    selected_steps = sorted(list(set([int(s) for s in data.get("steps", [])])))
    
    # Nettoyage des chaînes de filtres
    selected_params = [p.split("(")[-1].replace(")", "").strip().lower() for p in selected_params_raw if "(" in p]
    
    selected_levels = []
    for l in selected_levels_raw:
        try:
            parts = str(l).strip().split()[0]
            if parts and parts.isdigit():
                selected_levels.append(int(parts))
        except:
            continue
    if levtype == "sfc":
        selected_levels.append(0)
        
    total_steps = len(selected_steps)
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                # CORRECTION ICI : Utilisation de l'aiguilleur stratégique à la place de l'ancienne fonction manquante
                url = get_arpege_routing_strategy(step_val, levtype)
                
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"temp_arp_{step_val}.grib2")
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                try:
                    req = urllib.request.Request(url)
                    req.add_header('Host', 'data.ecmwf.int')
                    req.add_header('User-Agent', 'Mozilla/5.0')
                    req.add_header('Connection', 'close')
                    
                    with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
                        with open(chunk_raw_path, "wb") as f:
                            f.write(response.read())
                except Exception as step_error:
                    print(f"Saut de l'échéance {step_val} : {step_error}")
                    continue
                            
                # FILTRAGE ALIGNÉ À 100% SUR VOTRE RÉSULTAT DU TERMINAL
                if os.path.exists(chunk_raw_path) and os.path.getsize(chunk_raw_path) > 0:
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            short_name = str(getattr(grb, 'shortName', '')).lower()
                            level = int(grb.level if hasattr(grb, 'level') else 0)
                            lev_type = str(getattr(grb, 'typeOfLevel', '')).lower()
                            
                            match_p = short_name in selected_params
                            
                            # Validation des hauteurs isobariques (isobaricInhPa) ou de surface
                            if levtype == "sfc":
                                match_l = "surface" in lev_type or "ground" in lev_type or level == 0
                            else:
                                match_l = "isobaric" in lev_type and level in selected_levels
                                
                            if match_p and match_l:
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    os.remove(chunk_raw_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite. Vérifiez la parution temporelle des fichiers."}), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        if os.path.exists(output_path): os.remove(output_path)
        return jsonify({"error": f"Erreur d'extraction binaire : {str(e)}"}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
