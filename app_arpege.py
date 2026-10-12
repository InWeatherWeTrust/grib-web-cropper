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
    """Vérifie par une requête HEAD rapide si le fichier existe sur le serveur de l'ECMWF"""
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
    """Analyse les catalogues en temps réel, choisit le meilleur run et recalcule l'échéance cible"""
    ecmwf_static_ip = "136.156.129.245"
    suffix = "pl" if levtype == "pl" else "fc"
    
    # 1. STRATÉGIE A : Run 00Z d'AUJOURD'HUI (Idéal)
    today_str = datetime.utcnow().strftime("%Y%m%d")
    url_today = f"https://{ecmwf_static_ip}/forecasts/{today_str}/00z/arpege/0p25/oper/{today_str}0000-{step_val:03d}h-oper-{suffix}.grib2"
    
    if check_url_availability(url_today):
        return url_today, step_val
        
    # 2. STRATÉGIE B : Run 12Z d'HIER (Repli avec décalage temporel de +12h)
    yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y%m%d")
    adjusted_step = step_val + 12
    url_yesterday = f"https://{ecmwf_static_ip}/forecasts/{yesterday_str}/12z/arpege/0p25/oper/{yesterday_str}1200-{adjusted_step:03d}h-oper-{suffix}.grib2"
    
    return url_yesterday, adjusted_step

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
    """Affiche les listes de paramètres cibles. Plus de requêtes réseau instables au démarrage."""
    data = request.json
    levtype = data.get("levtype", "sfc")
    
    if levtype == "pl":
        # On affiche les variables d'altitude requises pour le routage hauturier
        params = [
            "Vent Horizontal (Force/Direction) (wind)",
            "Géopotentiel (Hauteur de pression) (gh)",
            "Température de l'air (t)"
        ]
        levels = ["700 hPa", "500 hPa", "400 hPa", "300 hPa", "250 hPa"]
    else:
        # On affiche les variables de surface fondamentales
        params = [
            "Vent Moyen à 10m (Force/Direction) (wind10m)",
            "Pression moyenne au niveau de la mer (pressure)",
            "Température à 2m (t2m)",
            "Total des Précipitations (tp)"
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
    selected_params_raw = data.get("params",)
    selected_steps = sorted(list(set([int(s) for s in data.get("steps",)])))
    
    # Traduction large des choix de l'utilisateur pour couvrir TOUTES les nomenclatures possibles
    target_categories = []
    for p in selected_params_raw:
        p_lower = str(p).lower()
        if "wind" in p_lower: target_categories.append("wind")
        elif "pressure" in p_lower or "pmer" in p_lower: target_categories.append("pressure")
        elif "t2m" in p_lower or "temperature" in p_lower: target_categories.append("temp")
        elif "gh" in p_lower or "geopotential" in p_lower: target_categories.append("gh")
        elif "tp" in p_lower or "precipitation" in p_lower: target_categories.append("tp")
        
    total_steps = len(selected_steps)
    output_filename = "crop_arpege_world.grib2"
    output_path = os.path.join(UPLOAD_FOLDER, output_filename)
    if os.path.exists(output_path): os.remove(output_path)
    
    ctx = ssl._create_unverified_context()
    
    try:
        with open(output_path, "wb") as fout:
            for step_index, step_val in enumerate(selected_steps):
                PROGRESS = int((step_index / total_steps) * 100)
                
                url, target_step = get_arpege_routing_strategy(step_val, levtype)
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
                            
                if os.path.exists(chunk_raw_path) and os.path.getsize(chunk_raw_path) > 0:
                    fin = pygrib.open(chunk_raw_path)
                    for grb in fin:
                        try:
                            short_name = str(getattr(grb, 'shortName', '')).lower()
                            
                            # CAPTURE LARGE SANS FILTRE STRICT (On intercepte les variantes OMM et Météo-France)
                            is_match = False
                            if "wind" in target_categories and short_name in ['u', 'v', '10u', '10v', 'ucomp', 'vcomp', 'ws', 'wdir']:
                                is_match = True
                            elif "pressure" in target_categories and short_name in ['msl', 'prmsl', 'pres']:
                                is_match = True
                            elif "temp" in target_categories and short_name in ['2t', 't']:
                                is_match = True
                            elif "gh" in target_categories and short_name in ['gh', 'z']:
                                is_match = True
                            elif "tp" in target_categories and short_name in ['tp', 'precip']:
                                is_match = True
                                
                            if is_match:
                                sub_data, lats, lons = grb.data(lat1=bounds["s"], lat2=bounds["n"], lon1=bounds["w"], lon2=bounds["e"])
                                grb.values = sub_data
                                fout.write(grb.tostring())
                        except Exception:
                            continue
                    fin.close()
                    os.remove(chunk_raw_path)
                    
        PROGRESS = 100
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return jsonify({"error": "Aucune donnée n'a pu être extraite. Vérifiez la validité de votre run d'analyse."}), 400
            
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
