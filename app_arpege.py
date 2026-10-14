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
    ecmwf_static_ip = "136.156.129.245"
    suffix = "pl" if levtype == "pl" else "fc"
    step_str = f"{step_val:03d}"
    
    # Stratégie A : Run 00Z d'aujourd'hui
    today_str = datetime.utcnow().strftime("%Y%m%d")
    url_today = f"https://{ecmwf_static_ip}/forecasts/{today_str}/00z/arpege/0p25/oper/{today_str}0000-{step_str}h-oper-{suffix}.grib2"
    if check_url_availability(url_today):
        return url_today, "RUN_AUJOURDHUI_00Z"
        
    # Stratégie B : Repli automatique sur 12Z d'hier
    yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y%m%d")
    adjusted_step = step_val + 12
    adjusted_str = f"{adjusted_step:03d}"
    url_yesterday = f"https://{ecmwf_static_ip}/forecasts/{yesterday_str}/12z/arpege/0p25/oper/{yesterday_str}1200-{adjusted_str}h-oper-{suffix}.grib2"
    return url_yesterday, "RUN_HIER_12Z_AJUSTE"

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
    data = request.json
    levtype = data.get("levtype", "sfc")
    if levtype == "pl":
        params = ["U component of wind (u)", "V component of wind (v)", "Geopotential (z)", "Temperature (t)", "Relative humidity (r)"]
        levels = ["1000 hPa", "850 hPa", "700 hPa", "500 hPa", "400 hPa", "300 hPa", "250 hPa"]
    else:
        params = ["10m U-component of wind (10u)", "10m V-component of wind (10v)", "Mean sea level pressure (msl)", "2m temperature (2t)", "Total precipitation (tp)"]
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
    
    selected_params = [p.split("(")[-1].replace(")", "").strip().lower() for p in selected_params_raw if "(" in p]
    
    selected_levels = []
    for l in selected_levels_raw:
        try:
            parts = str(l).strip().split()
            if parts and parts[0].isdigit():
                selected_levels.append(int(parts[0]))
        except Exception as e:
            print(f"Erreur conversion niveau: {e}")
            continue
            
    if levtype == "sfc":
        selected_levels.append(0)
        
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    # Variables de rapport de diagnostic
    diagnostic_report = {
        "tentatives_telechargement": [],
        "messages_analyses_echantillon": [],
        "total_messages_ecrits": 0,
        "filtres_python": {"params": selected_params, "levels": selected_levels, "mode": levtype}
    }
    
    try:
        with open(output_path, "wb") as fout:
            for step_val in selected_steps:
                url, strategie = get_arpege_routing_strategy(step_val, levtype)
                chunk_raw_path = os.path.join(UPLOAD_FOLDER, f"temp_arp_{step_val}.grib2")
                if os.path.exists(chunk_raw_path): os.remove(chunk_raw_path)
                
                poids_telecharge = 0
                erreur_reseau = "Aucune"
                try:
                    req = urllib.request.Request(url)
                    req.add_header('Host', 'data.ecmwf.int')
                    req.add_header('User-Agent', 'Mozilla/5.0')
                    req.add_header('Connection', 'close')
                    with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
                        donnees = response.read()
                        poids_telecharge = len(donnees)
                        with open(chunk_raw_path, "wb") as f:
                            f.write(donnees)
                except Exception as e:
                    erreur_reseau = str(e)
                
                diagnostic_report["tentatives_telechargement"].append({
                    "url": url,
                    "strategie": strategie,
                    "octets_recus": poids_telecharge,
                    "erreur": erreur_reseau
                })
                
                if os.path.exists(chunk_raw_path) and os.path.getsize(chunk_raw_path) > 0:
                    fin = pygrib.open(chunk_raw_path)
                    sample_count = 0
                    for grb in fin:
                        short_name = str(getattr(grb, 'shortName', '')).lower()
                        level = int(grb.level if hasattr(grb, 'level') else 0)
                        lev_type = str(getattr(grb, 'typeOfLevel', '')).lower()
                        
                        if sample_count < 3:
                            diagnostic_report["messages_analyses_echantillon"].append(
                                f"Msg trouvé -> shortName: {short_name}, level: {level}, typeOfLevel: {lev_type}"
                            )
                            sample_count += 1
                            
                        match_p = short_name in selected_params
                        if levtype == "sfc":
                            match_l = "surface" in lev_type or "ground" in lev_type or level == 0
                        else:
                            match_l = "isobaric" in lev_type and level in selected_levels
                            
                        if match_p and match_l:
                            sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                            grb.values = sub_data
                            fout.write(grb.tostring())
                            diagnostic_report["total_messages_ecrits"] += 1
                    fin.close()
                    os.remove(chunk_raw_path)
                    
        PROGRESS = 100
        if diagnostic_report["total_messages_ecrits"] == 0:
            # INTERCEPTION : Si le fichier est vide, on renvoie tout le journal de bord à l'écran
            return jsonify({
                "success": False, 
                "error": "RAPPORT DE DIAGNOSTIC AUTOMATIQUE", 
                "details": diagnostic_report
            }), 400
            
        return jsonify({"success": True, "download_url": f"/arpege/api/download/{output_filename}"})
    except Exception as e:
        PROGRESS = 0
        return jsonify({"error": f"Crash du moteur binaire : {str(e)}", "details": diagnostic_report}), 500

@app.route('/api/download/<filename>')
def download_file(filename):
    output_path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True)
    return "Fichier introuvable", 404
